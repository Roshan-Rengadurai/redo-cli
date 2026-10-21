"""Drive a real interactive pwsh through a pty with the generated hook
dot-sourced. Needs a pty, so it runs on macOS/Linux (pwsh is cross-platform:
same hook code); Windows is covered by the CI install smoke test. Set
REDO_TEST_PWSH to a pwsh binary if it isn't on PATH."""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from redo_cli import shell
from redo_cli.storage import parse_spool

PWSH = os.environ.get("REDO_TEST_PWSH") or shutil.which("pwsh")
WINDOWS = sys.platform == "win32"


def _drive(pwsh, home, lines, settle=1.0):
    """Feed lines to an interactive pwsh; return everything it printed."""
    env = dict(os.environ, HOME=str(home), TERM="xterm", NO_COLOR="1")
    import fcntl, pty, select, struct, termios
    master, slave = pty.openpty()
    # PSReadLine divides by the window width: a bare pty is 0x0
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 200, 0, 0))
    proc = subprocess.Popen([pwsh, "-NoProfile", "-NoLogo"], stdin=slave, stdout=slave,
                            stderr=slave, env=env, close_fds=True, cwd=str(home))
    os.close(slave)
    out = b""

    def drain(seconds):
        nonlocal out
        end = time.time() + seconds
        while time.time() < end:
            r, _, _ = select.select([master], [], [], 0.05)
            if r:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    return
                out += chunk
                # PSReadLine asks the terminal where the cursor is and waits
                # for an answer; play terminal.
                for _ in range(chunk.count(b"\x1b[6n")):
                    os.write(master, b"\x1b[1;1R")

    deadline = time.time() + 30              # pwsh startup: wait for the prompt,
    while b"PS " not in out and time.time() < deadline:   # not a fixed sleep
        drain(0.5)
    drain(0.5)
    for line in lines + ["exit"]:
        os.write(master, (line + "\r").encode())
        drain(settle)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    os.close(master)
    return out.decode(errors="replace")


@unittest.skipUnless(PWSH and not WINDOWS, "needs pwsh and a pty")
class PowerShellHook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.redo_home = self.home / ".redo"
        self.redo_home.mkdir()
        self.spool = self.redo_home / "spool"
        # stub "redo": logs argv, writes $REDO_STUB_PUSH into the push file
        stub_py = self.home / "stub.py"
        stub_py.write_text(
            "import os, sys\n"
            "open(os.path.join(os.environ['HOME'], 'redo-argv'), 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
            "p, f = os.environ.get('REDO_STUB_PUSH'), os.environ.get('REDO_PUSH_FILE')\n"
            "if p and f: open(f, 'w').write(p.replace('|', '\\n') + '\\n')\n")
        stub = self.home / "stub"
        stub.write_text("#!/bin/sh\nexec '%s' '%s' \"$@\"\n" % (sys.executable, stub_py))
        stub.chmod(0o755)
        self.hook = self.redo_home / "hook.ps1"
        self.hook.write_text(shell.hook_source("pwsh", self.spool, str(stub)))

    def tearDown(self):
        self.tmp.cleanup()

    def run_ps(self, *lines):
        return _drive(PWSH, self.home, [". '%s'" % self.hook] + list(lines))

    def records(self):
        return parse_spool(self.spool.read_text()) if self.spool.exists() else []

    def test_command_not_found_is_127(self):
        self.run_ps("gti status")
        recs = self.records()
        self.assertEqual([(r.command, r.exit_code, r.shell) for r in recs], [("gti status", 127, "pwsh")])

    def test_native_exit_code_kept(self):
        fail = "sh -c 'exit 3'"
        self.run_ps(fail)
        self.assertEqual([r.exit_code for r in self.records()], [3])

    def test_cmdlet_error_does_not_reuse_stale_exit_code(self):
        fail = "sh -c 'exit 3'"
        self.run_ps(fail, "Get-Item does-not-exist-zz")
        self.assertEqual([r.exit_code for r in self.records()], [3, 1])

    def test_success_marker(self):
        ok = self.redo_home / "ok"
        self.run_ps("Write-Output fine")
        self.assertTrue(ok.exists())
        self.run_ps("Write-Output fine", "gti")
        self.assertFalse(ok.exists())

    def test_redo_is_not_recorded_and_run_mode_executes(self):
        out = self.run_ps("gti", "$env:REDO_STUB_PUSH = 'run|Write-Output PUSHED-OK'", "redo fix")
        self.assertEqual([r.command for r in self.records()], ["gti"])
        self.assertIn("PUSHED-OK", out)
        self.assertEqual((self.home / "redo-argv").read_text().split(), ["fix"])
        self.assertFalse(list(self.redo_home.glob("push.*")), "push file not cleaned up")

    def test_push_mode_adds_fix_to_history(self):
        out = self.run_ps("$env:REDO_STUB_PUSH = 'push|Write-Output FROM-HIST'", "redo",
                          "[Microsoft.PowerShell.PSConsoleReadLine]::GetHistoryItems()[-2].CommandLine")
        self.assertIn("FROM-HIST", out.split("GetHistoryItems")[-1])


if __name__ == "__main__":
    unittest.main()
