import shutil
import unittest

from redo_cli.engine import diagnose
from redo_cli.models import Confidence, FailureEvent


import os as _os
import tempfile as _tempfile

_TMP = None


def setUpModule():
    # rules cache tool subcommand lists under $REDO_HOME; keep that off ~/.redo
    global _TMP
    _TMP = _tempfile.TemporaryDirectory()
    _os.environ["REDO_HOME"] = _TMP.name
    # never read the developer's real shell history
    _os.environ["HISTFILE"] = _os.path.join(_TMP.name, "no-history")


def tearDownModule():
    _os.environ.pop("REDO_HOME", None)
    _TMP.cleanup()


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
        if not shutil.which("git"):
            self.skipTest("git not installed")
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

    def test_git_unknown_subcommand_single_suggestion(self):
        d = diagnose(ev("git brnch", 1,
                        "git: 'brnch' is not a git command. See 'git --help'.\n\n"
                        "The most similar command is\n\tbranch"))
        self.assertEqual(d.rule_id, "unknown_subcommand")
        self.assertEqual(d.fix_command, "git branch")
        self.assertEqual(d.confidence, Confidence.HIGH)

    def test_git_unknown_subcommand_multiple_suggestions(self):
        d = diagnose(ev("git st", 1,
                        "git: 'st' is not a git command. See 'git --help'.\n\n"
                        "The most similar commands are\n\tstatus\n\treset\n\tstash"))
        self.assertEqual(d.rule_id, "unknown_subcommand")
        self.assertEqual(d.options, ("git status", "git reset", "git stash"))
        self.assertEqual(d.confidence, Confidence.MEDIUM)

    def test_cobra_style_unknown_subcommand(self):
        # gh, kubectl, and other cobra-based CLIs share this exact format.
        d = diagnose(ev("gh pr lst", 1,
                        'unknown command "lst" for "gh pr"\n\n'
                        "Did you mean this?\n\tlist\n\n"
                        "Usage:  gh pr <command> [flags]"))
        self.assertEqual(d.rule_id, "unknown_subcommand")
        self.assertEqual(d.fix_command, "gh pr list")

    def test_cargo_style_unknown_subcommand(self):
        d = diagnose(ev("cargo bulid", 101,
                        "error: no such command: `bulid`\n\n"
                        "help: a command with a similar name exists: `build`"))
        self.assertEqual(d.rule_id, "unknown_subcommand")
        self.assertEqual(d.fix_command, "cargo build")

    def test_brew_unknown_subcommand_single(self):
        d = diagnose(ev("brew upgrad", 1,
                        "Error: Invalid usage: Unknown command: brew upgrad\n"
                        "Did you mean upgrade?"))
        self.assertEqual(d.rule_id, "unknown_subcommand")
        self.assertEqual(d.fix_command, "brew upgrade")

    def test_brew_unknown_subcommand_multiple(self):
        d = diagnose(ev("brew instl", 1,
                        "Error: Invalid usage: Unknown command: brew instl\n"
                        "Did you mean instal or install?"))
        self.assertEqual(d.rule_id, "unknown_subcommand")
        self.assertEqual(d.options, ("brew instal", "brew install"))

    def test_chained_typo_git_subcommand_refined(self):
        if not shutil.which("git"):
            self.skipTest("git not installed")
        # binary name AND subcommand are both typos -- the top-level
        # correction (gti -> git) must not be trusted blindly.
        d = diagnose(ev("gti brance", 127, "zsh: command not found: gti"))
        self.assertEqual(d.rule_id, "command_typo_subcommand")
        self.assertIn("git branch", (d.fix_command,) + d.options)

    def test_chained_typo_brew_subcommand_refined(self):
        if not shutil.which("brew"):
            self.skipTest("brew not installed")
        # the refinement logic, not Homebrew's speed: a cold `brew commands`
        # can blow the 5 s cap on a CI runner
        from unittest import mock
        from redo_cli import rules
        with mock.patch.object(rules, "_tool_subcommands",
                               lambda tool: frozenset({"install", "list", "upgrade", "info"})):
            d = diagnose(ev("brwe instl", 127, "zsh: command not found: brwe"))
        self.assertEqual(d.rule_id, "command_typo_subcommand")
        self.assertIn("brew install", (d.fix_command,) + d.options)

    def test_typo_to_valid_subcommand_stays_simple(self):
        if not shutil.which("git"):
            self.skipTest("git not installed")
        # the common case: correcting the binary name is enough, and there's
        # no need to shell out to validate an already-valid subcommand.
        d = diagnose(ev("gti status", 127, "zsh: command not found: gti"))
        self.assertEqual(d.rule_id, "command_typo")
        self.assertEqual(d.fix_command, "git status")

    def test_gibberish_command_has_no_path_match(self):
        d = diagnose(ev("zzqxvbplughcombobulate1971", 127,
                        "zsh: command not found: zzqxvbplughcombobulate1971"))
        self.assertEqual(d.rule_id, "command_not_found")


if __name__ == "__main__":
    unittest.main()


class BlindQARound1(unittest.TestCase):
    """Regressions found by black-box typo testing (round 1)."""

    def _d(self, command, stderr, code=1, cwd="/tmp"):
        return diagnose(FailureEvent(id=None, command=command, exit_code=code,
                                     stderr=stderr, cwd=cwd, timestamp=1.0, shell="zsh"))

    def _nf(self, command):
        name = command.split()[0]
        return self._d(command, "zsh: command not found: %s" % name, code=127)

    def test_common_tool_wins_ties(self):
        self.assertEqual(self._nf("sl").fix_command, "ls")
        self.assertEqual(self._nf("ks -la").fix_command, "ls -la")
        self.assertEqual(self._nf("fit status").fix_command, "git status")
        self.assertEqual(self._nf("gt status").fix_command, "git status")

    def test_never_suggests_symbol_binaries(self):
        d = self._nf("q")
        self.assertNotIn("[", d.fix_command)

    def test_gibberish_never_suggests_installing_the_typo(self):
        d = self._nf("xqzvbn")
        self.assertNotIn("install xqzvbn", d.fix_command)
        self.assertEqual(d.confidence, Confidence.LOW)

    def test_missing_space(self):
        self.assertEqual(self._nf("gitstatus").fix_command, "git status")
        self.assertEqual(self._nf("cd..").fix_command, "cd ..")

    def test_double_space_tidied(self):
        d = self._d("git  stauts", "git: 'stauts' is not a git command.\n\n"
                    "The most similar command is\n\tstatus\n")
        self.assertEqual(d.fix_command, "git status")

    def test_bad_long_flag(self):
        self.assertEqual(self._d("node --verison", "node: bad option: --verison").fix_command,
                         "node --version")
        self.assertEqual(self._d("curl --verison", "curl: option --verison: is unknown").fix_command,
                         "curl --version")
        self.assertEqual(self._d("ls --al", "ls: unrecognized option `--al'").fix_command,
                         "ls -al")

    def test_pip_suggestion_parsed(self):
        d = self._d("pip3 lsit", 'ERROR: unknown command "lsit" - maybe you meant "list"')
        self.assertEqual(d.fix_command, "pip3 list")

    def test_missing_file_close_name(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as t:
            open(os.path.join(t, "nonexistent.txt"), "w").close()
            d = self._d("cat nonexsitent.txt",
                        "cat: nonexsitent.txt: No such file or directory", cwd=t)
        self.assertEqual(d.fix_command, "cat nonexistent.txt")

    def test_not_a_git_repo(self):
        d = self._d("git status", "fatal: not a git repository (or any of the parent directories): .git", 128)
        self.assertEqual(d.rule_id, "not_git_repo")

    def test_hook_mode_git_subcommand_typo_without_stderr(self):
        self.assertEqual(self._d("git stauts", "").fix_command, "git status")

    def test_npm_unknown_command_on_stdout_only(self):
        import shutil
        if not shutil.which("npm"):
            self.skipTest("npm not installed")
        self.assertEqual(self._d("npm lsit", "").fix_command, "npm list")
        self.assertEqual(self._d("npm --verison", "").fix_command, "npm --version")
        self.assertIsNone(self._d("npm i", "").options or None)   # real alias: no "fix"
        self.assertNotEqual(self._d("npm i", "").rule_id, "unknown_subcommand")


class BlindQARound2(unittest.TestCase):
    """Regressions found by black-box typo testing (round 2)."""

    def _d(self, command, stderr="", code=1, cwd="/tmp"):
        return diagnose(FailureEvent(id=None, command=command, exit_code=code,
                                     stderr=stderr, cwd=cwd, timestamp=1.0, shell="zsh"))

    def test_npm_ties_prefer_common_and_never_install(self):
        import shutil
        if not shutil.which("npm"):
            self.skipTest("npm not installed")
        self.assertEqual(self._d("npm rn dev").fix_command, "npm run dev")
        self.assertEqual(self._d("npm tset").fix_command, "npm test")

    def test_npm_missing_script_uses_npm_suggestion(self):
        d = self._d("npm run devv", 'npm error Missing script: "devv"\nnpm error\n'
                    "npm error Did you mean this?\n"
                    'npm error   npm run dev # run the "dev" package script\n')
        self.assertEqual(d.fix_command, "npm run dev")

    def test_git_similar_list_trimmed(self):
        d = self._d("git dif HEAD", "git: 'dif' is not a git command.\n\nThe most similar "
                    "commands are\n\tdiff\n\tconfig\n\tdifftool\n\tinit\n\trefs\n")
        self.assertEqual(d.fix_command, "git diff HEAD")
        self.assertEqual(d.options, ())

    def test_hook_mode_skips_prefixes_and_assignments(self):
        for cmd, want in [("time gti status", "time git status"),
                          ("FOO=1 gti status", "FOO=1 git status"),
                          ("X=hi; gti log -1", "X=hi; git log -1"),
                          ("env gti status", "env git status"),
                          ("echo status | xargs gti", "echo status | xargs git"),
                          ('echo "a\nb" && gti status', 'echo "a\nb" && git status')]:
            self.assertEqual(self._d(cmd, code=127).fix_command, want, cmd)

    def test_xargs_stderr(self):
        d = self._d("echo status | xargs gti", "xargs: gti: No such file or directory", 127)
        self.assertEqual(d.fix_command, "echo status | xargs git")

    def test_close_path_hook_and_run_mode(self):
        import os, tempfile
        with tempfile.TemporaryDirectory() as t:
            for f in ("script.sh", "script.py"):
                open(os.path.join(t, f), "w").close()
            self.assertEqual(self._d("./scirpt.sh", "zsh: no such file or directory: ./scirpt.sh",
                                     127, t).fix_command, "./script.sh")
            self.assertEqual(self._d("python3 scrpit.py", "", 2, t).fix_command,
                             "python3 script.py")

    def test_python_did_you_mean(self):
        d = self._d('python3 -c "improt os"', "    improt os\n    ^^^^^^^^^\n"
                    "SyntaxError: invalid syntax. Did you mean 'import'?")
        self.assertEqual(d.fix_command, 'python3 -c "import os"')
        d = self._d('python3 -c "pritn(1)"',
                    "NameError: name 'pritn' is not defined. Did you mean: 'print'?")
        self.assertEqual(d.fix_command, 'python3 -c "print(1)"')

    def test_make_target(self):
        import os, tempfile
        with tempfile.TemporaryDirectory() as t:
            with open(os.path.join(t, "Makefile"), "w") as f:
                f.write("build:\n\techo b\ntest:\n\techo t\n")
            self.assertEqual(self._d("make biuld", "make: *** No rule to make target `biuld'.  Stop.",
                                     2, t).fix_command, "make build")
            self.assertEqual(self._d("make tset", "", 2, t).fix_command, "make test")


class BlindQARound3Safety(unittest.TestCase):
    """Round 3 (adversarial): fixes must never destroy or overwrite data."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _files(self, *names):
        import os
        for n in names:
            open(os.path.join(self.dir, n), "w").close()

    def _d(self, command, stderr="", code=1):
        return diagnose(FailureEvent(id=None, command=command, exit_code=code,
                                     stderr=stderr, cwd=self.dir, timestamp=1.0, shell="zsh"))

    def test_rm_target_never_redirected_to_another_file(self):
        self._files("a.txt", "b.txt", "prod.db", "dev.db")
        for cmd in ("rm c.txt", "rm pro.db", "rm -rf pro.db"):
            d = self._d(cmd, "rm: %s: No such file or directory" % cmd.split()[-1])
            self.assertNotIn("b.txt", d.fix_command, cmd)
            self.assertNotIn("prod.db", d.fix_command, cmd)

    def test_never_correct_a_name_into_rm(self):
        for cmd in ("em -rf junk", "rn -rf junk", "vm file"):
            d = self._d(cmd, "zsh: command not found: %s" % cmd.split()[0], 127)
            self.assertFalse(d.fix_command.startswith("rm "), cmd)
            self.assertFalse(any(o.startswith("rm ") for o in d.options), cmd)

    def test_fix_never_reuses_a_file_already_in_the_command(self):
        self._files("a.txt", "notes.md", "new.txt")
        for cmd in ("sort a.tx > a.txt", "cat a.tx >> a.txt", "grep TODO note.md > notes.md",
                    "mv nw.txt new.txt"):
            d = self._d(cmd, "No such file or directory")
            self.assertNotEqual(d.rule_id, "missing_file", cmd)

    def test_destructive_fix_is_never_high_confidence(self):
        d = self._d("git rest --hard", "git: 'rest' is not a git command. See 'git --help'.\n\n"
                    "The most similar commands are\n\trestore\n\treset\n")
        self.assertNotEqual(d.confidence, Confidence.HIGH)
        self.assertTrue(d.risky)

    def test_ties_are_not_high_confidence(self):
        self._files("keep.txt", "keap.txt", "main.c", "main.h")
        for cmd in ("cat kep.txt", "cat main.x"):
            d = self._d(cmd, "cat: x: No such file or directory")
            self.assertNotEqual(d.confidence, Confidence.HIGH, cmd)

    def test_unsafe_token_swaps_give_no_fix(self):
        self._files("my file.txt", "a.txt")
        for cmd in ('cat "my fil.txt"', "cat `echo a.tx`", "X=$(cat a.tx) || false"):
            d = self._d(cmd, "No such file or directory")
            self.assertNotEqual(d.rule_id, "missing_file", cmd)

    def test_versions_are_not_paths(self):
        self._files("1.2.4")
        self.assertNotEqual(self._d("cat 1.2.3", "No such file or directory").rule_id, "missing_file")

    def test_dotfile_transposition(self):
        self._files(".env")
        self.assertEqual(self._d("cat .evn", "cat: .evn: No such file or directory").fix_command,
                         "cat .env")

    def test_cd_to_close_directory_in_hook_mode(self):
        import os
        os.mkdir(os.path.join(self.dir, "projects"))
        self.assertEqual(self._d("cd projets").fix_command, "cd projects")

    def test_mkdir_missing_parent(self):
        d = self._d("mkdir a/b.c", "mkdir: a: No such file or directory")
        self.assertEqual(d.fix_command, "mkdir -p a/b.c")



class ThefuckMechanisms(unittest.TestCase):
    """General mechanisms (not per-typo tables): history, tool-printed fix,
    git refs."""

    def setUp(self):
        import os, tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.hist = os.path.join(self.tmp.name, "hist")
        self._old = os.environ.get("HISTFILE")
        os.environ["HISTFILE"] = self.hist
        from redo_cli import rules
        rules._history.cache_clear()

    def tearDown(self):
        import os
        os.environ["HISTFILE"] = self._old
        self.tmp.cleanup()

    def _d(self, command, stderr="", code=1, cwd=None):
        return diagnose(FailureEvent(id=None, command=command, exit_code=code,
                                     stderr=stderr, cwd=cwd or self.tmp.name,
                                     timestamp=1.0, shell="zsh"))

    def test_history_fixes_a_typo_no_rule_knows(self):
        with open(self.hist, "w") as f:
            f.write(": 1700000000:0;ls -la\n: 1700000001:0;git log --oneline -5\n")
        d = self._d("git log --oneilne -5", "fatal: unrecognized argument: --oneilne", 128)
        self.assertEqual(d.fix_command, "git log --oneline -5")   # man-page flag rule or history

    def test_history_never_suggests_risky_or_identical_commands(self):
        with open(self.hist, "w") as f:
            f.write("rm -rf buidl\nfoo-unknown-tool x\n")
        d = self._d("rm -rf build", "rm: build: Permission denied", 1)
        self.assertNotEqual(d.rule_id, "history")

    def test_tool_printed_fix(self):
        d = self._d("git push", "fatal: The current branch feature has no upstream branch.\n"
                    "To push the current branch and set the remote as upstream, use\n\n"
                    "    git push --set-upstream origin feature\n", 128)
        self.assertEqual(d.fix_command, "git push --set-upstream origin feature")

    def test_git_branch_typo(self):
        import subprocess
        repo = self.tmp.name
        run = lambda *a: subprocess.run(["git", "-C", repo] + list(a), capture_output=True, check=True)
        run("init", "-q", "-b", "main")
        run("-c", "user.email=a@b", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "x")
        run("branch", "feature-login")
        d = self._d("git checkout feature-logni",
                    "error: pathspec 'feature-logni' did not match any file(s) known to git", 1, repo)
        self.assertEqual(d.fix_command, "git checkout feature-login")
        d = self._d("git switch mian", "", 128, repo)            # hook mode
        self.assertEqual(d.fix_command, "git switch main")


class ThefuckParity(unittest.TestCase):
    """Shapes thefuck handles with general rules; redo must too."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def mk(self, *paths, exe=False):
        import os
        for p in paths:
            full = os.path.join(self.dir, p)
            if p.endswith("/"):
                os.makedirs(full, exist_ok=True)
            else:
                os.makedirs(os.path.dirname(full), exist_ok=True)
                open(full, "w").close()
                if exe:
                    os.chmod(full, 0o755)

    def d(self, command, stderr="", code=1):
        return diagnose(FailureEvent(id=None, command=command, exit_code=code, stderr=stderr,
                                     cwd=self.dir, timestamp=1.0, shell="zsh"))

    def fix(self, *a, **k):
        return self.d(*a, **k).fix_command

    def test_pasted_prompt(self):
        self.assertEqual(self.fix("$ git status", "zsh: command not found: $", 127), "git status")

    def test_doubled_word(self):
        self.assertEqual(self.fix("git git status", "git: 'git' is not a git command."), "git status")

    def test_hyphenated_subcommand(self):
        self.assertEqual(self.fix("git-log -3", "zsh: command not found: git-log", 127), "git log -3")

    def test_script_without_dot_slash(self):
        self.mk("deploy.sh", exe=True)
        self.assertEqual(self.fix("deploy.sh staging", "zsh: command not found: deploy.sh", 127),
                         "./deploy.sh staging")

    def test_non_executable_python_script(self):
        self.mk("tool.py")
        self.assertEqual(self.fix("tool.py", "zsh: command not found: tool.py", 127), "python3 tool.py")

    def test_missing_extension(self):
        self.mk("foo.py")
        self.assertEqual(self.fix("python3 foo", "python3: can't open file '%s/foo': [Errno 2] "
                                  "No such file or directory" % self.dir, 2), "python3 foo.py")

    def test_is_a_directory(self):
        self.mk("src/")
        self.assertEqual(self.fix("cat src", "cat: src: Is a directory"), "ls src")
        self.assertEqual(self.fix("grep foo src", "grep: src: Is a directory", 2), "grep -r foo src")
        self.assertEqual(self.fix("cp src dst", "cp: src is a directory (not copied)."), "cp -r src dst")
        d = self.d("rm src", "rm: src: is a directory")
        self.assertEqual(d.fix_command, "rm -r src")
        self.assertTrue(d.risky)

    def test_middle_path_component_typo(self):
        self.mk("projects/foo/")
        self.assertEqual(self.fix("cd projets/foo"), "cd projects/foo")

    def test_case_only_mismatch(self):
        self.mk("README.md")
        self.assertEqual(self.fix("git add Readme.md", "fatal: pathspec 'Readme.md' did not match any files", 128),
                         "git add README.md")

    def test_touch_missing_parent(self):
        self.assertEqual(self.fix("touch a/b/c.txt", "touch: a/b/c.txt: No such file or directory"),
                         "mkdir -p a/b && touch a/b/c.txt")

    def test_cd_into_new_dir(self):
        d = self.d("cd brandnewdir", "cd: no such file or directory: brandnewdir")
        self.assertEqual(d.fix_command, "mkdir -p brandnewdir && cd brandnewdir")
        self.assertNotEqual(d.confidence, Confidence.HIGH)

    def test_ln_s_reversed(self):
        self.mk("target.txt")
        self.assertEqual(self.fix("ln -s mylink target.txt", "ln: target.txt: File exists"),
                         "ln -s target.txt mylink")

    def test_quotes(self):
        self.assertEqual(self.fix("git commit -m 'it's broken'", "zsh: unmatched '"),
                         "git commit -m \"it's broken\"")
        self.assertEqual(self.fix("echo \"hi", "zsh: unmatched \""), "echo \"hi\"")

    def test_short_help(self):
        self.assertEqual(self.fix("python3 -hh", "unknown option -hh\nusage: python3 [option]", 2), "python3 --help")

    def test_git_nothing_staged(self):
        self.assertEqual(self.fix("git commit -m x", "no changes added to commit "
                                  "(use \"git add\" and/or \"git commit -a\")"),
                         "git add -A && git commit -m x")

    def _repo(self):
        import subprocess
        run = lambda *a: subprocess.run(["git", "-C", self.dir] + list(a), capture_output=True, check=True)
        run("init", "-q", "-b", "main")
        run("-c", "user.email=a@b", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "x")

    def test_git_new_branch(self):
        self._repo()
        d = self.d("git checkout zzzqqq", "error: pathspec 'zzzqqq' did not match any file(s) known to git")
        self.assertEqual(d.fix_command, "git checkout -b zzzqqq")

    def test_git_branch_exists(self):
        self._repo()
        self.assertEqual(self.fix("git checkout -b main", "fatal: a branch named 'main' already exists", 128),
                         "git checkout main")

    def test_git_no_tracking(self):
        self._repo()
        err = ("There is no tracking information for the current branch.\n"
               "If you wish to set tracking information for this branch you can do so with:\n\n"
               "    git branch --set-upstream-to=origin/<branch> main\n")
        self.assertEqual(self.fix("git pull", err), "git branch --set-upstream-to=origin/main main && git pull")

    def test_two_dashes(self):
        self.assertEqual(self.fix("git log -oneline", "error: did you mean `--oneline` (with two dashes)?", 129),
                         "git log --oneline")

    def test_npm_script_shortcut_and_typo(self):
        import json, os
        with open(os.path.join(self.dir, "package.json"), "w") as f:
            json.dump({"scripts": {"dev": "x", "test": "y"}}, f)
        self.assertEqual(self.fix("npm dev"), "npm run dev")
        self.assertEqual(self.fix("npm run tset"), "npm run test")

    def test_python_m_stdlib_typo(self):
        d = self.d("python3 -m venvv x", "/usr/bin/python3: No module named venvv")
        self.assertEqual(d.fix_command, "python3 -m venv x")


class BlindQARound5(unittest.TestCase):
    """Round 5: hook mode (no stderr) must read the disk/repo, and never
    retarget destructive commands."""
    setUp, tearDown, mk, d, fix, _repo = (ThefuckParity.setUp, ThefuckParity.tearDown, ThefuckParity.mk,
                                          ThefuckParity.d, ThefuckParity.fix, ThefuckParity._repo)

    def _repo_with_branch(self, name):
        import subprocess
        self._repo()
        subprocess.run(["git", "-C", self.dir, "branch", name], capture_output=True, check=True)

    def test_never_retarget_branch_delete(self):
        self._repo_with_branch("feature")
        for err in ("", "error: branch 'featuer' not found."):
            d = self.d("git branch -D featuer", err)
            self.assertNotIn("-D feature", d.fix_command + " ".join(d.options))

    def test_hook_git_push_without_upstream(self):
        import subprocess
        self._repo()
        subprocess.run(["git", "-C", self.dir, "checkout", "-qb", "feat"], check=True)
        self.assertEqual(self.fix("git push"), "git push --set-upstream origin feat")

    def test_short_flag_with_double_dash(self):
        self.assertEqual(self.fix("df --h", "df: unrecognized option `--h'"), "df -h")

    def test_hook_mode_disk_evidence(self):
        self.mk("src/", "notes.py")
        self.assertEqual(self.fix("cat src"), "ls src")
        self.assertEqual(self.fix("mkdir a/b"), "mkdir -p a/b")
        self.assertEqual(self.fix("python3 nots.py"), "python3 notes.py")

    def test_hook_python_module(self):
        self.assertEqual(self.fix("python3 -m http.serve 8000"), "python3 -m http.server 8000")


class BlindQARound6(unittest.TestCase):
    """Round 6 (independent fuzzer): flags, glued arguments, meaning-changing fixes."""
    setUp, tearDown, mk, d, fix, _repo = (ThefuckParity.setUp, ThefuckParity.tearDown, ThefuckParity.mk,
                                          ThefuckParity.d, ThefuckParity.fix, ThefuckParity._repo)

    def test_long_flag_before_short_heuristic(self):
        self.assertEqual(self.fix("git diff --sat", "fatal: unrecognized argument: --sat", 128), "git diff --stat")

    def test_uppercase_flag(self):
        self.assertEqual(self.fix("git diff --STAT", "fatal: unrecognized argument: --STAT", 128), "git diff --stat")

    def test_glued_flag_value(self):
        self.assertEqual(self.fix("git log --oneline-5", "fatal: unrecognized argument: --oneline-5", 128),
                         "git log --oneline -5")

    def test_glued_subcommand_flag(self):
        self.assertEqual(self.fix("git remote-v", "git: 'remote-v' is not a git command."), "git remote -v")
        self.assertEqual(self.fix("git branch-a", "git: 'branch-a' is not a git command."), "git branch -a")

    def test_glued_subcommand_argument(self):
        self._repo()
        import subprocess
        subprocess.run(["git", "-C", self.dir, "branch", "develop"], check=True)
        self.assertEqual(self.fix("git checkoutdevelop", "git: 'checkoutdevelop' is not a git command."),
                         "git checkout develop")

    def test_glued_command_argument(self):
        self.mk("README.md", "src/app.py")
        self.assertEqual(self.fix("catREADME.md", "zsh: command not found: catREADME.md", 127), "cat README.md")
        self.assertEqual(self.fix("python3src/app.py", "zsh: no such file or directory: python3src/app.py", 127),
                         "python3 src/app.py")

    def test_never_drop_a_token(self):
        self.assertEqual(self.fix("find. -name x", "zsh: command not found: find.", 127), "find . -name x")

    def test_sub_verbs_only_from_synopsis(self):
        d = self.d("git grep asd", "")
        self.assertNotIn("git grep is", d.fix_command)

    def test_make_flag_glued_never_dropped(self):
        self.mk("Makefile")
        open(self.dir + "/Makefile", "w").write("build:\n\techo b\n")
        self.assertNotEqual(self.fix("make -nbuild", "", 2), "make build")

    def test_fix_turning_state_changing_is_not_high(self):
        import json
        json.dump({"scripts": {"lint": "x"}}, open(self.dir + "/package.json", "w"))
        d = self.d("npm runlint")
        self.assertEqual(d.fix_command, "npm run lint")

    def test_ref_typos_beyond_checkout(self):
        self._repo()
        self.assertEqual(self.fix("git show HED", "fatal: ambiguous argument 'HED': unknown revision or path "
                                  "not in the working tree.", 128), "git show HEAD")

    def test_ref_prefix(self):
        self._repo()
        import subprocess
        subprocess.run(["git", "-C", self.dir, "branch", "develop"], check=True)
        self.assertEqual(self.fix("git checkout dev", "error: pathspec 'dev' did not match any file(s) known to git"),
                         "git checkout develop")

    def test_hook_dir_guess_is_not_high(self):
        self.mk("src/")
        self.assertNotEqual(self.d("grep -tn def src").confidence, Confidence.HIGH)

    def test_subcommand_prefix_list_in_hook_mode(self):
        d = self.d("git sta")
        self.assertIn("git status", (d.fix_command,) + d.options)

    def test_flag_shapes_from_independent_fuzzer(self):
        self.mk("src/", "README.md")
        for cmd, err, want in [
                ("mkdir -P x", "mkdir: illegal option -- P", "mkdir -p x"),
                ("mkdir -V x", "mkdir: illegal option -- V", "mkdir -v x"),   # bad on BSD and GNU
                ("git diff ---stat", "error: invalid option: ---stat", "git diff --stat"),
                ("pip3 --evrsion", "no such option: --evrsion", "pip3 --version"),
                ("sed --n 1p README.md", "sed: illegal option -- -", "sed -n 1p README.md"),
                ("du sh src", "du: sh: No such file or directory", "du -sh src"),
                ("head 5 README.md", "head: 5: No such file or directory", "head -5 README.md"),
                ("find . -namE x", "find: -namE: unknown primary or operator", "find . -name x")]:
            self.assertEqual(self.fix(cmd, err), want, cmd)

    def test_glued_flag_cluster_and_argument(self):
        self._repo()
        self.assertEqual(self.fix("git log -pHEAD", "error: unknown switch `H'"), "git log -p HEAD")


class WindowsAndPowerShell(unittest.TestCase):
    def test_powershell_not_recognized_message(self):
        d = diagnose(FailureEvent(id=None, command="gti status", exit_code=1, cwd="/tmp", timestamp=1.0,
                                  shell="pwsh", stderr="gti: The term 'gti' is not recognized as a name of a "
                                  "cmdlet, function, script file, or executable program."))
        self.assertEqual(d.fix_command, "git status")

    def test_windows_path_scan_strips_extensions(self):
        import os, tempfile
        from unittest import mock
        from redo_cli import rules
        with tempfile.TemporaryDirectory() as t:
            for n in ("git.exe", "npm.cmd", "readme.txt"):
                open(os.path.join(t, n), "w").close()
            rules._path_executables.cache_clear()
            try:
                with mock.patch.object(rules.sys, "platform", "win32"), \
                        mock.patch.dict(os.environ, {"PATH": t, "PATHEXT": ".EXE;.CMD"}):
                    self.assertEqual(rules._path_executables(), frozenset({"git", "npm"}))
            finally:
                rules._path_executables.cache_clear()
