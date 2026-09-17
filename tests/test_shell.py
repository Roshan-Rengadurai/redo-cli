import os
import tempfile
import unittest
from pathlib import Path

from redo_cli import shell


class RcPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_zsh_uses_zshrc(self):
        self.assertEqual(shell.rc_path("zsh", self.home), self.home / ".zshrc")

    def test_fish_uses_config_fish(self):
        self.assertEqual(shell.rc_path("fish", self.home),
                         self.home / ".config" / "fish" / "config.fish")

    def test_bash_uses_bashrc(self):
        self.assertEqual(shell.rc_path("bash", self.home, platform="linux"),
                         self.home / ".bashrc")

    def test_bash_on_macos_uses_bash_profile(self):
        self.assertEqual(shell.rc_path("bash", self.home, platform="darwin"),
                         self.home / ".bash_profile")

    def test_unknown_shell_raises(self):
        with self.assertRaises(shell.UnsupportedShell):
            shell.rc_path("csh", self.home)

    def test_detect_shell_from_env(self):
        self.assertEqual(shell.detect_shell({"SHELL": "/usr/local/bin/fish"}), "fish")


class AliasTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rc = Path(self.tmp.name) / ".zshrc"

    def tearDown(self):
        self.tmp.cleanup()

    def test_alias_line_is_marked(self):
        self.assertEqual(shell.alias_line("r"), "alias r='redo'  # redo-cli")

    def test_add_alias_creates_rc_if_missing(self):
        status = shell.add_alias("r", self.rc)
        self.assertEqual(status, "added")
        self.assertIn("alias r='redo'  # redo-cli", self.rc.read_text())

    def test_add_alias_appends_on_own_line(self):
        self.rc.write_text("export FOO=1")  # no trailing newline
        shell.add_alias("r", self.rc)
        self.assertEqual(self.rc.read_text().splitlines(),
                         ["export FOO=1", "alias r='redo'  # redo-cli"])

    def test_add_alias_is_idempotent(self):
        shell.add_alias("r", self.rc)
        status = shell.add_alias("r", self.rc)
        self.assertEqual(status, "exists")
        self.assertEqual(self.rc.read_text().count("alias r="), 1)

    def test_add_alias_refuses_to_clobber_foreign_alias(self):
        self.rc.write_text("alias r='ranger'\n")
        status = shell.add_alias("r", self.rc)
        self.assertEqual(status, "conflict")
        self.assertNotIn("redo", self.rc.read_text())

    def test_add_alias_rejects_bad_names(self):
        for bad in ["", "a b", "x;rm", "-r", "r'"]:
            with self.assertRaises(ValueError):
                shell.add_alias(bad, self.rc)

    def test_remove_alias_removes_only_ours(self):
        self.rc.write_text("alias r='redo'  # redo-cli\nalias g='git'\n")
        self.assertEqual(shell.remove_alias("r", self.rc), "removed")
        self.assertEqual(self.rc.read_text(), "alias g='git'\n")

    def test_remove_alias_when_absent(self):
        self.rc.write_text("alias g='git'\n")
        self.assertEqual(shell.remove_alias("r", self.rc), "missing")

    def test_list_aliases_returns_only_ours(self):
        self.rc.write_text("alias g='git'\nalias r='redo'  # redo-cli\nalias rd='redo'  # redo-cli\n")
        self.assertEqual(shell.list_aliases(self.rc), ["r", "rd"])

    def test_list_aliases_missing_rc(self):
        self.assertEqual(shell.list_aliases(self.rc), [])


class HookSourceTests(unittest.TestCase):
    def setUp(self):
        self.spool = Path("/tmp/redo home/spool")

    def test_hook_path(self):
        self.assertEqual(shell.hook_path("zsh", Path("/h/.redo")), Path("/h/.redo/hook.zsh"))

    def test_zsh_hook_writes_record_to_spool(self):
        src = shell.hook_source("zsh", self.spool)
        self.assertIn("add-zsh-hook preexec _redo_preexec", src)
        self.assertIn("add-zsh-hook precmd _redo_precmd", src)
        self.assertIn(r"printf '%s\037%s\037%s\037%s\037%s\036'", src)
        self.assertIn(">> '/tmp/redo home/spool'", src)
        self.assertIn("print -z", src)

    def test_bash_hook_prepends_prompt_command(self):
        src = shell.hook_source("bash", self.spool)
        self.assertIn("PROMPT_COMMAND", src)
        self.assertIn("history 1", src)
        self.assertIn("history -s", src)

    def test_fish_hook_uses_events(self):
        src = shell.hook_source("fish", self.spool)
        self.assertIn("--on-event fish_preexec", src)
        self.assertIn("--on-event fish_postexec", src)
        self.assertIn("commandline -r", src)

    def test_every_hook_skips_signal_exits_and_defines_redo(self):
        for sh in shell.SUPPORTED:
            src = shell.hook_source(sh, self.spool)
            for code in ("130", "141", "148"):
                self.assertIn(code, src, sh)
            self.assertIn("_REDO_SKIP", src, sh)
            self.assertIn("REDO_PUSH_FILE", src, sh)
            self.assertIn("'/tmp/redo home'/push.", src, sh)

    def test_unknown_shell_raises(self):
        with self.assertRaises(shell.UnsupportedShell):
            shell.hook_source("csh", self.spool)


if __name__ == "__main__":
    unittest.main()
