"""Drive real shells through the generated hook. Skipped per shell when it
isn't installed. Each session: source hook, run a failing command, exit."""

import os
import sys
import unittest

if sys.platform == "win32":       # zsh/bash/fish via a pty: POSIX only
    raise unittest.SkipTest("POSIX shell hooks need a pty")

import pty
import select
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path

from redo_cli import shell
from redo_cli.storage import parse_spool

SHELL_ARGS = {
    "zsh":  ["zsh", "-f", "-i"],                       # -f: no rc files
    "bash": ["bash", "--norc", "--noprofile", "-i"],
    "fish": ["fish", "--no-config", "-i"],
}


def _env(home):
    return dict(os.environ, HOME=str(home),
                PATH=str(home / "bin") + os.pathsep + os.environ["PATH"],
                TERM="dumb", PS1="$ ", HISTFILE=str(home / ".hist"))


def _session(sh, home, script):
    return subprocess.run(SHELL_ARGS[sh], input=script, env=_env(home), text=True,
                          capture_output=True, timeout=20)


def _pty_session(sh, home, lines, settle=0.4):
    """Drive the shell through a real pty, set up like a terminal: it is the
    shell's controlling tty (fish refuses to run otherwise), has a window
    size, and answers the cursor/device queries fish 4 sends at startup.
    Needed for the line editor (zsh `print -z`) and for fish's preexec/
    postexec events, which only fire for commands typed at a terminal."""
    import fcntl
    import struct
    import termios
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 200, 0, 0))

    def controlling_tty():
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    proc = subprocess.Popen(SHELL_ARGS[sh], stdin=slave, stdout=slave, stderr=slave,
                            env=dict(_env(home), TERM="xterm"), close_fds=True,
                            preexec_fn=controlling_tty)
    os.close(slave)
    out = b""

    def drain(seconds):
        nonlocal out
        end = time.time() + seconds
        while time.time() < end:
            r, _, _ = select.select([master], [], [], 0.05)
            if r:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    return
                if not chunk:
                    return
                out += chunk
                for query, answer in ((b"\x1b[6n", b"\x1b[1;1R"), (b"\x1b[0c", b"\x1b[?62;22c"),
                                      (b"\x1b[c", b"\x1b[?62;22c")):
                    if query in chunk:
                        os.write(master, answer)

    drain(settle)
    for line in lines:
        os.write(master, (line + "\r").encode())
        drain(settle)
    os.write(master, b"exit\r")
    drain(settle)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    os.close(master)
    return out.decode(errors="replace")


class HookIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.redo_home = self.home / ".redo"
        self.redo_home.mkdir()
        self.spool = self.redo_home / "spool"
        # stub `redo` binary: records argv, and pushes what $REDO_STUB_PUSH says
        bin_dir = self.home / "bin"
        bin_dir.mkdir()
        stub = bin_dir / "redo"
        stub.write_text("#!/bin/sh\n"
                        "printf '%s\\n' \"$*\" >> \"$HOME/redo-argv\"\n"
                        "if [ -n \"$REDO_STUB_PUSH\" ] && [ -n \"$REDO_PUSH_FILE\" ]; then\n"
                        "  printf '%s\\n' \"$REDO_STUB_PUSH\" > \"$REDO_PUSH_FILE\"\n"
                        "fi\n"
                        "exit 0\n")
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)

    def tearDown(self):
        self.tmp.cleanup()

    def _hook(self, sh):
        p = self.redo_home / ("hook." + sh)
        p.write_text(shell.hook_source(sh, self.spool))
        return p

    def _records(self):
        return parse_spool(self.spool.read_text()) if self.spool.exists() else []

    def _run(self, sh, body):
        if not shutil.which(sh):
            self.skipTest(sh + " not installed")
        hook = self._hook(sh)
        if sh == "fish":   # fish only fires preexec/postexec for typed commands
            lines = ["source %s" % hook] + [ln for ln in body.split("\n") if ln]
            return subprocess.CompletedProcess(sh, 0, _pty_session(sh, self.home, lines, 0.8), "")
        return _session(sh, self.home, "source %s\n%s\nexit\n" % (hook, body))

    # -- capture ---------------------------------------------------------

    def _check_capture(self, sh):
        self._run(sh, "redo_no_such_cmd_xyz --flag\ntrue\n")
        recs = self._records()
        self.assertEqual(len(recs), 1, recs)
        self.assertEqual(recs[0].command, "redo_no_such_cmd_xyz --flag")
        self.assertEqual(recs[0].exit_code, 127)
        self.assertEqual(recs[0].shell, sh)
        self.assertEqual(recs[0].cwd, os.getcwd())
        self.assertGreater(recs[0].timestamp, 1_600_000_000)

    def test_zsh_captures_failure(self):  self._check_capture("zsh")
    def test_bash_captures_failure(self): self._check_capture("bash")
    def test_fish_captures_failure(self): self._check_capture("fish")

    def _check_success_and_empty_lines_not_recorded(self, sh):
        self._run(sh, "true\n\n\nfalse\n\n\n")
        self.assertEqual([r.command for r in self._records()], ["false"])

    def test_zsh_ignores_success_and_blank_enters(self):  self._check_success_and_empty_lines_not_recorded("zsh")
    def test_bash_ignores_success_and_blank_enters(self): self._check_success_and_empty_lines_not_recorded("bash")
    def test_fish_ignores_success_and_blank_enters(self): self._check_success_and_empty_lines_not_recorded("fish")

    def _check_signal_exit_skipped(self, sh):
        self._run(sh, "sh -c 'exit 130'\nsh -c 'exit 141'\nsh -c 'exit 148'\nsh -c 'exit 2'\n")
        self.assertEqual([r.exit_code for r in self._records()], [2])

    def test_zsh_skips_signal_exits(self):  self._check_signal_exit_skipped("zsh")
    def test_bash_skips_signal_exits(self): self._check_signal_exit_skipped("bash")
    def test_fish_skips_signal_exits(self): self._check_signal_exit_skipped("fish")

    def _check_pipeline_not_found_captured(self, sh):
        self._run(sh, "redo_no_such_cmd_xyz | cat\n")
        recs = self._records()
        self.assertEqual([(r.command, r.exit_code) for r in recs],
                         [("redo_no_such_cmd_xyz | cat", 127)])

    def test_zsh_captures_not_found_inside_pipeline(self):  self._check_pipeline_not_found_captured("zsh")
    def test_bash_captures_not_found_inside_pipeline(self): self._check_pipeline_not_found_captured("bash")
    def test_fish_captures_not_found_inside_pipeline(self): self._check_pipeline_not_found_captured("fish")

    def _check_success_marker(self, sh):
        ok = self.redo_home / "ok"
        self._run(sh, "true\n")
        self.assertTrue(ok.exists(), "success should create the marker")
        self._run(sh, "true\nfalse\n")
        self.assertFalse(ok.exists(), "a failure must delete it, same second or not")

    def test_zsh_success_marker(self):  self._check_success_marker("zsh")
    def test_bash_success_marker(self): self._check_success_marker("bash")

    # -- the redo function -------------------------------------------------

    def _check_redo_invocation_not_recorded(self, sh):
        self._run(sh, "false\nredo\nredo fix\n")
        self.assertEqual([r.command for r in self._records()], ["false"])
        argv = (self.home / "redo-argv").read_text().splitlines()
        self.assertEqual(argv, ["", "fix"])

    def test_zsh_redo_not_recorded(self):  self._check_redo_invocation_not_recorded("zsh")
    def test_bash_redo_not_recorded(self): self._check_redo_invocation_not_recorded("bash")
    def test_fish_redo_not_recorded(self): self._check_redo_invocation_not_recorded("fish")

    def _check_run_mode_evals(self, sh):
        # stub pushes "run\necho PUSHED-OK"; the function must eval it
        script = "export REDO_STUB_PUSH='run\necho PUSHED-OK'\nredo\n"
        if sh == "fish":
            script = "set -x REDO_STUB_PUSH 'run\necho PUSHED-OK'\nredo\n"
        r = self._run(sh, script)
        self.assertIn("PUSHED-OK", r.stdout)
        self.assertFalse(list(self.redo_home.glob("push.*")), "push file not cleaned up")

    def test_zsh_run_mode_evals(self):  self._check_run_mode_evals("zsh")
    def test_bash_run_mode_evals(self): self._check_run_mode_evals("bash")
    def test_fish_run_mode_evals(self): self._check_run_mode_evals("fish")

    def test_zsh_push_mode_loads_editor_buffer(self):
        # `print -z` needs ZLE, so this one runs through a pty. After `redo`
        # the next prompt should already contain the pushed line; a bare
        # Enter then executes it.
        if not shutil.which("zsh"):
            self.skipTest("zsh not installed")
        hook = self._hook("zsh")
        out = _pty_session("zsh", self.home, [
            "source %s" % hook,
            "export REDO_STUB_PUSH=$'push\\necho FROM-BUFFER'",
            "redo",
            "",                      # accept the pre-filled line
        ])
        self.assertIn("echo FROM-BUFFER", out)                 # drawn in the editor
        self.assertRegex(out, r"(?<!echo )FROM-BUFFER")         # ...and then executed

    def test_zsh_push_keeps_backslash_continuations(self):
        if not shutil.which("zsh"):
            self.skipTest("zsh not installed")
        hook = self._hook("zsh")
        out = _pty_session("zsh", self.home, [
            "source %s" % hook,
            "export REDO_STUB_PUSH=$'push\\necho JOINED \\\\\\nTOGETHER'",
            "redo",
            "",
        ])
        self.assertIn("JOINED TOGETHER", out)

    def test_bash_push_mode_replaces_redo_in_history(self):
        # `history -s` swaps the `redo` entry for the fix, so up-arrow lands
        # on the fix directly.
        r = self._run("bash", "export REDO_STUB_PUSH='push\necho FROM-HIST'\nredo\nhistory 2\n")
        lines = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
        self.assertTrue(any(ln.endswith("echo FROM-HIST") for ln in lines), r.stdout)
        self.assertFalse(any(ln.endswith("  redo") for ln in lines), r.stdout)


if __name__ == "__main__":
    unittest.main()
