import io
import os
import subprocess
import sys
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

    def test_init_installs_hook_and_rc_line(self):
        os.environ["HOME"] = self.tmp.name
        os.environ["SHELL"] = "/bin/zsh"
        rc, out = self._run(["init"])
        self.assertEqual(rc, 0)
        hook = os.path.join(self.tmp.name, "hook.zsh")   # REDO_HOME is the tmp dir
        self.assertTrue(os.path.exists(hook))
        self.assertIn("# redo-cli", open(os.path.join(self.tmp.name, ".zshrc")).read())
        self.assertIn("source", out)

    def test_init_print_writes_nothing(self):
        os.environ["HOME"] = self.tmp.name
        os.environ["SHELL"] = "/bin/zsh"
        rc, out = self._run(["init", "--print"])
        self.assertEqual(rc, 0)
        self.assertIn("add-zsh-hook", out)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, ".zshrc")))

    def test_init_remove(self):
        os.environ["HOME"] = self.tmp.name
        os.environ["SHELL"] = "/bin/zsh"
        self._run(["init"])
        rc, out = self._run(["init", "--remove"])
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "hook.zsh")))
        self.assertNotIn("redo-cli", open(os.path.join(self.tmp.name, ".zshrc")).read())

    def _push_file(self):
        p = os.path.join(self.tmp.name, "push")
        os.environ["REDO_PUSH_FILE"] = p
        self.addCleanup(os.environ.pop, "REDO_PUSH_FILE", None)
        return p

    def _read_push(self, p):
        return open(p).read() if os.path.exists(p) else None

    def test_fix_ingests_spool_before_reading(self):
        from redo_cli.storage import spool_path
        spool_path().write_text("zsh\x1f127\x1f/tmp\x1f5\x1fgti status\x1e")
        rc, out = self._run([])
        self.assertIn("git status", out)
        self.assertFalse(spool_path().exists())

    def test_fix_pushes_single_line_fix(self):
        p = self._push_file()
        self._seed("gti status", "zsh: command not found: gti", code=127)
        rc, out = self._run([])
        self.assertEqual(self._read_push(p), "push\ngit status\n")
        self.assertIn("on your prompt", out)

    def test_fix_strips_trailing_comment_when_pushing(self):
        p = self._push_file()
        self._seed("npm install", "npm ERR! code ETIMEDOUT")
        self._run([])
        self.assertEqual(self._read_push(p), "push\nnpm install\n")

    def test_fix_does_not_push_advice_only_fix(self):
        p = self._push_file()
        self._seed("cat /etc/shadow", "cat: /etc/shadow: Permission denied")
        rc, out = self._run([])
        self.assertIsNone(self._read_push(p))
        self.assertNotIn("on your prompt", out)

    def test_yes_flag_uses_run_mode(self):
        p = self._push_file()
        self._seed("gti status", "zsh: command not found: gti", code=127)
        rc, out = self._run(["-y"])
        self.assertEqual(self._read_push(p), "run\ngit status\n")
        self.assertIn("running", out)

    def test_yes_flag_on_fix_subcommand(self):
        p = self._push_file()
        self._seed("gti status", "zsh: command not found: gti", code=127)
        self._run(["fix", "--yes"])
        self.assertEqual(self._read_push(p), "run\ngit status\n")

    def test_no_push_flag(self):
        p = self._push_file()
        self._seed("gti status", "zsh: command not found: gti", code=127)
        rc, out = self._run(["--no-push"])
        self.assertIsNone(self._read_push(p))
        self.assertNotIn("on your prompt", out)

    def test_unmatched_hook_failure_pushes_redo_run(self):
        p = self._push_file()
        self._seed("make weird-target", "")          # hook-captured: no stderr
        rc, out = self._run(["-y"])                  # -y must NOT auto re-run
        self.assertEqual(self._read_push(p), "push\nredo run make weird-target\n")
        self.assertIn("re-run", out)

    def test_unmatched_hook_failure_quotes_shell_metachars(self):
        p = self._push_file()
        self._seed('python -c "import x" | grep y', "")
        self._run([])
        self.assertEqual(self._read_push(p),
                         "push\nredo run 'python -c \"import x\" | grep y'\n")

    def test_unmatched_with_stderr_pushes_nothing(self):
        p = self._push_file()
        self._seed("make weird-target", "make: *** No rule to make target")
        self._run([])
        self.assertIsNone(self._read_push(p))

    def test_bash_gets_up_arrow_hint(self):
        self._push_file()
        os.environ["REDO_SHELL"] = "bash"
        self.addCleanup(os.environ.pop, "REDO_SHELL", None)
        self._seed("gti status", "zsh: command not found: gti", code=127)
        rc, out = self._run([])
        self.assertIn("press up", out)

    def test_no_push_file_env_prints_only(self):
        self._seed("gti status", "zsh: command not found: gti", code=127)
        rc, out = self._run([])
        self.assertIn("git status", out)
        self.assertNotIn("on your prompt", out)

    def test_run_single_argument_is_executed_verbatim(self):
        env = dict(os.environ, REDO_HOME=self.tmp.name, NO_COLOR="1")
        r = subprocess.run([sys.executable, "-m", "redo_cli", "run",
                            "python3 -c 'import sys; sys.exit(3)'"],
                           env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 3)          # ran as a shell line, not as a command name
        rc, out = self._run(["history"])
        self.assertIn("python3 -c 'import sys; sys.exit(3)'", out)
        self.assertNotIn("'python3 -c", out)      # not re-quoted

    def test_stale_failure_is_labelled_and_not_pushed(self):
        p = self._push_file()
        self._seed("gti status", "zsh: command not found: gti", code=127)   # timestamp 1.0
        open(os.path.join(self.tmp.name, "ok"), "w").close()               # a success since
        rc, out = self._run([])
        self.assertIn("last command succeeded", out)
        self.assertIsNone(self._read_push(p))

    def test_risky_fix_goes_to_history_not_prompt(self):
        p = self._push_file()
        self._seed("git rest --hard", "git: 'rest' is not a git command.\n\n"
                   "The most similar command is\n\treset\n")
        rc, out = self._run(["-y"])
        self.assertEqual(self._read_push(p), "hist\ngit reset --hard\n")
        self.assertIn("press up", out)


if __name__ == "__main__":
    unittest.main()
