"""Deterministic failure matchers.

Each ``match_*`` takes a FailureEvent and returns a Diagnosis or None. Order
matters: more specific rules (npm EACCES, port-in-use) must come before the
generic ones (permission denied, command not found). ``RULES`` at the bottom
fixes that order; ``engine.diagnose`` walks it first-match-wins.

Rules read three signals: the command string, the exit code, and stderr. They
are written to degrade gracefully when stderr is absent (Stage 2 hook mode),
dropping confidence rather than refusing to answer.
"""

from __future__ import annotations

import re
from typing import Optional

from .models import Confidence, Diagnosis, FailureEvent

HIGH = Confidence.HIGH
MEDIUM = Confidence.MEDIUM
LOW = Confidence.LOW


# import-name -> pip package name, for the cases where they differ
PYPI_NAMES = {
    "cv2": "opencv-python",
    "PIL": "pillow",
    "yaml": "pyyaml",
    "sklearn": "scikit-learn",
    "bs4": "beautifulsoup4",
    "dotenv": "python-dotenv",
    "jwt": "pyjwt",
    "dateutil": "python-dateutil",
    "Crypto": "pycryptodome",
    "OpenSSL": "pyopenssl",
    "serial": "pyserial",
}

# one-keystroke slips we can correct with high confidence
COMMON_TYPOS = {
    "gti": "git",
    "pyhton": "python",
    "pythn": "python",
    "npmm": "npm",
    "yarnn": "yarn",
    "claer": "clear",
    "clera": "clear",
    "grpe": "grep",
    "gerp": "grep",
    "docekr": "docker",
    "kubclt": "kubectl",
}


def _has(text: str, *needles: str) -> bool:
    low = text.lower()
    return any(n.lower() in low for n in needles)


def _make(rule_id, title, summary, fix, confidence, explanation, lesson):
    return Diagnosis(
        rule_id=rule_id,
        title=title,
        summary=summary,
        fix_command=fix,
        confidence=confidence,
        explanation=explanation,
        lesson=lesson,
        matched=True,
    )


def match_git_push_rejected(e: FailureEvent) -> Optional[Diagnosis]:
    if not re.search(r"\bgit\s+push\b", e.command):
        return None
    keywords = ("rejected", "non-fast-forward", "fetch first",
                "tip of your current branch is behind", "behind its remote")
    if e.stderr.strip() and not _has(e.stderr, *keywords):
        return None  # a git push failing for some *other* reason (auth, etc.)
    confidence = HIGH if e.stderr.strip() else MEDIUM
    return _make(
        "git_push_rejected",
        "git push rejected",
        "remote rejected your push because your branch is behind origin",
        "git pull --rebase && git push",
        confidence,
        ("Your push was rejected because origin contains commits your local "
         "branch does not have. Git refuses a non-fast-forward push so it "
         "cannot silently overwrite history that exists on the remote."),
        ("Git requires your branch to be up to date with the remote before "
         "pushing. `git pull --rebase` replays your local commits on top of "
         "the remote ones, giving a linear history you can push cleanly."),
    )


def match_merge_conflict(e: FailureEvent) -> Optional[Diagnosis]:
    if not _has(e.stderr, "merge conflict", "automatic merge failed",
                "fix conflicts and then commit", "conflict (content)"):
        return None
    return _make(
        "merge_conflict",
        "merge conflict",
        "the merge produced conflicts that need manual resolution",
        ("# 1. open the conflicted files and resolve the <<<<<<< markers\n"
         "# 2. stage and commit the resolution:\n"
         "git add . && git commit\n"
         "# ...or bail out entirely:\n"
         "git merge --abort"),
        MEDIUM,
        ("Git merged what it could but found regions where both sides edited "
         "the same lines. Those are wrapped in <<<<<<< / ======= / >>>>>>> "
         "markers and must be resolved by hand."),
        ("A conflict means two branches changed the same place differently, so "
         "Git can't pick a winner. Resolve the marked regions, delete the "
         "markers, then add + commit to record the merge."),
    )


def match_port_in_use(e: FailureEvent) -> Optional[Diagnosis]:
    if not _has(e.stderr, "eaddrinuse", "address already in use"):
        return None
    m = re.search(r":(\d{2,5})\b", e.stderr)
    port = m.group(1) if m else "<port>"
    return _make(
        "port_in_use",
        "port already in use",
        "port %s is already bound by another process" % port,
        "lsof -ti tcp:%s | xargs kill" % port,
        HIGH,
        ("Another process is already listening on port %s, so this one can't "
         "bind to it — usually a previous dev server that didn't shut down, or "
         "a second copy of the app already running." % port),
        ("Only one process can listen on a TCP port at a time. `lsof -ti "
         "tcp:%s` prints the PID holding the port; piping it to `kill` frees "
         "the port so your server can start." % port),
    )


def match_python_import(e: FailureEvent) -> Optional[Diagnosis]:
    m = re.search(r"No module named ['\"]([\w\.]+)['\"]", e.stderr)
    if not m:
        return None
    top = m.group(1).split(".")[0]
    pkg = PYPI_NAMES.get(top, top)
    return _make(
        "python_import_error",
        "python import error",
        "Python could not import the module '%s'" % top,
        "pip install %s" % pkg,
        HIGH,
        ("The interpreter searched every directory on sys.path and found no "
         "package named '%s'. Either it isn't installed in the active "
         "environment, or you're running a different Python than the one you "
         "installed it into." % top),
        ("`ModuleNotFoundError` is about installation and environment, not your "
         "code. Install into the SAME interpreter you run with — "
         "`python -m pip install ...` guarantees that."),
    )


def match_node_module_missing(e: FailureEvent) -> Optional[Diagnosis]:
    m = re.search(r"Cannot find module ['\"]([^'\"]+)['\"]", e.stderr)
    if not m:
        return None
    mod = m.group(1)
    if mod.startswith(".") or mod.startswith("/"):
        return _make(
            "node_local_module",
            "missing module",
            "Node could not resolve the local file '%s'" % mod,
            "# check the import path, file extension, and filename casing",
            MEDIUM,
            ("'%s' is a relative path, so Node is looking for a file on disk, "
             "not an npm package. The path, the extension, or the letter-casing "
             "of the filename is off." % mod),
            ("Relative imports map directly to files. macOS is case-insensitive "
             "but Linux/CI is not, so `./Utils` vs `./utils` is a classic "
             "'works on my machine' module error."),
        )
    pkg = "/".join(mod.split("/")[:2]) if mod.startswith("@") else mod.split("/")[0]
    return _make(
        "node_module_missing",
        "missing package",
        "the package '%s' is not installed" % pkg,
        "npm install %s" % pkg,
        HIGH,
        ("Node resolved the import '%s' to an npm package, but it isn't present "
         "in node_modules — never installed, or node_modules is stale relative "
         "to package.json." % mod),
        ("`Cannot find module` for a bare name means a missing dependency. "
         "Install it (and commit package.json) so teammates and CI get it too."),
    )


def match_missing_virtualenv(e: FailureEvent) -> Optional[Diagnosis]:
    if _has(e.stderr, "externally-managed-environment"):
        summary = "pip refused to install into the system Python (PEP 668)"
    elif "activate" in e.command and _has(e.stderr, "no such file or directory"):
        summary = "the virtualenv you tried to activate does not exist yet"
    else:
        return None
    return _make(
        "missing_virtualenv",
        "missing virtualenv",
        summary,
        "python3 -m venv .venv && source .venv/bin/activate",
        HIGH,
        ("Your system Python is protected (or no environment is active), so "
         "packages should go into a project-local virtualenv instead of the "
         "global interpreter."),
        ("A virtualenv is an isolated copy of Python per project. Create it "
         "with `python3 -m venv .venv`, activate it, and `pip install` then "
         "writes into that folder instead of your system Python."),
    )


def match_npm_install(e: FailureEvent) -> Optional[Diagnosis]:
    if not re.search(r"\bnpm\s+(install|i|ci)\b", e.command):
        return None
    if _has(e.stderr, "eresolve"):
        fix = "npm install --legacy-peer-deps"
        summary = "npm could not satisfy conflicting peer dependencies"
        conf = MEDIUM
    elif _has(e.stderr, "eacces"):
        fix = "# fix npm's directory permissions (use nvm or a user prefix) — do NOT use sudo npm"
        summary = "npm hit a permission error writing into its install path"
        conf = MEDIUM
    elif _has(e.stderr, "enoent") and "package.json" in e.stderr.lower():
        fix = "# cd into the directory that actually contains package.json"
        summary = "no package.json was found in this directory"
        conf = MEDIUM
    elif _has(e.stderr, "etimedout", "enotfound", "network", "registry error"):
        fix = "npm install   # check network / proxy / registry, then try again"
        summary = "npm could not reach the registry"
        conf = LOW
    else:
        fix = "npm install --verbose   # read the first 'npm ERR!' line"
        summary = "npm install failed"
        conf = LOW
    return _make(
        "npm_install_failure",
        "npm install failure",
        summary,
        fix,
        conf,
        ("npm install fails for several distinct reasons — dependency "
         "conflicts, permissions, wrong directory, or network. The first "
         "`npm ERR! code ...` line names which one you hit."),
        ("Always read the first `npm ERR!` line: the `code` (ERESOLVE, EACCES, "
         "ENOENT, ...) is the category. ERESOLVE is a peer-dependency clash; "
         "--legacy-peer-deps tells npm to install anyway."),
    )


def match_brew_missing(e: FailureEvent) -> Optional[Diagnosis]:
    if not _has(e.stderr, "no available formula", "no formulae found"):
        return None
    m = (re.search(r'name[s]?[ "]+([\w.+\-/]+)', e.stderr, re.I)
         or re.search(r"brew\s+install\s+(\S+)", e.command))
    name = m.group(1).strip('"') if m else "<formula>"
    return _make(
        "brew_package_missing",
        "brew package missing",
        "Homebrew has no formula named '%s'" % name,
        "brew search %s" % name,
        MEDIUM,
        ("Homebrew searched its taps and found nothing called '%s'. The name is "
         "misspelled, lives in a tap you haven't added, or is a cask rather "
         "than a formula." % name),
        ("Formula names don't always match the project name. `brew search` does "
         "a fuzzy lookup across formulae and casks so you can find the real "
         "name (or learn you need `brew install --cask`)."),
    )


def match_command_not_found(e: FailureEvent) -> Optional[Diagnosis]:
    m = (re.search(r"command not found: (\S+)", e.stderr)
         or re.search(r"(\S+): command not found", e.stderr)
         or re.search(r"(\S+): not found", e.stderr))
    if m:
        name = m.group(1)
    elif e.exit_code == 127 and e.command.split():
        name = e.command.split()[0]
    else:
        return None

    if name in COMMON_TYPOS:
        correct = COMMON_TYPOS[name]
        fixed = e.command.replace(name, correct, 1) if e.command else correct
        return _make(
            "command_typo",
            "command not found",
            "'%s' isn't a command — did you mean '%s'?" % (name, correct),
            fixed,
            HIGH,
            ("The shell searched every directory in $PATH for '%s' and found "
             "nothing. It's a one-character slip for '%s'." % (name, correct)),
            ("'command not found' means the shell couldn't locate the binary on "
             "$PATH. One keystroke off a real tool is just a typo; otherwise "
             "the tool isn't installed or isn't on PATH."),
        )

    if name == "brew":
        return _make(
            "command_not_found",
            "command not found",
            "Homebrew (`brew`) is not installed or not on your PATH",
            '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"',
            MEDIUM,
            ("The shell can't find `brew` on $PATH — either Homebrew isn't "
             "installed, or its shellenv line hasn't been added to your profile "
             "(on Apple Silicon that's /opt/homebrew/bin)."),
            ("A program runs only if its binary sits in a $PATH directory. "
             "Installers usually append that line to your shell profile; if "
             "they didn't, the command 'isn't found' even though it's on disk."),
        )

    return _make(
        "command_not_found",
        "command not found",
        "'%s' was not found on your PATH" % name,
        "# install '%s' (e.g. brew install %s) or check it's on $PATH" % (name, name),
        MEDIUM,
        ("The shell searched every directory in $PATH and found no executable "
         "named '%s' — not installed, installed somewhere off PATH, or "
         "misspelled." % name),
        ("$PATH is the list of directories your shell searches for commands. "
         "'command not found' means the name matched nothing there."),
    )


def match_permission_denied(e: FailureEvent) -> Optional[Diagnosis]:
    if not (e.exit_code == 126 or _has(e.stderr, "permission denied", "eacces")):
        return None
    target = None
    sm = re.search(r"permission denied: (\S+)", e.stderr, re.I)
    if sm:
        target = sm.group(1)
    else:
        cm = re.search(r"(\.?/\S+|\S+\.(?:sh|py|rb|js|bin))", e.command)
        if cm:
            target = cm.group(1)
    if target and (target.startswith("./") or
                   target.endswith((".sh", ".py", ".rb", ".js", ".bin"))):
        return _make(
            "permission_denied_exec",
            "permission denied",
            "'%s' is not marked executable" % target,
            "chmod +x %s && %s" % (target, e.command.strip()),
            HIGH,
            ("The file '%s' exists but doesn't have the execute bit set, so the "
             "kernel refuses to run it directly." % target),
            ("Unix files carry read/write/execute bits. A freshly created or "
             "freshly cloned script often lacks +x; `chmod +x` grants execute "
             "so you can run it."),
        )
    return _make(
        "permission_denied",
        "permission denied",
        "the operation was blocked by file permissions",
        "# inspect with `ls -l`; fix the owner/mode rather than reaching for sudo",
        MEDIUM,
        ("Something tried to read, write, or execute a path your user isn't "
         "allowed to touch. The safe fix is to correct the file's owner or "
         "mode, not to escalate the whole command with sudo."),
        ("Permission errors are about a file's owner and mode versus your user. "
         "Reaching for sudo hides the problem and can leave root-owned files "
         "that break later commands."),
    )


def fallback(e: FailureEvent) -> Diagnosis:
    return Diagnosis(
        rule_id="",
        title="uncategorized",
        summary="Failure detected, but no exact match was found.",
        fix_command="redo explain",
        confidence=LOW,
        explanation=(
            "No deterministic rule matched this failure.\n\n"
            "Likely causes:\n"
            "  - missing dependency\n"
            "  - environment mismatch\n"
            "  - incorrect command usage\n\n"
            "The command exited with code %d. Re-read the last few lines of its "
            "output and find the first line that names a file, a package, or an "
            "argument." % e.exit_code
        ),
        lesson=(
            "When nothing matches, work backwards from the exact error text: "
            "copy the first error line, decide whether it's about a missing "
            "file, a missing dependency, or a wrong argument, and fix only that "
            "one thing before re-running."
        ),
        matched=False,
    )


# Order is the contract: specific before generic.
RULES = [
    match_git_push_rejected,
    match_merge_conflict,
    match_port_in_use,
    match_python_import,
    match_node_module_missing,
    match_missing_virtualenv,
    match_npm_install,
    match_brew_missing,
    match_command_not_found,
    match_permission_denied,
]
