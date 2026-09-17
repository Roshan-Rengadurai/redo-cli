import unittest

from redo_cli.engine import diagnose
from redo_cli.models import Confidence, FailureEvent


def ev(command="", exit_code=1, stderr=""):
    return FailureEvent(id=None, command=command, exit_code=exit_code,
                        stderr=stderr, cwd="/tmp", timestamp=0.0, shell="zsh")


class RuleTests(unittest.TestCase):
    def test_git_push_rejected(self):
        d = diagnose(ev(
            "git push origin main", 1,
            "! [rejected] main -> main (non-fast-forward)\n"
            "error: failed to push some refs ... tip of your current branch is behind"))
        self.assertEqual(d.rule_id, "git_push_rejected")
        self.assertIn("git pull --rebase", d.fix_command)
        self.assertEqual(d.confidence, Confidence.HIGH)

    def test_git_push_other_failure_is_not_claimed(self):
        # auth failure on push should NOT be diagnosed as "behind"
        d = diagnose(ev("git push origin main", 1,
                        "fatal: Authentication failed for 'https://...'"))
        self.assertNotEqual(d.rule_id, "git_push_rejected")

    def test_port_in_use(self):
        d = diagnose(ev("npm run dev", 1,
                        "Error: listen EADDRINUSE: address already in use :::3000"))
        self.assertEqual(d.rule_id, "port_in_use")
        self.assertIn("3000", d.fix_command)

    def test_python_import_maps_pip_name(self):
        d = diagnose(ev("python app.py", 1,
                        "ModuleNotFoundError: No module named 'cv2'"))
        self.assertEqual(d.rule_id, "python_import_error")
        self.assertIn("pip install opencv-python", d.fix_command)

    def test_node_module_missing(self):
        d = diagnose(ev("node server.js", 1, "Error: Cannot find module 'express'"))
        self.assertEqual(d.rule_id, "node_module_missing")
        self.assertIn("npm install express", d.fix_command)

    def test_node_scoped_module(self):
        d = diagnose(ev("node x.js", 1, "Error: Cannot find module '@scope/pkg'"))
        self.assertIn("npm install @scope/pkg", d.fix_command)

    def test_command_typo(self):
        d = diagnose(ev("gti status", 127, "zsh: command not found: gti"))
        self.assertEqual(d.rule_id, "command_typo")
        self.assertEqual(d.fix_command, "git status")

    def test_command_not_found_generic(self):
        d = diagnose(ev("foobar", 127, "zsh: command not found: foobar"))
        self.assertEqual(d.title, "command not found")
        self.assertEqual(d.rule_id, "command_not_found")

    def test_npm_eresolve(self):
        d = diagnose(ev("npm install", 1, "npm ERR! code ERESOLVE\nnpm ERR! ..."))
        self.assertEqual(d.rule_id, "npm_install_failure")
        self.assertIn("legacy-peer-deps", d.fix_command)

    def test_permission_denied_script(self):
        d = diagnose(ev("./deploy.sh", 126, "zsh: permission denied: ./deploy.sh"))
        self.assertEqual(d.rule_id, "permission_denied_exec")
        self.assertIn("chmod +x ./deploy.sh", d.fix_command)

    def test_merge_conflict(self):
        d = diagnose(ev(
            "git merge feature", 1,
            "Auto-merging app.py\nCONFLICT (content): Merge conflict in app.py\n"
            "Automatic merge failed; fix conflicts and then commit the result."))
        self.assertEqual(d.rule_id, "merge_conflict")

    def test_missing_virtualenv_pep668(self):
        d = diagnose(ev("pip install requests", 1,
                        "error: externally-managed-environment"))
        self.assertEqual(d.rule_id, "missing_virtualenv")
        self.assertIn("venv", d.fix_command)

    def test_brew_missing(self):
        d = diagnose(ev("brew install foobarbaz", 1,
                        'Warning: No available formula with the name "foobarbaz".'))
        self.assertEqual(d.rule_id, "brew_package_missing")
        self.assertIn("brew search foobarbaz", d.fix_command)

    def test_fallback_is_actionable(self):
        d = diagnose(ev("some weird thing", 1, "kaboom unexpected"))
        self.assertFalse(d.matched)
        self.assertEqual(d.title, "uncategorized")
        self.assertTrue(d.fix_command)  # never empty


if __name__ == "__main__":
    unittest.main()
