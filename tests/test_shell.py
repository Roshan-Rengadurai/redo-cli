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
        self.assertIn("print -rz", src)

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
        for sh in ("zsh", "bash", "fish"):
            src = shell.hook_source(sh, self.spool)
            for code in ("130", "141", "148"):
                self.assertIn(code, src, sh)
            self.assertIn("_REDO_SKIP", src, sh)
            self.assertIn("REDO_PUSH_FILE", src, sh)
            self.assertIn("'/tmp/redo home'/push.", src, sh)

    def test_unknown_shell_raises(self):
        with self.assertRaises(shell.UnsupportedShell):
            shell.hook_source("csh", self.spool)


class InitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.rc = root / ".zshrc"
        self.hook = root / ".redo" / "hook.zsh"
        self.spool = root / ".redo" / "spool"

    def tearDown(self):
        self.tmp.cleanup()

    def test_source_line_is_marked_and_quoted(self):
        self.assertEqual(shell.source_line(Path("/h/.redo/hook.zsh")),
                         "source '/h/.redo/hook.zsh'  # redo-cli")

    def test_add_init_writes_hook_and_rc_line(self):
        self.assertEqual(shell.add_init("zsh", self.rc, self.hook, self.spool), "added")
        self.assertIn("add-zsh-hook", self.hook.read_text())
        self.assertIn(shell.source_line(self.hook), self.rc.read_text().splitlines())

    def test_add_init_again_regenerates_hook_without_duplicating_line(self):
        shell.add_init("zsh", self.rc, self.hook, self.spool)
        self.hook.write_text("stale")
        self.assertEqual(shell.add_init("zsh", self.rc, self.hook, self.spool), "updated")
        self.assertIn("add-zsh-hook", self.hook.read_text())
        self.assertEqual(self.rc.read_text().count("# redo-cli"), 1)

    def test_remove_init(self):
        shell.add_init("zsh", self.rc, self.hook, self.spool)
        self.assertEqual(shell.remove_init(self.rc, self.hook), "removed")
        self.assertFalse(self.hook.exists())
        self.assertNotIn("redo-cli", self.rc.read_text())
        self.assertEqual(shell.remove_init(self.rc, self.hook), "missing")

    def test_init_installed(self):
        self.assertFalse(shell.init_installed(self.rc, self.hook))
        shell.add_init("zsh", self.rc, self.hook, self.spool)
        self.assertTrue(shell.init_installed(self.rc, self.hook))


if __name__ == "__main__":
    unittest.main()


class PowerShellTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_supported(self):
        self.assertIn("pwsh", shell.SUPPORTED)
        self.assertIn("powershell", shell.SUPPORTED)

    def test_detect_on_windows_without_shell_env(self):
        self.assertIn(shell.detect_shell({"PSModulePath": "C:\\x"}, platform="win32"), ("pwsh", "powershell"))

    def test_hook_file_is_ps1(self):
        self.assertEqual(shell.hook_path("pwsh", self.root), self.root / "hook.ps1")
        self.assertEqual(shell.hook_path("powershell", self.root), self.root / "hook.ps1")

    def test_source_line_dot_sources_with_ps_quoting(self):
        self.assertEqual(shell.source_line(Path("/h/it's/hook.ps1"), "pwsh"),
                         ". '/h/it''s/hook.ps1'  # redo-cli")

    def test_hook_source_is_powershell(self):
        src = shell.hook_source("pwsh", Path("/tmp/r h/spool"))
        for needle in ("function global:prompt", "Get-History -Count 1", "CommandNotFoundException",
                       "AddToHistory", "[char]0x1f", "'/tmp/r h/spool'", "Get-Command -CommandType Application redo"):
            self.assertIn(needle, src)
        self.assertNotIn("`u{", src)           # must run on Windows PowerShell 5.1

    def test_alias_line_and_list(self):
        rc = self.root / "profile.ps1"
        self.assertEqual(shell.alias_line("r", "pwsh"), "Set-Alias -Name r -Value redo  # redo-cli")
        self.assertEqual(shell.add_alias("r", rc, "pwsh"), "added")
        self.assertEqual(shell.list_aliases(rc), ["r"])
        rc.write_text(rc.read_text() + "Set-Alias g git\n")
        self.assertEqual(shell.add_alias("g", rc, "pwsh"), "conflict")

    def test_add_and_remove_init(self):
        rc, hook, spool = self.root / "p.ps1", self.root / ".redo" / "hook.ps1", self.root / ".redo" / "spool"
        self.assertEqual(shell.add_init("pwsh", rc, hook, spool), "added")
        self.assertIn(". '%s'  # redo-cli" % hook, rc.read_text())
        self.assertEqual(shell.remove_init(rc, hook, "pwsh"), "removed")
        self.assertNotIn("redo-cli", rc.read_text())
