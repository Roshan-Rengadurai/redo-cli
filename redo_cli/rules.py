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

import dataclasses
import difflib
import functools
import os
import json
import re
import shlex
import shutil
import sys
import subprocess
import time
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

@functools.lru_cache(maxsize=1)
def _path_executables() -> frozenset:
    """Every name in a $PATH directory -- ground truth for typo correction
    instead of a hand-maintained list of known slips. Plain listdir (~1 ms);
    checking each entry is executable cost ~30 ms per lookup, so only the
    few final candidates are verified, via _is_exec."""
    names = set()
    for d in os.environ.get("PATH", "").split(os.pathsep):
        try:
            names.update(os.listdir(d))
        except OSError:
            continue
    if sys.platform == "win32":
        # `git.exe` is typed as `git`; Windows names are case-insensitive
        exts = {e.lower() for e in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if e}
        names = {os.path.splitext(n)[0].lower() for n in names if os.path.splitext(n)[1].lower() in exts}
    return frozenset(names)


@functools.lru_cache(maxsize=64)
def _is_exec(name: str) -> bool:
    return shutil.which(name) is not None


def _edit_distance(a: str, b: str, limit: int = 99) -> int:
    """Damerau-Levenshtein distance: insert/delete/substitute/adjacent-swap,
    each cost 1. difflib's ratio() scores a transposition (by far the most
    common typo, e.g. 'gti') as two changes, which ties it with unrelated
    words that merely share letters -- this doesn't.

    Stops early once every cell in a row exceeds `limit` (returns limit+1):
    scanning ~1500 executables per lookup made `redo stats` take seconds."""
    la, lb = len(a), len(b)
    prev2 = None
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        if min(cur) > limit:
            return limit + 1
        prev2, prev = prev, cur
    return prev[lb]


def _close_names(base: str, names, limit: Optional[int] = None) -> list:
    """Names within a typo or two of `base` (transpositions count once);
    all of them if they tie. Default budget is tight, for big name sets."""
    if limit is None:
        limit = 1 if len(base) <= 5 else 2
    scored = [(_edit_distance(base, n, limit), n) for n in names
              if abs(len(n) - len(base)) <= limit]
    scored = [t for t in scored if t[0] <= limit]
    if not scored:
        return []
    best = min(d for d, _ in scored)
    return sorted(n for d, n in scored if d == best)


# Tools people actually type, most-used first. Ranks ties toward what was
# almost certainly meant ('fit' -> git, not fmt) and lets us recognise a
# typo of a tool that isn't installed yet ('dokcer' -> docker).
COMMON_TOOLS = (
    "git ls cd cat grep python3 python pip3 pip node npm npx yarn pnpm brew "
    "docker kubectl make curl wget ssh scp vim vi nano code tar echo which "
    "clear less man sudo cp mv rm mkdir touch find sed awk head tail open "
    "go cargo rustc java gcc ps top du df wc stat id date sort uniq diff "
    "tee xargs env ping whoami uname pwd"
).split()
_RANK = {t: i for i, t in enumerate(COMMON_TOOLS)}


# Commands that delete, move, overwrite or kill. A typo is never "corrected"
# into one of these, and no fix involving them is ever high confidence or
# auto-run: being wrong costs data, being cautious costs a keystroke.
DESTRUCTIVE = {"rm", "rmdir", "unlink", "shred", "dd", "mkfs", "truncate",
               "mv", "kill", "killall", "pkill", "chmod", "chown", "sudo",
               "doas", "halt", "reboot", "shutdown", "diskutil"}
_RISKY_ARGS = re.compile(
    r"\bgit\s+(?:reset\s.*--hard|clean\b|branch\s+-[dD]\b|checkout\s+(?:\.|--)"
    r"|stash\s+(?:drop|clear)\b|push\b.*(?:--force|\s-f\b)|restore\b|rebase\b)"
    r"|\b(?:npm|yarn|pnpm|pip3?|brew)\s+(?:uninstall|remove|rm|un|publish)\b")
_REDIRECT = re.compile(r"(?<![0-9&<])>{1,2}(?!&)\s*(?!/dev/null)\S")


_STATEFUL = re.compile(
    r"\bgit\s+(?:push|commit|merge|pull|rebase|tag|am|apply|stash|reset|clean|branch\s+-)"
    r"|\b(?:npm|pnpm|yarn|pip3?|brew|cargo|gem|go)\s+(?:i|install|add|publish|upgrade|update|get|"
    r"link|unlink|uninstall|remove|rm|un)\b"
    r"|\b(?:curl|wget|ssh|scp|rsync)\b")


def changes_state(cmd: str) -> bool:
    """Writes somewhere: the network, a package set, git history, files."""
    return bool(_STATEFUL.search(cmd)) or is_risky(cmd)


def is_risky(cmd: str) -> bool:
    """Would running `cmd` delete, move, overwrite or kill something?"""
    return bool(_REDIRECT.search(cmd) or _RISKY_ARGS.search(cmd)
                or any(w in DESTRUCTIVE for w in _command_words(cmd)))


_QWERTY = ["qwertyuiop", "asdfghjkl", "zxcvbnm"]
_KEYPOS = {ch: (r, c) for r, row in enumerate(_QWERTY) for c, ch in enumerate(row)}


def _typo_cost(typed: str, cand: str) -> int:
    """Tie-breaker for one-edit typos, by how people actually slip: swapped
    letters (`ud`->du) and doubled/dropped letters (`wcc`->wc) are likelier
    than hitting a key across the keyboard (`ud`->cd)."""
    if len(typed) == len(cand) + 1 and any(
            typed[i] == typed[i + 1] and typed[:i] + typed[i + 1:] == cand
            for i in range(len(cand))):
        return 0                                   # doubled a letter (`wcc` -> wc)
    if len(typed) != len(cand):
        return 1                                   # insertion / deletion
    diff = [i for i, (a, b) in enumerate(zip(typed, cand)) if a != b]
    if len(diff) == 2 and diff[1] == diff[0] + 1 and sorted(typed) == sorted(cand):
        return 0                                   # adjacent transposition
    if len(diff) == 1:
        a, b = _KEYPOS.get(typed[diff[0]]), _KEYPOS.get(cand[diff[0]])
        if a and b and abs(a[0] - b[0]) <= 1 and abs(a[1] - b[1]) <= 1:
            return 1                               # neighbouring key
    return 2


@functools.lru_cache(maxsize=256)
def _closest_executables(name: str, max_distance: Optional[int] = None):
    """The executable(s) closest to `name`, from $PATH plus COMMON_TOOLS.

    Ranked by edit distance, then how plausible the slip is (_typo_cost),
    then installed, then COMMON_TOOLS popularity. Returns one name when it
    clearly wins, the tied names (<=4) when it doesn't, else nothing.
    Third value: True only for a clean single winner (safe to call HIGH)."""
    name = name.lower()
    if len(name) < 2:
        return [], 0, False
    if max_distance is None:
        max_distance = 1 if len(name) <= 5 else 2
    installed = _path_executables()
    scored = []
    for cand in installed | frozenset(COMMON_TOOLS):
        if (cand == name or not cand[:1].isalnum()      # never suggest `[`
                or cand in DESTRUCTIVE                    # `em -rf` is not `rm -rf`
                or abs(len(cand) - len(name)) > max_distance):
            continue
        dist = _edit_distance(name, cand, max_distance)
        if dist <= max_distance:
            # a popular tool earns back one unit of slip cost: `np` is npm as
            # likely as the obscure `pn`, so they tie and both get shown
            cost = (_typo_cost(name, cand) if dist == 1 else 2) - (cand in _RANK)
            scored.append((dist, cost, cand))
    if not scored:
        return [], 0, False
    best = min(scored)[:2]
    tied = [c for d, k, c in scored if (d, k) == best]
    tied = [c for c in tied if c in installed and _is_exec(c)] or tied
    tied.sort(key=lambda c: (_RANK.get(c, len(_RANK)), c))
    common = [c for c in tied if c in _RANK]
    if len(tied) > 4:                   # 'sat' ties say/sar/scat/...: a guess at best
        return (common[:4], best[0], False) if common else ([], 0, False)
    # "clear" = nothing else within one unit of slip cost (`sat`: cat vs stat)
    rivals = sum(1 for d, k, _ in scored if d == best[0] and k <= best[1] + 1)
    return tied, best[0], rivals == 1


def _swap(cmd: str, old: str, new: str) -> str:
    """Replace the whole shell word `old` once."""
    return re.sub(r"(?<!\S)%s(?!\S)" % re.escape(old), new.replace("\\", r"\\"), cmd, count=1)


def _tidy(cmd: str) -> str:
    """Collapse runs of spaces in a suggested fix, unless quoting makes
    whitespace significant."""
    if "'" in cmd or '"' in cmd or "\n" in cmd:
        return cmd
    return re.sub(r"[ \t]+", " ", cmd.strip())


def _has(text: str, *needles: str) -> bool:
    low = text.lower()
    return any(n.lower() in low for n in needles)


def _make(rule_id, title, summary, fix, confidence, explanation, lesson, options=()):
    return Diagnosis(rule_id, title, summary, fix, confidence, explanation,
                     lesson, matched=True, options=tuple(options))


def match_git_push_rejected(e: FailureEvent) -> Optional[Diagnosis]:
    if not re.search(r"\bgit\s+push\b", e.command):
        return None
    keywords = ("rejected", "non-fast-forward", "fetch first",
                "tip of your current branch is behind", "behind its remote")
    if e.stderr.strip() and not _has(e.stderr, *keywords):
        return None  # a git push failing for some *other* reason (auth, etc.)
    if not e.stderr.strip():                 # hook mode: ask the repo what's wrong
        return _git_push_hook(e)
    confidence = HIGH
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


def _git_push_hook(e: FailureEvent) -> Optional[Diagnosis]:
    w = e.command.split()
    if len(w) >= 4 and not w[3].startswith("-"):            # git push origin <ref>
        return match_git_ref_typo(dataclasses.replace(
            e, command=" ".join(w[:4]), stderr="error: src refspec %s does not match any" % w[3]))
    if not _git(e.cwd, "rev-parse", "--abbrev-ref", "@{u}"):
        br = _git(e.cwd, "rev-parse", "--abbrev-ref", "HEAD")
        if br and br != "HEAD":
            return _make("git_no_upstream", "no upstream", "'%s' has no upstream branch yet" % br,
                         "git push --set-upstream origin %s" % br, MEDIUM,
                         "The branch was never pushed, so git doesn't know where to push it.",
                         "Push once with --set-upstream (-u); plain `git push` works after that.")
        return None
    if _git(e.cwd, "rev-list", "--count", "HEAD..@{u}") not in ("", "0"):
        return _make("git_push_rejected", "git push rejected", "your branch is behind its upstream",
                     "git pull --rebase && git push", MEDIUM,
                     "The remote has commits you don't, so a plain push is refused.",
                     "Rebase onto the remote, then push.")
    return None


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


def _port_busy(cmd: str) -> Optional[str]:
    import socket
    for p in re.findall(r"(?:\s|:|=)(\d{4,5})\b", cmd):
        if 1024 <= int(p) <= 65535:
            with socket.socket() as sk:
                sk.settimeout(0.2)
                if sk.connect_ex(("127.0.0.1", int(p))) == 0:
                    return p
    return None


def match_port_in_use(e: FailureEvent) -> Optional[Diagnosis]:
    if not (_has(e.stderr, "eaddrinuse", "address already in use")
            or (not e.stderr.strip() and _port_busy(e.command))):
        return None
    m = (re.search(r":(\d{2,5})\b", e.stderr)
         or re.search(r"(?:\s|:|=)(\d{2,5})\b", e.command))
    port = m.group(1) if m else "<port>"
    return _make(
        "port_in_use",
        "port already in use",
        "port %s is already bound by another process" % port,
        "lsof -ti tcp:%s | xargs kill" % port,
        HIGH,
        ("Another process is already listening on port %s, so this one can't "
         "bind to it: usually a previous dev server that didn't shut down, or "
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
         "code. Install into the SAME interpreter you run with: "
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
         "in node_modules: never installed, or node_modules is stale relative "
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
        fix = "# fix npm's directory permissions (use nvm or a user prefix, not sudo npm)"
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
        ("npm install fails for several distinct reasons: dependency "
         "conflicts, permissions, wrong directory, or network. The first "
         "`npm ERR! code ...` line names which one you hit."),
        ("Always read the first `npm ERR!` line: the `code` (ERESOLVE, EACCES, "
         "ENOENT, ...) is the category. ERESOLVE is a peer-dependency clash; "
         "--legacy-peer-deps tells npm to install anyway."),
    )


def match_brew_missing(e: FailureEvent) -> Optional[Diagnosis]:
    if not e.command.startswith("brew ") or not _has(e.stderr, "no available formula", "no formulae found"):
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


# A lot of modern CLIs already run their own edit-distance match against
# real subcommands and print the result -- reuse that instead of guessing or
# hardcoding a subcommand list per tool. Each entry: (bad-token pattern,
# suggestions pattern). Candidates are the words of the suggestions group:
# "\tbranch\n\tbisect", "build", or brew's "instal or install".
_SUGGESTION_FORMATS = [
    # git: "git: 'brance' is not a git command.\n\nThe most similar command is\n\tbranch"
    (re.compile(r"^git: '([^']+)' is not a git command\.", re.M),
     re.compile(r"The most similar command(?:s)? (?:is|are)\n((?:\t\S.*\n?)+)")),
    # npm: 'npm error Missing script: "devv"\n...Did you mean this?\nnpm error   npm run dev # ...'
    (re.compile(r'Missing script: "([^"]+)"'),
     re.compile(r"Did you mean this\?\n(?:npm error|npm ERR!)?\s+npm run (\S+)")),
    # pip: 'ERROR: unknown command "lsit" - maybe you meant "list"'
    (re.compile(r'unknown command "([^"]+)" - maybe'),
     re.compile(r'maybe you meant "([^"]+)"')),
    # cobra-based CLIs (gh, kubectl, hugo, ...): 'unknown command "lst" for "gh pr"\n\nDid you mean this?\n\tlist'
    (re.compile(r'unknown command "([^"]+)" for '),
     re.compile(r"Did you mean this\?\n((?:\t\S.*\n?)+)")),
    # cargo: "error: no such command: `bulid`\n\nhelp: a command with a similar name exists: `build`"
    (re.compile(r"error: no such (?:command|subcommand): `([^`]+)`"),
     re.compile(r"a command with a similar name exists: `([^`]+)`")),
    # brew: "Error: Invalid usage: Unknown command: brew upgrad\nDid you mean upgrade?"
    (re.compile(r"Unknown command: \S+ (\S+)"),
     re.compile(r"Did you mean ([^?]+)\?")),
]


def match_unknown_subcommand(e: FailureEvent) -> Optional[Diagnosis]:
    """Several CLIs (git, gh, kubectl, cargo, brew, ...) already compute and
    print their own 'did you mean' suggestion for an unknown subcommand --
    parse whichever format matches instead of maintaining a subcommand list
    per tool ourselves."""
    for bad_re, suggest_re in _SUGGESTION_FORMATS:
        bad = bad_re.search(e.stderr)
        suggest = suggest_re.search(e.stderr) if bad else None
        if not suggest:
            continue
        candidates = [w for w in re.findall(r"[^\s,]+", suggest.group(1)) if w != "or"]
        if not candidates:
            continue
        bad_word = bad.group(1)
        tool0 = (e.command.split() or [""])[0]
        split = _split_word(bad_word, _tool_subcommands(tool0), e.cwd, tool0) if tool0 in _LIST_CMDS else None
        if split:                                   # `git remote-v` -> remote -v, not remote-fd
            return _make("missing_space", "missing space", "'%s' is '%s' with a space missing" % (bad_word, split),
                         _swap(e.command, bad_word, split), HIGH,
                         "The subcommand and its argument got glued together.",
                         "The shell splits words on spaces only.")
        named = len(candidates)
        # git lists everything "similar" (`dif` -> diff, config, init, ...);
        # keep only those within one edit of the nearest.
        dists = {c: _edit_distance(bad_word, c) for c in candidates}
        candidates = [c for c in candidates if dists[c] <= min(dists.values()) + 1]
        options = tuple(_tidy(e.command.replace(bad_word, cand, 1)) for cand in candidates)
        tool = e.command.split()[0] if e.command.split() else "the tool"
        return _make(
            "unknown_subcommand", "unknown subcommand",
            ("'%s' isn't a %s subcommand: did you mean '%s'?" % (bad_word, tool, candidates[0])
             if len(options) == 1 else "'%s' isn't a %s subcommand: it suggests these instead" % (bad_word, tool)),
            options[0], HIGH if named == 1 else MEDIUM,
            "%s parsed '%s' as a subcommand and didn't recognize it; it computed "
            "the closest real one(s) itself." % (tool, bad_word),
            "Many CLIs (git, gh, kubectl, cargo, ...) do their own 'did you mean' "
            "matching on unknown subcommands, so redo reuses it.",
            options=options if len(options) > 1 else (),
        )
    return None


# tool name -> a read-only, side-effect-free way to list its real
# subcommands (the same introspection each tool's own shell completion uses).
_LIST_CMDS = {
    "git": ["git", "--list-cmds=main,others,alias,nohelpers"],
    "brew": ["brew", "commands", "--quiet"],
    # npm's shell-completion list: every command *and* alias (`i`, `ls`, ...)
    "npm": ["npm", "completion", "--", "npm", ""],
    "pip3": ["pip3", "help"],
    "pip": ["pip", "help"],
}
# Most-typed subcommands first; breaks edit-distance ties.
_COMMON_SUBCMDS = {
    "npm": ["run", "test", "install", "start", "list", "ls", "ci", "exec",
            "init", "update", "outdated", "audit", "uninstall", "publish"],
    "git": ["status", "commit", "push", "pull", "log", "diff", "add",
            "checkout", "branch", "switch", "merge", "rebase", "stash",
            "fetch", "show", "reset", "restore", "remote", "clone", "tag"],
}
# Tools whose list above includes every alias, so "not in the list" really
# means "not a command" -- safe to act on even with no stderr (hook mode).
_COMPLETE_LISTS = {"git", "npm", "pip", "pip3", "brew"}
# HOMEBREW_NO_AUTO_UPDATE matters: without it, invoking any `brew` subcommand
# can trigger a slow, network-dependent auto-update first.
_TOOL_ENV = {
    "brew": {"HOMEBREW_NO_AUTO_UPDATE": "1", "HOMEBREW_NO_ENV_HINTS": "1"},
    "npm": {"COMP_CWORD": "1", "COMP_LINE": "npm ", "COMP_POINT": "4"},
}


_SUBCMD_TTL = 24 * 3600


def _disk_cached(key: str, compute, ttl: int = _SUBCMD_TTL) -> str:
    """Tool introspection (subcommand lists, man pages, --help) costs
    50-700 ms a call and rarely changes: keep it in $REDO_HOME for a day."""
    from .storage import home_dir
    path = home_dir() / "cache" / re.sub(r"[^\w.-]", "_", key)
    try:
        if time.time() - path.stat().st_mtime < ttl:
            return path.read_text()
    except OSError:
        pass
    text = compute()
    if text:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        except OSError:
            pass
    return text


@functools.lru_cache(maxsize=len(_LIST_CMDS))
def _tool_subcommands(tool: str) -> frozenset:
    """`tool`'s own real command list, queried read-only -- no side effects,
    no hardcoded list to maintain per tool."""
    cmd = _LIST_CMDS.get(tool)
    if cmd is None:
        return frozenset()

    def run():
        env = dict(os.environ)
        env.update(_TOOL_ENV.get(tool, {}))
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=5, env=env).stdout
        except (OSError, subprocess.SubprocessError):
            return ""
    return frozenset(_disk_cached("subcommands." + tool, run).split())


def _split_word(word: str, valid, cwd: str, tool: str) -> Optional[str]:
    """`remote-v` -> 'remote -v', `checkoutdevelop` -> 'checkout develop',
    `runlint` -> 'run lint': a real subcommand glued to a flag or to an
    argument that provably exists (a ref, a file, a script)."""
    for i in range(len(word) - 1, 1, -1):
        head, rest = word[:i], word[i:]
        if head not in valid:
            continue
        if rest.startswith("-") and len(rest) > 1:
            return head + " " + rest
        known = set(_npm_scripts(cwd)) if tool in ("npm", "yarn", "pnpm") else set(_git_refs(cwd)) if tool == "git" else set()
        if rest in known or os.path.exists(os.path.join(cwd or ".", rest)):
            return head + " " + rest
    return None


def match_unknown_subcommand_listed(e: FailureEvent, any_list: bool = False) -> Optional[Diagnosis]:
    """Unknown subcommand the tool didn't suggest a fix for (npm prints
    'Unknown command' to *stdout*, which we never capture; in hook mode we
    have no output at all): fuzzy-match it against the tool's own list."""
    parts = e.command.split()
    if len(parts) < 2 or parts[0] not in _LIST_CMDS:
        return None
    tool, bad = parts[0], parts[1]
    m = re.search(r'Unknown command: "([^"]+)"', e.stderr)
    if m:
        bad = m.group(1)
    elif ((tool not in _COMPLETE_LISTS and not any_list) or bad.startswith("-")
          or (e.stderr.strip() and bad not in e.stderr)):   # stderr is about something else
        return None
    valid = _tool_subcommands(tool)
    if not valid or bad in valid:
        return None
    split = _split_word(bad, valid, e.cwd, tool)
    if split:                                                 # `remote-v`, `checkoutdevelop`, `runlint`
        return _make("missing_space", "missing space", "'%s' is '%s' with a space missing" % (bad, split),
                     _swap(e.command, bad, split), HIGH if e.stderr.strip() else MEDIUM,
                     "The subcommand and its argument got glued together.",
                     "The shell splits words on spaces only.")
    if bad.lower() in valid:                                  # `git Status`
        return _make(
            "unknown_subcommand", "unknown subcommand",
            "%s subcommands are lowercase: '%s'" % (tool, bad.lower()),
            e.command.replace(bad, bad.lower(), 1), HIGH,
            "%s matches subcommands case-sensitively." % tool,
            "Subcommands are exact, lowercase words.",
        )
    tied = _close_names(bad, valid, 1 if len(bad) <= 3 else 2)   # small, closed list
    if not tied:
        pref = _COMMON_SUBCMDS.get(tool, [])
        starts = sorted((c for c in valid if c.startswith(bad)),
                        key=lambda c: (pref.index(c) if c in pref else len(pref), len(c), c))[:4]
        if not starts or len(bad) < 2:
            return None
        options = tuple(_swap(e.command, bad, c) for c in starts)   # `git sta` -> status, stash, ...
        return _make("unknown_subcommand", "unknown subcommand",
                     "'%s' is the start of several %s commands" % (bad, tool), options[0], MEDIUM,
                     "No %s command is called '%s', but several start with it." % (tool, bad),
                     "Subcommands are exact words; tab completion fills them in.",
                     options=options if len(options) > 1 else ())
    # Ties go to the commands people actually type: `rn` is `run`, not
    # `in` (= install a package called "dev"); `tset` is `test`, not `set`.
    pref = _COMMON_SUBCMDS.get(tool, [])
    tied.sort(key=lambda c: (pref.index(c) if c in pref else len(pref), c))
    good = tied[0]
    fixed = _tidy(e.command.replace(bad, good, 1))
    sure = _edit_distance(bad, good) <= 1 and (len(tied) == 1 or good in pref)
    return _make(
        "unknown_subcommand", "unknown subcommand",
        "'%s' isn't one of %s's commands: did you mean '%s'?" % (bad, tool, good),
        fixed, HIGH if sure else MEDIUM,
        "%s doesn't know '%s'; '%s' is the closest command in its own list." % (tool, bad, good),
        "Subcommands are fixed words; redo checks the tool's real list instead of guessing.",
    )


_COMPLAINT = re.compile(r"unknown|unrecognized|unexpected|not supported|invalid|illegal|bad option"
                        r"|no such option|^usage:", re.I | re.M)


@functools.lru_cache(maxsize=32)
def _man_text(page: str) -> str:
    """The man page as plain text (read-only, no side effects)."""
    def run():
        env = dict(os.environ, MANPAGER="cat", PAGER="cat", MANWIDTH="200")
        try:
            r = subprocess.run(["man", page], capture_output=True, text=True, timeout=3,
                               stdin=subprocess.DEVNULL, env=env)
        except (OSError, subprocess.SubprocessError):
            return ""
        return re.sub(r".\x08", "", r.stdout)   # strip overstrike bold
    return _disk_cached("man." + page, run)


@functools.lru_cache(maxsize=16)
def _help_flags(tool: str, sub: str = "") -> frozenset:
    """Long flags the tool documents: its man page (`git-log`, `curl`), plus
    `--help` for tools without subcommands. Only consulted after a failure."""
    text = _man_text("%s-%s" % (tool, sub)) if sub else _man_text(tool)
    if not sub:
        def run():
            env = dict(os.environ, PAGER="cat", MANPAGER="cat", GIT_PAGER="cat")
            try:
                r = subprocess.run([tool, "--help"], capture_output=True, text=True,
                                   timeout=2, stdin=subprocess.DEVNULL, env=env)
                return r.stdout + r.stderr
            except (OSError, subprocess.SubprocessError):
                return ""
        text += _disk_cached("help." + tool, run)
    return frozenset(_flags_in(text))


def _flags_in(text: str) -> set:
    """`--color`, and `--[no-]short` -> --short / --no-short."""
    out = set()
    for neg, name in re.findall(r"--(\[no-\])?([a-z][\w-]+)", text):
        out.add("--" + name)
        if neg:
            out.add("--no-" + name)
    out |= {"-" + w for w in re.findall(r"(?m)^\s+-([a-z]{3,})\b", text)}   # find's -name, -type
    return out


def _short_flags(tool: str, sub: str, stderr: str) -> set:
    """Single-letter options the tool documents: `-p, --parents` lines in
    its man page, `[-abc]` / `-x` in the usage text it just printed."""
    text = (_man_text("%s-%s" % (tool, sub)) if sub else _man_text(tool)) + "\n" + stderr
    letters = set(re.findall(r"(?m)^\s+-([A-Za-z0-9])\b", text))
    for cluster in re.findall(r"\[-([A-Za-z0-9]+)\]", text):
        letters |= set(cluster)
    letters |= set(re.findall(r"[\[ |]-([A-Za-z0-9])[\] |,]", text))
    return letters


def match_bad_flag(e: FailureEvent) -> Optional[Diagnosis]:
    """`git log --onelin`, `tar --verison`, `ls --colour`: fix the option
    against the ones the tool documents (its usage text, man page, --help)."""
    words = e.command.split()
    if not words or not _is_exec(words[0]):
        return None
    tool = words[0]
    sub = words[1] if tool in _LIST_CMDS and len(words) > 1 and not words[1].startswith("-") else ""
    longs = [w.split("=")[0] for w in words[1:]
             if re.match(r"-{1,3}[A-Za-z][\w-]*$", w.split("=")[0]) and (w.startswith("--") or len(w) > 3)]
    if e.stderr.strip():
        if not _COMPLAINT.search(e.stderr):
            return None
        bad = next((w for w in longs if w in e.stderr or "`%s'" % w.lstrip("-") in e.stderr), None)
        if not bad and re.search(r"option -- -|^usage:", e.stderr, re.M):   # sed `--n`, git's bare usage
            bad = next((w for w in longs if w.startswith("--")), None)
        if not bad:                                                        # python3 `-version`
            bad = next((w for w in longs if w[:2] != "--" and "--" + w[1:] in
                        _help_flags(tool, sub) | {"--version", "--help"}), None)
        m = re.search(r"unrecognized option [`'\"](--?[\w-]+)", e.stderr)
        bad = bad or (m.group(1) if m and m.group(1) in e.command else None)
    else:   # npm prints its usage to stdout; hook mode has nothing at all
        bad = None
    cheap = {"--version", "--help"} | _flags_in(e.stderr)

    def settled(b):             # answerable without reading the man page (~100 ms)
        return (b.lower() in cheap or len(_close_names(b, cheap - {b}, 2)) == 1
                or b.startswith("--") and len(b) <= 4 and b[2:].isalpha())

    if not bad and (e.stderr.strip() or not longs):
        valid = set()           # nothing to check
    elif bad and settled(bad):  # never in hook mode: there, "not in cheap" isn't "invalid"
        valid = cheap
    else:
        valid = cheap | _help_flags(tool, sub)
    if not bad and not e.stderr.strip() and len(valid) > 2:
        bad = next((w for w in longs if w not in valid and (w.startswith("--") or "-" + w in valid)), None)
    if not bad:
        short_help = next((w for w in words[1:] if re.fullmatch(r"-h\w{0,3}", w) and w in e.stderr), None)
        if not short_help or not e.stderr.strip():
            return None
        return _make("bad_flag", "unknown option", "%s wants the long form: --help" % tool,
                     _swap(e.command, short_help, "--help"), HIGH,
                     "%s rejected '%s'." % (tool, short_help), "Many tools only accept --help.")
    valid.discard(bad)
    if len(valid) <= 2 and bad.startswith("--") and not _close_names(bad, valid, 2):
        valid |= _help_flags(tool, sub)      # cheap set didn't help: read the docs
    glued = [f for f in valid if bad.startswith(f) and bad[len(f):][:1] in ("-", "=") or
             bad.startswith(f) and bad[len(f):].isdigit()]
    if bad.startswith("---"):
        bad_norm = "--" + bad.lstrip("-")
        good = bad_norm if bad_norm in valid or not valid - {"--version", "--help"} else (
            (_close_names(bad_norm, valid, 2) or [bad_norm])[0])     # `---stat`
    elif bad.startswith("--") and bad[2:].isalpha() and bad.lower() not in valid and (
            len(bad) - 2 <= 2 or                                   # `--h`, `--sh`: short, always
            len(bad) - 2 <= 4 and not _close_names(bad, valid, 1)
            and set(bad[2:]) <= _short_flags(tool, sub, e.stderr)):
        good = "-" + bad[2:]                 # `df --h`, `tar --czf`: doubled dash on short flags
    elif bad.lower() in valid:
        good = bad.lower()                   # `--STAT`
    elif glued:                               # `--oneline-5` -> `--oneline -5`
        f = max(glued, key=len)
        rest = bad[len(f):]
        good = f + (" " + rest if rest[0] == "-" else (rest if rest[0] == "=" else " -" + rest))
    elif bad[:2] != "--" and "--" + bad[1:] in valid:
        good = "--" + bad[1:]                # `-oneline`
    elif len(_close_names(bad, valid, 2)) == 1:
        good = _close_names(bad, valid, 2)[0]
    else:
        return None
    return _make(
        "bad_flag", "unknown option",
        "%s doesn't have a '%s' option: did you mean '%s'?" % (tool, bad, good),
        _tidy(_swap(e.command, bad, good) if bad in words else e.command.replace(bad, good, 1)),
        HIGH if e.stderr.strip() else MEDIUM,
        "%s rejected '%s'; '%s' is the closest option it documents." % (tool, bad, good),
        ("Long options take two dashes (--version); bundled short flags take "
         "one (-al). Mixing them up is the usual cause."),
    )


_KEYNEIGHBOURS = {ch: {o for o, (r2, c2) in _KEYPOS.items()
                       if o != ch and abs(r2 - r) <= 1 and abs(c2 - c) <= 1}
                  for ch, (r, c) in _KEYPOS.items()}


def match_short_flag(e: FailureEvent) -> Optional[Diagnosis]:
    """`mkdir -P`, `du -SH`, `wc -k`, `git shortlog -dn`: one letter of a
    short-flag cluster isn't an option; fix it by case or neighbouring key
    against the letters the tool documents."""
    m = (re.search(r"(?:illegal|invalid|unknown) option -- ([A-Za-z0-9])", e.stderr)
         or re.search(r"unknown (?:switch|option) `-?([A-Za-z0-9]+)'", e.stderr)
         or re.search(r"Unknown option: -([A-Za-z0-9])\b", e.stderr))
    w = e.command.split()
    if not w or (not m and e.stderr.strip()) or not any(re.fullmatch(r"-[A-Za-z0-9]+", x) for x in w[1:]):
        return None
    sub = w[1] if w[0] in _LIST_CMDS and len(w) > 1 and not w[1].startswith("-") else ""
    valid = _short_flags(w[0], sub, e.stderr)
    if m:
        said = m.group(1)                     # one letter, or git's whole cluster (`-dn`)
    else:                                     # hook mode: the man page is the evidence
        clusters = [x[1:] for x in w[1:] if re.fullmatch(r"-[A-Za-z0-9]+", x)]
        said = next((ch for x in clusters for ch in x if valid and ch not in valid), "")
        if not said:
            return None
    bad = next((ch for ch in said if ch not in valid), said[0])
    cluster = next((x for x in w[1:] if re.fullmatch(r"-[A-Za-z0-9]+", x) and bad in x), None)
    if not cluster or not valid or bad in valid:
        return None
    body = cluster[1:]
    i = next(i for i, ch in enumerate(body) if ch not in valid)
    rest = body[i:]
    if i and len(rest) >= 2 and (rest in _git_refs(e.cwd) or rest in ("HEAD",) or rest.isdigit()
                                  or os.path.exists(os.path.join(e.cwd or ".", rest))):
        return _make("missing_space", "missing space", "'%s' is '-%s %s' with the space missing" % (cluster, body[:i], rest),
                     _swap(e.command, cluster, "-%s %s" % (body[:i], rest)), HIGH if m else MEDIUM,
                     "The options and the argument after them got glued together.",
                     "The shell splits words on spaces only.")
    if sum(ch not in valid for ch in body) > 1 and body.swapcase() != body and not set(body.swapcase()) <= valid:
        return None                   # several bad letters: not a one-key slip
    fixed_cluster = None
    if cluster[1:].swapcase() != cluster[1:] and set(cluster[1:].swapcase()) <= valid:
        fixed_cluster = "-" + cluster[1:].swapcase()          # `-SH` -> -sh, `-P` -> -p
    elif bad.swapcase() in valid:
        fixed_cluster = cluster.replace(bad, bad.swapcase())
    else:
        near = sorted(n for n in _KEYNEIGHBOURS.get(bad.lower(), ()) if n in valid and n not in cluster)
        if len(near) == 1:
            fixed_cluster = cluster.replace(bad, near[0])      # `-dn` -> -sn
        elif 1 < len(near) <= 4:                               # `wc -k`: -l or -m
            options = tuple(_swap(e.command, cluster, cluster.replace(bad, n)) for n in near)
            return _make("bad_flag", "unknown option", "%s has no '-%s' option" % (w[0], bad),
                         options[0], MEDIUM, "These neighbouring keys are options %s documents." % w[0],
                         "Short options are single, case-sensitive letters.", options=options)
    if not fixed_cluster:
        return None
    return _make("bad_flag", "unknown option",
                 "%s has no '-%s' option: did you mean '%s'?" % (w[0], bad, fixed_cluster),
                 _swap(e.command, cluster, fixed_cluster), MEDIUM,
                 "%s rejected '-%s'; '%s' uses only options it documents." % (w[0], bad, fixed_cluster),
                 "Short options are single, case-sensitive letters.")


def match_missing_dash(e: FailureEvent) -> Optional[Diagnosis]:
    """`du sh src`, `head 5 f`, `wc l f`, `git remote v`, `python3 m json.tool`:
    a flag typed without its dash, read as a file or subcommand."""
    w = e.command.split()
    if len(w) < 2:
        return None
    sub = w[1] if w[0] in _LIST_CMDS and len(w) > 2 and not w[1].startswith("-") else ""
    for i, t in enumerate(w[1:], 1):
        if t == sub or not re.fullmatch(r"[A-Za-z]{1,3}|\d+", t):
            continue
        said = re.search(r"%s(?:: |'\.?)(?:No such file|open: No such|can't open)|subcommand: `%s'"
                         r"|No rule to make target `%s'" % ((re.escape(t),) * 3), e.stderr)
        if not said and not re.search(r"can't open file '[^']*/%s'" % re.escape(t), e.stderr):
            continue
        if os.path.exists(os.path.join(e.cwd or ".", t)):
            return None
        valid = _short_flags(w[0], sub, e.stderr)
        if t.isdigit() and w[0] in ("head", "tail") or (valid and set(t) <= valid):
            return _make("missing_dash", "missing dash", "'%s' looks like the option '-%s' without its dash" % (t, t),
                         _swap(e.command, t, "-" + t), MEDIUM,
                         "%s read '%s' as a file or subcommand; '-%s' is an option it documents." % (w[0], t, t),
                         "Options start with a dash; without it they're just arguments.")
        return None
    return None


def match_bad_subverb(e: FailureEvent) -> Optional[Diagnosis]:
    """`git stash aply`: the third word is one of a fixed set of verbs
    listed in the tool's own synopsis (`git stash apply`, `git remote add`)."""
    w = e.command.split()
    if len(w) < 3 or w[0] not in _LIST_CMDS or w[2].startswith("-") or (
            e.stderr.strip() and w[2] not in e.stderr):
        return None
    verbs = set(re.findall(r"^\s*(?:or:\s+)?%s %s ([a-z][\w-]+)" % (w[0], w[1]),
                           _man_text("%s-%s" % (w[0], w[1])), re.M))
    if len(verbs) < 2 or w[2] in verbs:     # only tools with a real verb list
        return None
    close = _close_names(w[2], verbs, 2)
    if len(close) != 1:
        return None
    return _make("unknown_subcommand", "unknown subcommand",
                 "'%s' isn't a `%s %s` command: did you mean '%s'?" % (w[2], w[0], w[1], close[0]),
                 _swap(e.command, w[2], close[0]), HIGH if e.stderr.strip() else MEDIUM,
                 "The `%s %s` synopsis lists '%s'." % (w[0], w[1], close[0]),
                 "Some subcommands have their own fixed verbs.")


def _refine_typo(command: str, tool: str, orig_name: str) -> Optional[Diagnosis]:
    """`gti brance`: after fixing the name, check the subcommand too, or a
    chained typo gets pushed as a fix that fails the moment it runs."""
    d = match_unknown_subcommand_listed(FailureEvent(
        id=None, command=command, exit_code=1, stderr="", cwd="", timestamp=0, shell=""),
        any_list=True)
    if d is None:
        return None
    return dataclasses.replace(
        d, rule_id="command_typo_subcommand",
        summary="'%s' isn't a command, and '%s' isn't a %s subcommand either"
                % (orig_name, command.split()[1], tool))


# Shell builtins/keywords: never "not found", never the culprit.
_BUILTINS = set(
    "cd export source . alias unalias set unset type eval exec exit return "
    "local read pushd popd dirs history fg bg jobs kill wait test [ [[ true "
    "false echo printf if then else elif fi for do done while until case "
    "esac function time { } ! builtin command declare typeset setopt "
    "unsetopt shift trap ulimit umask which where whence print noglob hash "
    "rehash let".split())
# Words that run the *next* word as the command.
_PREFIXES = {"time", "env", "command", "builtin", "exec", "nohup", "xargs",
             "nice", "noglob", "nocorrect", "watch"}


def _command_words(cmd: str):
    """The command word of every segment (`a && b | c; d`), skipping
    `VAR=x` assignments and prefixes like `time` / `env` / `xargs -n1`."""
    cmd = re.sub(r"'[^']*'|\"(?:\\.|[^\"\\])*\"", "''", cmd)   # quoted text isn't a command
    for seg in re.split(r"&&|\|\||[;|\n&()`]|\$\(", cmd):
        prefixed = False
        for w in seg.split():
            if re.match(r"^[A-Za-z_]\w*=", w) or (prefixed and w.startswith("-")):
                continue
            if w in _PREFIXES:
                prefixed = True
                continue
            yield w.strip("'\"")
            break


def _failing_name(e: FailureEvent) -> Optional[str]:
    """No stderr to read (hook mode, or a tool that didn't say): the first
    command word that is neither a builtin nor installed."""
    installed = _path_executables()
    for w in _command_words(e.command):
        if "/" in w:
            if not os.path.exists(os.path.join(e.cwd or ".", os.path.expanduser(w))):
                return w
        elif w not in _BUILTINS and w not in installed:
            return w
    return None


def _split_glued(name: str, cwd: str = ""):
    """`grep-c` -> (grep, -c), `gitstatus` -> (git, status), `cd..` ->
    (cd, ..): only when the split is provably right, else 'catalog' would
    become 'cat alog'."""
    for i in range(len(name) - 1, 1, -1):
        tool, rest = name[:i], name[i:]
        if not (tool in _BUILTINS or (tool in _path_executables() and _is_exec(tool))):
            continue
        if rest[:1] == "-" and rest[1:2] != "-" and tool in _LIST_CMDS and rest[1:] in _tool_subcommands(tool):
            return tool, rest[1:]                  # `git-log` -> git log
        if tool in ("man", "which", "type", "where") and _is_exec(rest):
            return tool, rest                       # `mandiff` -> man diff
        if (rest.startswith("-") and len(rest) > 1
                or (tool == "cd" and rest[0] in "./~")
                or (len(rest) > 1 and os.path.exists(os.path.join(cwd or ".", rest)))   # `catREADME.md`, `python3src/x.py`
                or (tool in _LIST_CMDS and rest in _tool_subcommands(tool))):
            return tool, rest
    return None


def match_command_not_found(e: FailureEvent) -> Optional[Diagnosis]:
    m = (re.search(r"The term '([^']+)' is not recognized", e.stderr)      # PowerShell
         or re.search(r"command not found: (\S+)", e.stderr)
         or re.search(r"(\S+): command not found", e.stderr)
         or re.search(r"xargs: (\S+): No such file or directory", e.stderr)
         or re.search(r"(\S+): not found", e.stderr))
    if m:
        name = m.group(1)
    elif e.exit_code == 127:
        name = _failing_name(e)
        if name is None:
            return None
    else:
        return None

    if "/" in name and not _split_glued(name, e.cwd):   # a path; match_missing_file had first go
        return _make(
            "command_not_found", "command not found",
            "there's no file at '%s'" % name,
            "# check the path: ls %s" % (os.path.dirname(name) or "."), MEDIUM,
            "'%s' is a path, so the shell ran it directly instead of searching "
            "$PATH, and nothing exists there." % name,
            "A command containing '/' is run as a file path; $PATH isn't consulted.",
        )

    installed = _path_executables()

    # `pip` / `python` missing but the versioned binary is right there.
    if name + "3" in installed and _is_exec(name + "3"):
        fixed = _tidy(e.command.replace(name, name + "3", 1))
        return _make(
            "command_versioned", "command not found",
            "'%s' isn't on your PATH, but '%s3' is" % (name, name), fixed, HIGH,
            ("Many systems (macOS included) only ship the versioned name: "
             "'%s3', not '%s'." % (name, name)),
            ("Python tooling is often installed only under its versioned "
             "name. Use '%s3', or alias '%s' to it in your shell rc." % (name, name)),
        )

    split = _split_glued(name, e.cwd)
    if split:
        tool, rest = split
        return _make(
            "missing_space", "command not found",
            "'%s' is '%s %s' with the space missing" % (name, tool, rest),
            e.command.replace(name, tool + " " + rest, 1), HIGH,
            "The shell read '%s' as one command name; it needs a space "
            "after '%s'." % (name, tool),
            "The shell splits words on spaces: the first word is the "
            "command, the rest are its arguments.",
        )

    matches, distance, clear = _closest_executables(name)
    # A "match" that is just a prefix of what was typed drops the rest
    # (`echohi` -> echo): that's a glued word, not a typo.
    rest = name[len(matches[0]):] if matches and name.startswith(matches[0]) else ""
    if rest and (len(rest) > 1 or os.path.exists(os.path.join(e.cwd or ".", rest))):   # not `gitt`
        tool, rest = matches[0], name[len(matches[0]):]
        return _make(
            "missing_space", "command not found",
            "'%s' looks like '%s %s' with the space missing" % (name, tool, rest),
            e.command.replace(name, tool + " " + rest, 1), MEDIUM,
            "'%s' starts with the command '%s'; the rest reads like its argument." % (name, tool),
            "The shell splits words on spaces: the first word is the command.",
        )
    if matches:
        # pytho -> python -> (not installed, but python3 is) python3
        matches = [m + "3" if not _is_exec(m) and _is_exec(m + "3") else m
                   for m in matches]
        correct = matches[0]
        fixed = _tidy(e.command.replace(name, correct, 1)) if e.command else correct

        if not _is_exec(correct):
            return _make(
                "command_typo_not_installed", "command not found",
                "'%s' looks like '%s', but '%s' isn't installed" % (name, correct, correct),
                "# install %s (e.g. brew install %s), then: %s" % (correct, correct, fixed),
                MEDIUM,
                ("'%s' is one slip away from '%s', but there's no '%s' on your "
                 "$PATH either, so fixing the spelling alone won't help." % (name, correct, correct)),
                ("'command not found' can hide two problems: a typo and a "
                 "missing install. Fix the spelling, then install the tool."),
            )

        if correct in _LIST_CMDS:
            refined = _refine_typo(fixed, correct, name)
            if refined is not None:
                return refined

        confidence = HIGH if clear and distance <= 1 and len(name) > 2 else MEDIUM

        options = tuple(_tidy(e.command.replace(name, m, 1)) for m in matches)
        return _make(
            "command_typo",
            "command not found",
            "'%s' isn't a command: did you mean '%s'?" % (name, correct) if len(matches) == 1
            else "'%s' isn't a command: closest matches on your $PATH" % name,
            fixed,
            confidence,
            ("The shell searched every directory in $PATH for '%s' and found "
             "nothing. '%s' is the closest name actually installed on this "
             "machine." % (name, correct)),
            ("'command not found' means the shell couldn't locate the binary on "
             "$PATH. redo checks what's really installed instead of matching "
             "against a canned list of typos."),
            options=options if len(options) > 1 else (),
        )

    if name == "brew":
        return _make(
            "command_not_found",
            "command not found",
            "Homebrew (`brew`) is not installed or not on your PATH",
            '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"',
            MEDIUM,
            ("The shell can't find `brew` on $PATH: either Homebrew isn't "
             "installed, or its shellenv line hasn't been added to your profile "
             "(on Apple Silicon that's /opt/homebrew/bin)."),
            ("A program runs only if its binary sits in a $PATH directory. "
             "Installers usually append that line to your shell profile; if "
             "they didn't, the command 'isn't found' even though it's on disk."),
        )

    if name in _RANK:   # a real, well-known tool that just isn't installed
        return _make(
            "command_not_installed", "command not found",
            "'%s' isn't installed (or isn't on your PATH)" % name,
            "# install %s (e.g. brew install %s) or check it's on $PATH" % (name, name),
            MEDIUM,
            ("'%s' is a real tool, but the shell found no executable by that "
             "name in any $PATH directory." % name),
            ("$PATH is the list of directories your shell searches for commands. "
             "'command not found' means the name matched nothing there."),
        )

    # Unknown name, nothing close: say so. Never suggest installing the typo.
    return _make(
        "command_not_found",
        "command not found",
        "'%s' isn't a command, and nothing installed is close to it" % name,
        "# check the spelling: nothing on your $PATH is near '%s'" % name,
        LOW,
        ("The shell searched every directory in $PATH and found no executable "
         "named '%s', and no installed command is within a typo or two of it." % name),
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
    runner = {".py": "python3", ".sh": "bash", ".js": "node", ".rb": "ruby"}.get(os.path.splitext(target or "")[1])
    if target and runner:
        try:
            with open(os.path.join(e.cwd or ".", target), "rb") as f:
                shebang = f.read(2) == b"#!"
        except OSError:
            shebang = True
        if not shebang:      # chmod +x would run it under sh and fail
            return _make("run_with_interpreter", "permission denied",
                         "'%s' isn't executable and has no #! line" % target,
                         _swap(e.command, target, runner + " " + target), HIGH,
                         "Without a shebang the kernel can't tell how to run it; ask %s directly." % runner,
                         "Scripts run as ./name need +x *and* a #! line naming the interpreter.")
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


# Never "correct" which file these act on: a missing `rm` target is not a
# typo to fix, and retargeting `mv`/`dd` moves or destroys the wrong file.
_NO_RETARGET = DESTRUCTIVE | {"cp", "ln", "install", "rsync"}
_COPY_LIKE = {"cp", "rsync", "install", "scp"}
# Commands whose plain-word arguments are file names (`ls dcos`).
_FILE_CMDS = {"ls", "cat", "less", "more", "head", "tail", "vim", "vi", "nano", "code",
              "open", "cd", "source", "wc", "file", "stat", "du", "bat", "cp",
              "python", "python3", "node", "ruby", "bash", "sh", "perl", "deno", "bun"}
_PLAIN_WORD = re.compile(r"[\w./~+@%,:-]+")
_VERSION = re.compile(r"\d+(?:\.\d+)+")


def _path_from_history(t: str) -> list:
    """`cat config.yml` -> the `~/proj/app/config.yml` you've used before."""
    if "/" in t:
        return []
    for h in _history():
        for word in h.split():
            if "/" in word and os.path.basename(word) == t and \
                    os.path.exists(os.path.expanduser(word)):
                return [word]
    return []


def match_missing_file(e: FailureEvent) -> Optional[Diagnosis]:
    """A path argument that doesn't exist but has a near-identical sibling:
    `./scirpt.sh`, `python3 scrpit.py`, `cd projets`, `cat ~/.zshrcc`.
    Works without stderr (hook mode) since the filesystem is the evidence."""
    if e.stderr.strip() and not re.search(
            r"no such file|not found|can't open|cannot find|does not exist|did not match",
            e.stderr, re.I):
        return None
    words = e.command.split()
    cmd_words = set(_command_words(e.command))
    if cmd_words & (_NO_RETARGET - _COPY_LIKE):
        return None
    if cmd_words & _COPY_LIKE:
        words = words[:-1]      # never "correct" a destination: it may not exist yet
    for i, t in enumerate(words):
        is_cd_arg = i > 0 and words[i - 1] not in ("-m", "-c", "-e") and (
            words[i - 1] == "cd" or words[0] in _FILE_CMDS)
        named = len(t) > 1 and re.search(r"[/'`\s]%s['`:]" % re.escape(t), e.stderr)
        if ((i == 0 and "/" not in t) or not _PLAIN_WORD.fullmatch(t) or t.startswith("-") or "://" in t
                or _VERSION.fullmatch(t)
                or not (is_cd_arg or named or "/" in t or "." in t[1:] or t.startswith(("~", ".")))):
            continue
        if _exists_exact(os.path.join(e.cwd or ".", os.path.expanduser(t.rstrip("/")))):
            continue
        close = _fix_path(t, e.cwd) or [
            t + ext for ext in (".py", ".js", ".ts", ".sh", ".rb", ".go")
            if "." not in os.path.basename(t) and os.path.exists(os.path.join(e.cwd or ".", t + ext))]
        # A name already elsewhere in the command would read and write the
        # same file (`sort a.tx > a.txt`) or do nothing (`mv nw.txt new.txt`).
        close = [n for n in close if n not in words] or _path_from_history(t)
        if not close:
            continue
        options = tuple(_swap(e.command, t, n) for n in close)
        good = close[0]
        sure = len(close) == 1 and e.stderr.strip()
        return _make(
            "missing_file", "file not found",
            "'%s' doesn't exist, but '%s' does" % (t, good) if len(close) == 1
            else "'%s' doesn't exist; similar names are here" % t,
            options[0], HIGH if sure else MEDIUM,
            "Nothing is called '%s'; '%s' is the closest match on disk." % (t, good),
            "Paths are matched exactly, character for character. Tab completion avoids this.",
            options=options if len(options) > 1 else (),
        )
    return None


def _undocumented_short_flag(words) -> bool:
    valid = _short_flags(words[0], "", "")
    letters = {ch for w in words[1:] if re.fullmatch(r"-[A-Za-z0-9]+", w) for ch in w[1:]}
    return bool(valid and letters - valid)


def _parent_missing(e: FailureEvent, path: str) -> bool:
    parent = os.path.dirname(os.path.join(e.cwd or ".", os.path.expanduser(path)))
    return not os.path.isdir(parent)


def match_mkdir_parent(e: FailureEvent) -> Optional[Diagnosis]:
    parts = e.command.split()
    if parts[:1] != ["mkdir"] or "-p" in parts:
        return None
    if not (_has(e.stderr, "no such file") if e.stderr.strip()
            else any(_parent_missing(e, a) for a in parts[1:] if not a.startswith("-"))
            and not _undocumented_short_flag(parts)):
        return None
    return _make(
        "mkdir_parent", "missing parent directory",
        "a parent directory doesn't exist yet",
        "mkdir -p " + e.command.split(None, 1)[1], HIGH if e.stderr.strip() else MEDIUM,
        "mkdir only creates the last path component unless told to create parents.",
        "`mkdir -p` creates every missing directory along the path (and doesn't fail if it exists).",
    )


def match_did_you_mean_word(e: FailureEvent) -> Optional[Diagnosis]:
    """Python (and others) already say "Did you mean 'import'?" -- apply it
    to the word it was about."""
    m = re.search(r"Did you mean:? '([\w.]+)'\?", e.stderr)
    if not m:
        return None
    good = m.group(1)
    n = re.search(r"name '(\w+)' is not defined", e.stderr)
    if n:
        bad = n.group(1)
    else:
        close = _close_names(good, set(re.findall(r"\w+", e.command)) - {good})
        if not close:
            return None
        bad = close[0]
    fixed = re.sub(r"\b%s\b" % re.escape(bad), good, e.command, count=1)
    return _make(
        "did_you_mean", "typo in code",
        "'%s' should be '%s'" % (bad, good), fixed, HIGH,
        "The program itself suggested '%s' in place of '%s'." % (good, bad),
        "Interpreters spell-check names and keywords on error; read the last line of the traceback.",
    )


def match_make_target(e: FailureEvent) -> Optional[Diagnosis]:
    m = re.search(r"No rule to make target [`'\"]([^'\"]+)'", e.stderr)
    parts = e.command.split()
    if m:
        bad = m.group(1)
    elif not e.stderr.strip() and parts[:1] == ["make"] and len(parts) > 1 and not parts[-1].startswith("-"):
        bad = parts[-1]             # hook mode: guess the last word
    else:
        return None
    try:
        with open(os.path.join(e.cwd or ".", "Makefile")) as f:
            targets = set(re.findall(r"^([A-Za-z0-9_][\w.-]*)\s*:(?!=)", f.read(), re.M))
    except OSError:
        return None
    if bad in targets:
        return None
    close = _close_names(bad, targets)
    if not close:
        return None
    good = close[0]
    return _make(
        "make_target", "unknown make target",
        "the Makefile has no '%s' target: did you mean '%s'?" % (bad, good),
        e.command.replace(bad, good, 1), HIGH if m else MEDIUM,
        "make looked for a rule named '%s' and found none; '%s' is the closest target in ./Makefile." % (bad, good),
        "make targets are the names before ':' in the Makefile.",
    )


# ------------------------------------------------- general mechanisms
# Borrowed from thefuck's most general rules: instead of a table of known
# slips, compare against what the tool, the repo, or *you* already know.

def match_printed_fix(e: FailureEvent) -> Optional[Diagnosis]:
    """The tool printed the exact command to run (`git push
    --set-upstream origin x`, `brew install --cask x`): use it."""
    tool = (e.command.split() or [""])[0]
    if not tool:
        return None
    for ln in e.stderr.splitlines():
        cand = ln.strip()
        head = " ".join(e.command.split()[:2]) + " "
        if (ln[:1] in " \t" and cand.startswith(head) and cand != e.command.strip()
                and not re.search(r"[<\[|]|\.\.\.|usage|\b[A-Z]{2,}\b", cand)):
            return _make(
                "printed_fix", "the tool told you",
                "%s printed the command that fixes this" % tool, cand, HIGH,
                "%s's own error output names this command as the fix." % tool,
                "Read the indented lines in an error: tools often print the exact next command.",
            )
    return None


@functools.lru_cache(maxsize=32)   # `redo stats` re-diagnoses every row
def _git_refs(cwd: str) -> tuple:
    try:
        out = subprocess.run(
            ["git", "-C", cwd or ".", "for-each-ref", "--format=%(refname:short)",
             "refs/heads", "refs/remotes", "refs/tags"],
            capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return ()
    return tuple(r[len("origin/"):] if r.startswith("origin/") else r for r in out.split())


def match_git_ref_typo(e: FailureEvent) -> Optional[Diagnosis]:
    """`git checkout feature-logni` / `git switch mian`: match against the
    repo's real branches and tags."""
    amb = re.search(r"ambiguous argument '([^']+)'", e.stderr)
    m = re.match(r"\s*git\s+(checkout|switch|merge|push\s+\S+)\s+(\S+)\s*$", e.command)   # never delete/rebase targets
    if amb and re.match(r"\s*git\s+(show|diff|log|merge|checkout|switch|cherry-pick)\b", e.command):
        bad = amb.group(1)
    elif not m or m.group(2).startswith("-"):
        return None
    else:
        bad = m.group(2)
    said = re.search(r"pathspec '([^']+)' did not match|invalid reference: (\S+)"
                     r"|not something we can merge|invalid upstream|src refspec|ambiguous argument", e.stderr)
    if e.stderr.strip() and not said:
        return None
    refs = set(_git_refs(e.cwd)) | {"HEAD", "FETCH_HEAD", "ORIG_HEAD"}
    if bad in refs:
        return None
    close = (_close_names(bad, refs) or sorted(r for r in refs if r.startswith(bad) and len(bad) >= 3)[:4])
    if not close:
        return None
    options = tuple(e.command.replace(bad, r, 1) for r in close[:4])
    return _make(
        "git_ref_typo", "unknown branch",
        "there's no branch '%s'" % bad + ("; did you mean '%s'?" % close[0] if len(close) == 1 else ""),
        options[0], HIGH if len(close) == 1 and said else MEDIUM,
        "git looked for a branch, tag or file named '%s' and found none; these are the closest refs in this repo." % bad,
        "Branch names are exact. `git branch -a` lists them; tab completion avoids typos.",
        options=options if len(options) > 1 else (),
    )


def _history_path() -> Optional[str]:
    h = os.environ.get("HISTFILE")
    if h:
        return h
    for name in (".zsh_history", ".bash_history", ".local/share/fish/fish_history"):
        p = os.path.join(os.path.expanduser("~"), name)
        if os.path.exists(p):
            return p
    return None


@functools.lru_cache(maxsize=1)
def _history() -> tuple:
    """Recent unique commands from the shell's own history, newest first.
    Reads only the tail (zsh `: ts:0;cmd`, bash plain, fish `- cmd: x`)."""
    lines = []
    for path in (_history_path(), os.environ.get("REDO_SESSION_HISTORY")):
        if not path:
            continue
        try:
            with open(path, "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 256 * 1024))
                lines += f.read().decode(errors="replace").splitlines()
        except OSError:
            pass
    seen, out = set(), []
    for ln in reversed(lines):
        ln = re.sub(r"^: \d+:\d+;|^- cmd: ", "", ln).strip()
        if ln and ln not in seen and not ln.endswith("\\"):
            seen.add(ln)
            out.append(ln)
    return tuple(out[:5000])


def match_history(e: FailureEvent) -> Optional[Diagnosis]:
    """Last resort, like thefuck's `history` rule: the closest command you've
    run before. Only safe, different, runnable commands qualify; ties go to
    the most recent."""
    cmd = e.command.strip()
    limit = max(1, min(3, len(cmd) // 6))
    best = None
    for h in _history():
        if abs(len(h) - len(cmd)) > limit or h == cmd:
            continue
        d = _edit_distance(cmd, h, limit)
        if d <= limit and (best is None or d < best[0]):
            first = h.split()[0]
            if not is_risky(h) and (first in _BUILTINS or _is_exec(first)):
                best = (d, h)
                if d == 1:
                    break
    if not best:
        return None
    return _make(
        "history", "close to a command you've run",
        "you've run something very close to this before", best[1], MEDIUM,
        "No rule matched, but your shell history has '%s', a typo or two away." % best[1],
        "Your own history is the best typo dictionary: redo checks it when nothing else fits.",
    )


# ------------------------------------------------------------ shape rules
# Failure *shapes* thefuck covers with general rules. Each is keyed on what
# went wrong (a pasted prompt, a directory where a file was expected, ...),
# never on a specific typo.

def match_pasted_prompt(e: FailureEvent) -> Optional[Diagnosis]:
    m = re.match(r"\s*[$%>#]\s+(\S.*)", e.command)
    if not m or e.exit_code != 127:
        return None
    return _make("pasted_prompt", "pasted prompt",
                 "the leading '%s' is a copied shell prompt, not a command" % e.command.strip()[0],
                 m.group(1), HIGH,
                 "Docs show commands after a prompt sign; it got pasted along with the command.",
                 "Copy from after the `$` -- the prompt sign isn't part of the command.")


def match_doubled_word(e: FailureEvent) -> Optional[Diagnosis]:
    w = e.command.split()
    if len(w) < 3 or w[0] != w[1]:
        return None
    return _make("doubled_word", "doubled word", "'%s' was typed twice" % w[0],
                 e.command.split(None, 1)[1], HIGH,
                 "The tool read the second '%s' as its subcommand." % w[0],
                 "Usually a leftover from editing a previous command.")


def match_local_script(e: FailureEvent) -> Optional[Diagnosis]:
    """`deploy.sh` that exists here: the shell only searches $PATH."""
    w = e.command.split()
    if e.exit_code != 127 or not w or "/" in w[0]:
        return None
    path = os.path.join(e.cwd or ".", w[0])
    if not os.path.isfile(path):
        return None
    if os.access(path, os.X_OK):
        fix = "./" + e.command.strip()
    else:
        runner = {".py": "python3", ".sh": "bash", ".js": "node", ".rb": "ruby"}.get(os.path.splitext(w[0])[1])
        if not runner:
            return None
        fix = runner + " " + e.command.strip()
    return _make("local_script", "script not on PATH",
                 "'%s' is in this folder, but the shell only looks on $PATH" % w[0], fix, HIGH,
                 "Commands without a '/' are looked up on $PATH only; the current folder isn't on it.",
                 "Run local scripts as ./name (needs +x) or through their interpreter.")


_DIR_FIX = {"cat": "ls", "less": "ls", "more": "ls", "head": "ls", "tail": "ls",
            "grep": "-r", "cp": "-r", "scp": "-r", "rm": "-r"}


def match_is_a_directory(e: FailureEvent) -> Optional[Diagnosis]:
    w = e.command.split()
    how = _DIR_FIX.get(w[0] if w else "")
    if not how:
        return None
    if e.stderr.strip():
        if not re.search(r"is a directory", e.stderr, re.I):
            return None
    else:   # hook mode: the disk says which argument is a folder
        args = [a for a in w[1:] if not a.startswith("-")]
        args = args[:-1] if w[0] in ("cp", "scp") else args       # cp's destination may be a dir
        if not any(os.path.isdir(os.path.join(e.cwd or ".", os.path.expanduser(a))) for a in args):
            return None
        if _undocumented_short_flag(w):       # `grep -tn dir`: the bad flag is the real error
            return None
    fix = ("ls " + " ".join(w[1:])) if how == "ls" else "%s %s %s" % (w[0], how, " ".join(w[1:]))
    return _make("is_a_directory", "is a directory",
                 "'%s' was given a directory" % w[0], fix, HIGH if e.stderr.strip() else MEDIUM,
                 "%s expected a file but got a folder." % w[0],
                 "Folders need `ls` to view, or -r to grep/copy/remove recursively.")


def _exists_exact(path: str) -> bool:
    """os.path.exists, but case-sensitive even on macOS's case-insensitive
    disk: `Readme.md` "exists" there, yet git and friends won't match it."""
    if not os.path.exists(path):
        return False
    folder, name = os.path.split(os.path.normpath(path))
    try:
        return name in (".", "..", "") or name in os.listdir(folder or ".")
    except OSError:
        return True


def _fix_path(t: str, cwd: str) -> list:
    """Correct a path one component at a time (`projets/foo` ->
    projects/foo). A case-only difference wins outright, else the closest
    sibling. Returns the corrected path, the tied last-component options,
    or [] when it can't be fixed unambiguously."""
    parts = t.rstrip("/").split("/")
    start = 1 if parts[0] in ("", "~") else 0
    here = {"": "/", "~": os.path.expanduser("~")}.get(parts[0], cwd or ".") if start else (cwd or ".")
    out = parts[:start]
    for i in range(start, len(parts)):
        comp = parts[i]
        nxt = os.path.join(here, comp)
        if comp in ("", ".", "..") or _exists_exact(nxt):
            out.append(comp)
            here = nxt
            continue
        try:
            names = os.listdir(here)
        except OSError:
            return []
        close = [n for n in names if n.lower() == comp.lower()] or _close_names(comp, names)
        if len(close) != 1:
            last = i == len(parts) - 1
            return ["/".join(out + [n]) for n in close[:4]] if close and last else []
        out.append(close[0])
        here = os.path.join(here, close[0])
    fixed = "/".join(out)
    return [fixed] if fixed != t.rstrip("/") else []


def match_touch_parent(e: FailureEvent) -> Optional[Diagnosis]:
    w = e.command.split()
    if w[:1] != ["touch"] or len(w) != 2 or "/" not in w[1]:
        return None
    if not (_has(e.stderr, "no such file") if e.stderr.strip() else _parent_missing(e, w[1])):
        return None
    return _make("touch_parent", "missing parent directory", "the folder for '%s' doesn't exist" % w[1],
                 "mkdir -p %s && %s" % (os.path.dirname(w[1]), e.command.strip()), HIGH,
                 "touch creates files, not the folders above them.",
                 "`mkdir -p` creates every missing folder along a path.")


def match_cd_new_dir(e: FailureEvent) -> Optional[Diagnosis]:
    w = e.command.split()
    if w[:1] != ["cd"] or len(w) != 2 or not _PLAIN_WORD.fullmatch(w[1]):
        return None
    if os.path.exists(os.path.join(e.cwd or ".", os.path.expanduser(w[1]))):
        return None
    return _make("cd_new_dir", "no such directory", "'%s' doesn't exist yet" % w[1],
                 "mkdir -p %s && cd %s" % (w[1], w[1]), MEDIUM,
                 "There's no folder by that name (or anything close to it) here.",
                 "cd only enters existing folders; create it first if you meant a new one.")


def match_ln_order(e: FailureEvent) -> Optional[Diagnosis]:
    m = re.match(r"\s*ln\s+(-\w*s\w*)\s+(\S+)\s+(\S+)\s*$", e.command)
    if not m or (e.stderr.strip() and not _has(e.stderr, "file exists")):
        return None
    flag, a, b = m.groups()
    j = lambda x: os.path.join(e.cwd or ".", x)
    if os.path.lexists(j(a)) or not os.path.exists(j(b)):
        return None
    return _make("ln_order", "ln arguments reversed", "ln -s takes the target first, then the link name",
                 "ln %s %s %s" % (flag, b, a), HIGH,
                 "'%s' exists and '%s' doesn't, so they're in the wrong order." % (b, a),
                 "`ln -s TARGET LINK`: same order as `cp SOURCE DEST`.")


def match_quotes(e: FailureEvent) -> Optional[Diagnosis]:
    if not re.search(r"unmatched [\"']|unexpected EOF while looking for matching|"
                     r"unterminated quoted string", e.stderr):
        return None
    cmd = e.command
    if cmd.count("'") % 2 and cmd.count('"') % 2:          # echo 'mixed"
        i = min(cmd.index("'"), cmd.index('"'))
        opener, other = cmd[i], "'" if cmd[i] == '"' else '"'
        j = cmd.rindex(other)
        fix = cmd[:j] + opener + cmd[j + 1:]
    elif cmd.count("'") % 2 and '"' not in cmd and cmd.count("'") >= 3:
        i, j = cmd.index("'"), cmd.rindex("'")      # 'it's broken' -> "it's broken"
        fix = cmd[:i] + '"' + cmd[i + 1:j] + '"' + cmd[j + 1:]
    elif cmd.count('"') % 2:
        fix = cmd + '"'
    elif cmd.count("'") % 2:
        fix = cmd + "'"
    else:
        return None
    try:
        shlex.split(fix)
    except ValueError:
        return None
    return _make("unclosed_quote", "unbalanced quotes", "a quote was opened but never closed",
                 fix, MEDIUM,
                 "The shell kept reading for the closing quote and hit the end of the line.",
                 "Quotes pair up; an apostrophe inside '...' ends it early -- use \"...\" around it.")


def _git(cwd: str, *args) -> str:
    try:
        return subprocess.run(["git", "-C", cwd or "."] + list(args), capture_output=True,
                              text=True, timeout=3).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _git_hook_evidence(e: FailureEvent, c: str) -> str:
    """Hook mode has no output; reconstruct the message git would have
    printed from the repo's actual state, so the rules below can fire."""
    if re.match(r"git\s+commit\b", c) and "-a" not in c.split() and \
            _git(e.cwd, "status", "--porcelain") and not _git(e.cwd, "diff", "--cached", "--name-only"):
        return "no changes added to commit"
    m = re.match(r"git\s+(?:checkout\s+-b|switch\s+-c)\s+(\S+)", c)
    if m and m.group(1) in _git_refs(e.cwd):
        return "a branch named '%s' already exists" % m.group(1)
    if re.match(r"git\s+pull\s*$", c) and not _git(e.cwd, "rev-parse", "--abbrev-ref", "@{u}"):
        return "no tracking information"
    m = re.match(r"git\s+merge\s+([\w./-]+)\s*$", c)
    if m and _git(e.cwd, "rev-parse", "--verify", "-q", m.group(1)) and not _git(e.cwd, "merge-base", "HEAD", m.group(1)):
        return "refusing to merge unrelated histories"
    if re.match(r"git\s+stash\s+(pop|apply)\b", c) and _git(e.cwd, "status", "--porcelain", "--untracked-files=no"):
        return "would be overwritten"
    m = re.match(r"git\s+(?:checkout|switch)\s+([\w./-]+)\s*$", c)
    if m and m.group(1) not in _git_refs(e.cwd) and not os.path.exists(os.path.join(e.cwd or ".", m.group(1))):
        return "did not match any file"
    return ""


def match_git_shapes(e: FailureEvent) -> Optional[Diagnosis]:
    """git failures whose fix is a known next step, not a spelling fix."""
    c, err = e.command.strip(), e.stderr
    if not c.startswith("git "):
        return None
    if not err.strip():
        err = _git_hook_evidence(e, c)
    m = re.search(r"refusing to merge unrelated histories", err)
    if m:
        return _make("git_unrelated", "unrelated histories", "the two branches share no history",
                     c + " --allow-unrelated-histories", MEDIUM,
                     "git refuses to merge histories with no common commit unless told to.",
                     "Common after `git init` + adding an existing remote.")
    m = re.search(r"option '(-[^']+)' must come before non-option arguments", err)
    if m:
        w = c.split()
        w.remove(m.group(1))
        return _make("git_flag_order", "flag after filename", "'%s' must come before file names" % m.group(1),
                     " ".join(w[:2] + [m.group(1)] + w[2:]), HIGH,
                     "git stops reading options at the first file name.",
                     "Put options right after the subcommand.")
    if "would be overwritten" in err and re.match(r"git\s+stash\s+(pop|apply)", c):
        return _make("git_stash_blocked", "local changes in the way",
                     "your uncommitted changes conflict with the stash",
                     "# commit or stash your current changes first, then: " + c, MEDIUM,
                     "Applying the stash would overwrite files you've changed since.",
                     "git won't let a stash clobber uncommitted work.")
    if "no changes added to commit" in err or "nothing added to commit but untracked" in err:
        return _make("git_nothing_staged", "nothing staged", "nothing is staged for this commit",
                     "git add -A && " + c, MEDIUM,
                     "git commit only records staged changes, and none were staged.",
                     "`git add` stages changes; `git commit -a` stages tracked files automatically.")
    m = re.search(r"a branch named '([^']+)' already exists", err)
    if m and re.search(r"checkout\s+-b|switch\s+-c", c):
        return _make("git_branch_exists", "branch exists", "'%s' already exists" % m.group(1),
                     re.sub(r"\s(?:-b|-c)\s", " ", c, count=1), HIGH,
                     "You asked to create a branch that's already there; switch to it instead.",
                     "-b/-c creates a branch; without it, checkout/switch just moves to it.")
    if "no tracking information" in err:
        br = _git(e.cwd, "rev-parse", "--abbrev-ref", "HEAD") or "main"
        return _make("git_no_tracking", "no upstream", "this branch doesn't track a remote branch",
                     "git branch --set-upstream-to=origin/%s %s && %s" % (br, br, c), MEDIUM,
                     "git doesn't know which remote branch to pull from.",
                     "Set an upstream once; plain `git pull`/`git push` use it afterwards.")
    m = re.search(r"did you mean [`'\"](--?[\w-]+)[`'\"]", err)
    if m:
        bad = next((w for w in c.split() if w.lstrip("-") == m.group(1).lstrip("-")), None)
        if bad:
            return _make("git_flag_hint", "unknown option", "git says '%s' should be '%s'" % (bad, m.group(1)),
                         _swap(c, bad, m.group(1)), HIGH, "git suggested the right flag itself.",
                         "Long options take two dashes.")
    m = re.match(r"git\s+(checkout|switch)\s+([\w./-]+)\s*$", c)
    refs = _git_refs(e.cwd) if m else ()
    if m and re.search(r"did not match any file|invalid reference", err) and not _close_names(
            m.group(2), refs) and not any(r.startswith(m.group(2)) for r in refs):
        flag = "-b" if m.group(1) == "checkout" else "-c"
        return _make("git_new_branch", "no such branch", "there's no branch '%s' yet" % m.group(2),
                     "git %s %s %s" % (m.group(1), flag, m.group(2)), MEDIUM,
                     "Nothing in this repo is called '%s'." % m.group(2),
                     "Create and switch in one go with `git checkout -b` / `git switch -c`.")
    return None


def _npm_scripts(cwd: str) -> list:
    try:
        with open(os.path.join(cwd or ".", "package.json")) as f:
            return list((json.load(f).get("scripts") or {}).keys())
    except (OSError, ValueError, AttributeError):
        return []


def match_npm_script(e: FailureEvent) -> Optional[Diagnosis]:
    """`npm dev` (script exists, `run` missing) and `npm run tset`."""
    m = re.match(r"\s*(npm|yarn|pnpm)\s+(run\s+)?(\S+)(.*)$", e.command)
    if not m:
        return None
    tool, run, name, rest = m.groups()
    scripts = _npm_scripts(e.cwd)
    if not scripts or name in scripts and run:
        return None
    if not run:
        if name not in scripts or name in _tool_subcommands(tool):
            return None
        good = name
    else:
        close = _close_names(name, scripts, 1 if len(name) <= 3 else 2)
        if len(close) != 1:
            return None
        good = close[0]
    return _make("npm_script", "npm script", "'%s' is a script in package.json" % good,
                 "%s run %s%s" % (tool, good, rest), HIGH,
                 "package.json defines a '%s' script; scripts run via `%s run`." % (good, tool),
                 "Only a few scripts (start, test) work without `run`.")


def _module_candidates(name: str) -> list:
    """stdlib module names near `name`; for `http.serve`, submodules of http."""
    import importlib.util
    import pkgutil
    if "." in name:
        pkg, sub = name.rsplit(".", 1)
        try:
            spec = importlib.util.find_spec(pkg)
        except (ImportError, ValueError):
            return []
        subs = [m.name for m in pkgutil.iter_modules(spec.submodule_search_locations or [])] if spec else []
        return ["%s.%s" % (pkg, n) for n in _close_names(sub, subs, 2)]
    return _close_names(name, getattr(sys, "stdlib_module_names", ()), 2)


def match_java_go(e: FailureEvent) -> Optional[Diagnosis]:
    w = e.command.split()
    if len(w) < 2:
        return None
    if w[0] == "java" and w[-1].endswith(".class"):
        return _make("java_class", "class file name", "java takes a class name, not a .class file",
                     " ".join(w[:-1] + [w[-1][:-6]]), HIGH,
                     "`java` loads a class by name; the .class suffix makes it look for a class literally called 'Foo.class'.",
                     "javac takes files (Foo.java); java takes class names (Foo).")
    ext = {"javac": ".java", "go": ".go"}.get(w[0])
    target = w[2] if w[0] == "go" and len(w) > 2 and w[1] == "run" else (w[1] if w[0] == "javac" else None)
    if ext and target and "." not in target and os.path.exists(os.path.join(e.cwd or ".", target + ext)):
        return _make("missing_extension", "missing extension", "'%s' needs its %s extension" % (target, ext),
                     _swap(e.command, target, target + ext), HIGH,
                     "%s takes source files; '%s%s' is right here." % (w[0], target, ext),
                     "Compilers take file names with extensions.")
    return None


def match_python_module(e: FailureEvent) -> Optional[Diagnosis]:
    m = re.search(r"\bpython[\d.]*\s+-m\s+(\S+)", e.command)
    if not m:
        return None
    mod = m.group(1)
    if e.stderr.strip():
        if "No module named" not in e.stderr:
            return None
    else:
        import importlib.util
        try:
            if importlib.util.find_spec(mod):
                return None
        except (ImportError, ValueError):
            pass
    close = _module_candidates(mod)
    if len(close) != 1:
        return None
    return _make("python_module_typo", "unknown module", "'%s' isn't a module: did you mean '%s'?" % (mod, close[0]),
                 _swap(e.command, mod, close[0]), HIGH if e.stderr.strip() else MEDIUM,
                 "python -m runs a module by name; '%s' is the closest standard-library module." % close[0],
                 "Typos in `-m` names look like missing packages, but nothing needs installing.")



def match_not_git_repo(e: FailureEvent) -> Optional[Diagnosis]:
    if "not a git repository" not in e.stderr:
        return None
    return _make(
        "not_git_repo", "not a git repository",
        "you're not inside a git repository",
        "# cd into your project first, or start a new repo here: git init", MEDIUM,
        "git looked in this folder and every parent for a .git directory and found none.",
        "git commands work on the repository you're standing in, found by walking up to a .git folder.",
    )


def _error_line(stderr: str) -> str:
    """The line most worth showing: first one that reads like an error."""
    lines = [ln.strip() for ln in stderr.splitlines() if ln.strip()]
    for ln in lines:
        if re.search(r"error|fatal|not found|no such|denied|invalid|unknown|cannot|can't|failed", ln, re.I):
            return ln
    return lines[0] if lines else ""


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
    match_pasted_prompt,
    match_doubled_word,
    match_local_script,
    match_is_a_directory,
    match_git_shapes,
    match_python_module,
    match_java_go,
    match_npm_script,
    match_printed_fix,
    match_missing_file,
    match_git_ref_typo,
    match_git_push_rejected,
    match_merge_conflict,
    match_port_in_use,
    match_python_import,
    match_node_module_missing,
    match_missing_virtualenv,
    match_npm_install,
    match_brew_missing,
    match_unknown_subcommand,
    match_unknown_subcommand_listed,
    match_bad_flag,
    match_short_flag,
    match_missing_dash,
    match_bad_subverb,
    match_command_not_found,
    match_permission_denied,
    match_not_git_repo,
    match_did_you_mean_word,
    match_make_target,
    match_mkdir_parent,
    match_touch_parent,
    match_ln_order,
    match_quotes,
    match_cd_new_dir,
]
