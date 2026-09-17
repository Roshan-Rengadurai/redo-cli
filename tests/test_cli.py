import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout

from redo_cli import cli
from redo_cli.models import FailureEvent
from redo_cli.storage import Storage, default_db_path


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["REDO_HOME"] = self.tmp.name
        self._env = {k: os.environ.get(k) for k in ("HOME", "SHELL")}

    def tearDown(self):
        os.environ.pop("REDO_HOME", None)
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def _seed(self, command, stderr, code=1):
        s = Storage(default_db_path())
        s.record(FailureEvent(id=None, command=command, exit_code=code,
                              stderr=stderr, cwd="/tmp", timestamp=1.0, shell="zsh"))
        s.close()

    def _run(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(argv)
        return rc, buf.getvalue()

    def test_fix_outputs_command(self):
        self._seed("git push origin main",
                   "! [rejected] (non-fast-forward) tip of your current branch is behind")
        rc, out = self._run(["fix"])
        self.assertEqual(rc, 0)
        self.assertIn("You ran:", out)
        self.assertIn("git pull --rebase", out)

    def test_fix_with_no_history(self):
        rc, out = self._run(["fix"])
        self.assertEqual(rc, 0)
        self.assertIn("No failures captured", out)

    def test_stats_aggregates_titles(self):
        self._seed("python app.py", "ModuleNotFoundError: No module named 'cv2'")
        rc, out = self._run(["stats"])
        self.assertEqual(rc, 0)
        self.assertIn("python import error", out)

    def test_history_lists_commands(self):
        self._seed("npm run dev", "EADDRINUSE :::3000")
        rc, out = self._run(["history"])
        self.assertEqual(rc, 0)
        self.assertIn("npm run dev", out)

    def test_bare_invocation_runs_fix(self):
        self._seed("python app.py", "ModuleNotFoundError: No module named 'cv2'")
        rc, out = self._run([])
        self.assertEqual(rc, 0)
        self.assertIn("pip install opencv-python", out)

    def test_bare_invocation_with_no_history(self):
        rc, out = self._run([])
        self.assertEqual(rc, 0)
        self.assertIn("No failures captured", out)

    def test_alias_installs_into_shell_rc(self):
        os.environ["HOME"] = self.tmp.name
        os.environ["SHELL"] = "/bin/zsh"
        rc, out = self._run(["alias", "r"])
        self.assertEqual(rc, 0)
        self.assertIn("alias r='redo'  # redo-cli",
                      open(os.path.join(self.tmp.name, ".zshrc")).read())
        self.assertIn("source", out)

    def test_alias_print_only(self):
        os.environ["HOME"] = self.tmp.name
        os.environ["SHELL"] = "/bin/zsh"
        rc, out = self._run(["alias", "--print", "r"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "alias r='redo'  # redo-cli")
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, ".zshrc")))

    def test_alias_lists_installed(self):
        os.environ["HOME"] = self.tmp.name
        os.environ["SHELL"] = "/bin/zsh"
        self._run(["alias", "r", "rd"])
        rc, out = self._run(["alias"])
        self.assertEqual(rc, 0)
        self.assertIn("r", out.split())
        self.assertIn("rd", out.split())

    def test_alias_remove(self):
        os.environ["HOME"] = self.tmp.name
        os.environ["SHELL"] = "/bin/zsh"
        self._run(["alias", "r"])
        rc, out = self._run(["alias", "--remove", "r"])
        self.assertEqual(rc, 0)
        self.assertNotIn("redo", open(os.path.join(self.tmp.name, ".zshrc")).read())

    def test_alias_conflict_exits_nonzero(self):
        os.environ["HOME"] = self.tmp.name
        os.environ["SHELL"] = "/bin/zsh"
        with open(os.path.join(self.tmp.name, ".zshrc"), "w") as f:
            f.write("alias r='ranger'\n")
        rc, out = self._run(["alias", "r"])
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
