"""argparse front-end + rendering.

Commands: run / fix / explain / learn / history / stats. `fix` is the headline
(bare `redo` runs it); everything else is built around the same diagnose() call. Shell integration
(`redo init`) lands in Stage 2 — until then `redo run <cmd>` is the capture
path.
"""

from __future__ import annotations

import argparse
import sqlite3
import os
import re
import shlex
import sys
import time
from typing import Optional

from . import __version__
from . import colors as c
from . import shell
from . import shell as shell_mod
from .engine import diagnose
from .rules import _command_words, _error_line, changes_state, is_risky
from .models import Confidence, Diagnosis, FailureEvent
from .storage import (
    Storage,
    is_benign,
    default_db_path,
    home_dir,
    spool_path,
)

_CONF_COLOR = {
    Confidence.HIGH: c.green,
    Confidence.MEDIUM: c.yellow,
    Confidence.LOW: c.red,
}


# --------------------------------------------------------------- rendering

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _safe(text: str) -> str:
    """Escape/control bytes from a captured command must not drive the terminal."""
    return _CONTROL.sub("?", text)


def _no_failures() -> str:
    if os.environ.get("REDO_PUSH_FILE"):   # running via the hook's function
        return ("No failures captured yet. When a command fails, type "
                + c.cyan("redo") + ".")
    return (
        "No failures captured yet.\n"
        "Install the shell hook to capture them automatically:  " + c.cyan("redo init") + "\n"
        "or wrap one command:  " + c.cyan("redo run <command>")
    )


def _ago(timestamp: float) -> str:
    """Human-scale age, so a stale capture (e.g. no shell hook installed --
    only `redo run` and the hook actually record a failure) reads as stale
    instead of looking like it tracked a command it never saw."""
    s = time.time() - timestamp
    if s < 5:
        return "just now"
    if s < 90:
        return "%ds ago" % int(s)
    m = s / 60
    if m < 90:
        return "%dm ago" % int(m)
    h = m / 60
    if h < 36:
        return "%dh ago" % int(h)
    return "%dd ago" % int(h / 24)


# ------------------------------------------------------------ push-to-prompt
#
# Under the `redo` shell function (installed by `redo init`) the binary hands
# a command back through $REDO_PUSH_FILE as "<mode>\n<command>\n"; the
# function puts it on the prompt ("push") or evals it ("run").

_TRAILING_COMMENT = re.compile(r"\s+#.*$")


def _pushable_fix(d: Diagnosis) -> Optional[str]:
    if not d.matched:
        return None
    fix = d.fix_command.strip()
    if any(ln.strip().startswith("#") for ln in fix.splitlines()):
        return None                      # advice, not a command: show, don't push
    return _TRAILING_COMMENT.sub("", fix) or None


def _rerun_command(event: FailureEvent) -> str:
    """`redo run <cmd>` for a hook-captured failure we couldn't diagnose
    without output. Quoted as one arg when the command has shell syntax."""
    cmd = event.command.strip()
    try:
        plain = shlex.join(shlex.split(cmd)) == cmd
    except ValueError:
        plain = False
    return "redo run " + (cmd if plain else shlex.quote(cmd))


def _safe_to_rerun(command: str) -> bool:
    """Only suggest re-running things that don't change state; `cd` would
    run in a subshell and tell us nothing."""
    return not (is_risky(command) or changes_state(command)
                or "cd" in _command_words(command))


def _was_run(event: FailureEvent) -> bool:
    """Captured by `redo run` (output was recorded, even if empty), as
    opposed to the shell hook, which never sees output."""
    return event.shell.startswith("run:")


def _write_push(mode: str, command: str) -> bool:
    path = os.environ.get("REDO_PUSH_FILE")
    if not path:
        return False
    with open(path, "w") as f:
        f.write(mode + "\n" + command + "\n")
    return True


def _push_footer(mode: str, rerun: bool) -> str:
    bash = os.environ.get("REDO_SHELL") in ("bash", "pwsh")   # fix goes to history: press up
    if mode == "run":
        return c.dim("running the fix...")
    if mode == "hist":
        return c.dim("\u2191  it changes or deletes data, so it isn't pre-filled: press up to load it")
    what = "re-run it with output captured" if rerun else "run the fix"
    if bash:
        return c.dim("\u2191  press up to load it, then enter to %s" % what)
    return c.dim("\u21b5  it's on your prompt, press enter to %s" % what)


def _print_fix(event: FailureEvent, d: Diagnosis, footer: str = "") -> None:
    lines = _header(event)
    if d.matched:
        lines += [c.bold("Failure:"), "  " + d.summary, ""]
        color = _CONF_COLOR[d.confidence]
        lines.append(c.bold("Fix ") + color("(%s confidence):" % d.confidence.label())
                     + (c.red("  careful: changes or deletes data") if d.risky else ""))
        for ln in d.fix_command.splitlines():
            if ln.strip().startswith("#"):
                lines.append("  " + c.dim(ln))
            else:
                lines.append("  " + c.green(ln))
        lines += ["", c.dim("why? -> redo explain      learn? -> redo learn")]
    else:
        err = _error_line(event.stderr)
        if err:
            # Say what actually broke instead of three canned guesses.
            lines += [
                c.bold("No rule matched. The error was:"),
                "  " + c.yellow(_safe(err)),
                "",
                c.dim("exit code %d -- start from that line" % event.exit_code),
            ]
        elif _was_run(event):
            lines += [
                c.bold("It failed (exit %d) without printing an error, and no "
                       "rule matched." % event.exit_code),
            ]
        elif not _safe_to_rerun(event.command):
            lines += [
                c.bold("Failure detected (exit %d), but no rule matched." % event.exit_code),
                c.dim("The hook doesn't capture output, and this command changes state, "
                      "so redo won't suggest re-running it."),
            ]
        else:
            lines += [
                c.bold("Failure detected, but no exact match was found."),
                "",
                c.bold("Suggested next step:"),
                "  " + c.green(_rerun_command(event)),
                c.dim("  (the hook doesn't capture output; re-running with "
                      "`redo run` records it, which may let redo be more "
                      "specific -- only re-run it if it's safe to repeat)"),
            ]
    if footer:
        lines.append(footer)
    print("\n".join(lines))


# ---------------------------------------------------------------- commands

def _is_ps(shell: str) -> bool:
    return os.path.splitext(os.path.basename(shell))[0].lower() in shell_mod.POWERSHELLS


def _run_shell() -> str:
    """The shell `redo run` uses: the caller's PowerShell when the hook's
    function says so (or on Windows, where $SHELL is unset), else $SHELL."""
    import shutil
    if os.environ.get("REDO_SHELL") in shell_mod.POWERSHELLS or (
            sys.platform == "win32" and not os.environ.get("SHELL")):
        return shutil.which("pwsh") or shutil.which("powershell") or "powershell"
    return os.environ.get("SHELL") or "/bin/sh"


def _pipeline_check(shell: str) -> str:
    """Appended to a `redo run` line: exit 127 if any pipeline stage wasn't
    found (`gti log | head` otherwise exits 0), else keep the real status."""
    name = os.path.basename(shell)
    if name == "zsh":
        return '\n__s=(${pipestatus[@]}); (( ${__s[(I)127]} )) && exit 127; exit ${__s[-1]}'
    if name == "bash":
        return ('\n__s=("${PIPESTATUS[@]}"); for __c in "${__s[@]}"; do '
                '[ "$__c" = 127 ] && exit 127; done; exit "${__s[${#__s[@]}-1]}"')
    return ""


def cmd_run(args: argparse.Namespace) -> int:
    import subprocess   # only `run` needs these; keep bare `redo` startup lean
    import time

    parts = list(args.command)
    if parts and parts[0] == "--":
        parts = parts[1:]
    if not parts:
        print("usage: redo run <command> [args...]", file=sys.stderr)
        return 2

    # One argument means the user (or a pushed `redo run '<cmd>'`) already
    # quoted a whole shell line: run it verbatim so pipes/redirects work.
    command = parts[0] if len(parts) == 1 else shlex.join(parts)
    shell = _run_shell()

    # stdout/stdin are inherited (stay interactive); stderr is teed so we both
    # show it live and keep a copy to diagnose.
    # Only pipelines need the check; anything else keeps redo's own code out
    # of the user's syntax errors.
    suffix = _pipeline_check(shell) if "|" in command else ""
    argv = ([shell, "-NoProfile", "-Command", command] if _is_ps(shell)
            else [shell, "-c", command + suffix])
    proc = subprocess.Popen(argv,
                            stderr=subprocess.PIPE, text=True, errors="replace")
    captured = []
    if proc.stderr is not None:
        echo = True
        for line in proc.stderr:
            captured.append(line)
            if echo:
                try:
                    sys.stderr.write(line)
                except BrokenPipeError:   # `redo run x 2>&1 | head`: keep recording
                    echo = False
    proc.wait()
    code = proc.returncode
    if code < 0:                          # killed by signal N -> shell's 128+N
        code = 128 - code

    event = FailureEvent(
        id=None,
        command=command,
        exit_code=code,
        stderr="".join(captured).strip(),
        cwd=os.getcwd(),
        timestamp=time.time(),
        shell="run:" + os.path.splitext(os.path.basename(shell))[0],
    )
    if code != 0 and not is_benign(event):
        try:
            store = Storage(default_db_path())
            store.record(event)
            store.close()
        except (sqlite3.DatabaseError, OSError) as e:   # never mask the real exit code
            print("redo: couldn't record the failure (%s)" % e, file=sys.stderr)
            return code
        try:
            print(c.dim("\n  redo: captured failure (exit %d): run `redo`" % code),
                  file=sys.stderr)
        except BrokenPipeError:
            pass
    return code


def _store() -> Storage:
    """Open the db and fold in anything the shell hook spooled since last time."""
    store = Storage(default_db_path())
    store.ingest_spool(spool_path())
    return store


def _latest():
    store = _store()
    event = store.latest()
    store.close()
    return event


def _interactive_pick(options) -> Optional[int]:
    """Arrow-key / vim-binding (j/k) picker confined to len(options) lines --
    no alternate screen, no full-window takeover. Redraws in place using
    plain ANSI cursor movement. Returns None if the user backs out."""
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    idx = 0
    n = len(options)

    def render(first: bool) -> None:
        if not first:
            sys.stdout.write("\033[%dA" % n)
        for i, opt in enumerate(options):
            mark = c.green("> ") if i == idx else "  "
            sys.stdout.write("\033[2K" + mark + (c.bold(opt) if i == idx else opt) + "\r\n")
        sys.stdout.flush()

    # TCSANOW, not the setcbreak default (TCSAFLUSH): flushing/draining can
    # hang indefinitely on some ptys (macOS in particular) if the far end
    # isn't reading at that exact instant.
    tty.setcbreak(fd, termios.TCSANOW)
    try:
        render(first=True)
        while True:
            ch = sys.stdin.read(1)
            if ch == "\x1b":
                if sys.stdin.read(1) == "[":
                    arrow = sys.stdin.read(1)
                    idx = {"A": (idx - 1) % n, "B": (idx + 1) % n}.get(arrow, idx)
                else:
                    return None
            elif ch in "kK":
                idx = (idx - 1) % n
            elif ch in "jJ":
                idx = (idx + 1) % n
            elif ch in "\r\n":
                return idx
            elif ch.isdigit() and 1 <= int(ch) <= n:
                return int(ch) - 1
            elif ch in "q\x03":
                return None
            render(first=False)
    finally:
        # TCSANOW, not TCSADRAIN: draining can block forever on some ptys
        # when nothing is reading the far end (e.g. under a test harness).
        termios.tcsetattr(fd, termios.TCSANOW, old)


def _list(options) -> None:
    for i, opt in enumerate(options, 1):
        print("  %d) %s" % (i, c.green(opt)))


def _pick_option(d: Diagnosis) -> Optional[str]:
    """Inline arrow-key/j-k/number picker on a real terminal; otherwise the
    list plus option 1 (it goes on the prompt, so it's still editable)."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(c.bold("Did you mean:"))
        _list(d.options)
        print()
        return d.options[0]
    print(c.bold("Did you mean:") + c.dim("  (\u2191/\u2193, j/k or 1-9, enter to pick, q to cancel)"))
    try:
        idx = _interactive_pick(d.options)
    except KeyboardInterrupt:
        idx = None
    except Exception:            # no termios (e.g. Windows)
        _list(d.options)
        return d.options[0]
    print()
    return None if idx is None else d.options[idx]


def _stale(event: FailureEvent) -> bool:
    """The hook creates $REDO_HOME/ok on success and deletes it on failure,
    so it exists exactly when the last command succeeded. (The mtime check
    keeps a `redo run` failure, which the hook never sees, from looking stale.)"""
    try:
        return (home_dir() / "ok").stat().st_mtime >= event.timestamp
    except OSError:
        return False


def _header(event: FailureEvent) -> list:
    return [c.bold("You ran:") + c.dim("  (%s)" % _ago(event.timestamp)),
            "  " + c.cyan(_safe(event.command)), ""]


def cmd_fix(args: argparse.Namespace) -> int:
    event = _latest()
    if event is None:
        print(_no_failures())
        return 0
    d = diagnose(event)
    if _stale(event):
        # the hook saw a successful command after this failure (maybe its fix)
        print(c.dim("Your last command succeeded. Most recent failure, %s:" % _ago(event.timestamp)))
        args.no_push = True

    if len(d.options) > 1 and getattr(args, "no_push", False):
        print("\n".join(_header(event) + [c.bold("Failure:"), "  " + d.summary, "",
                                          c.bold("Did you mean:")]))
        _list(d.options)
        return 0

    if len(d.options) > 1:
        print("\n".join(_header(event) + [c.bold("Failure:"), "  " + d.summary, ""]))
        if getattr(args, "yes", False):   # several candidates: a human picks
            print(c.dim("more than one candidate, so -y won't run one blindly"))
        if d.risky:
            print(c.red("careful: ") + "these change or delete data -- check before running")
        picked = _pick_option(d)
        if picked is None:
            print(c.dim("cancelled"))
            return 1
        mode = "hist" if d.risky else "push"
        if not getattr(args, "no_push", False) and _write_push(mode, picked):
            print(_push_footer(mode, rerun=False))
        print(c.dim("why? -> redo explain      learn? -> redo learn"))
        return 0

    footer = ""
    if not getattr(args, "no_push", False):
        # risky fixes are never pre-filled: whatever you type next would be
        # appended to `rm -r src`. They go to history (up-arrow) instead.
        mode = "hist" if d.risky else "run" if getattr(args, "yes", False) else "push"
        fix = _pushable_fix(d)
        rerun = False
        if (fix is None and not d.matched and not event.stderr.strip()
                and not _was_run(event) and _safe_to_rerun(event.command)):
            fix, mode, rerun = _rerun_command(event), "push", True
        if fix is not None and _write_push(mode, fix):
            footer = _push_footer(mode, rerun)
    _print_fix(event, d, footer)
    if getattr(args, "yes", False) and not os.environ.get("REDO_PUSH_FILE"):
        print(c.dim("(-y runs the fix through the shell function; install it "
                    "with `redo init`. Nothing was run.)"))
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    event = _latest()
    if event is None:
        print(_no_failures())
        return 0
    d = diagnose(event)
    print(c.bold("Command:"))
    print("  " + c.cyan(_safe(event.command)))
    print()
    print(c.bold("What happened:"))
    for ln in d.explanation.splitlines():
        print(("  " + ln) if ln else "")
    print()
    print(c.bold("Confidence: ") + _CONF_COLOR[d.confidence](d.confidence.label()))
    return 0


def cmd_learn(args: argparse.Namespace) -> int:
    event = _latest()
    if event is None:
        print(_no_failures())
        return 0
    d = diagnose(event)
    print(c.bold("Concept:"))
    for ln in d.lesson.splitlines():
        print(("  " + ln) if ln else "")
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    store = _store()
    events = store.recent(args.limit)
    store.close()
    if not events:
        print(_no_failures())
        return 0
    for e in events:
        print(c.dim("%8s  exit %-3d " % (_ago(e.timestamp), e.exit_code)) + e.command)
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    store = _store()
    events = store.recent(args.limit)
    store.close()
    if not events:
        print(_no_failures())
        return 0
    counts = {}
    for e in events:
        title = diagnose(e).title
        counts[title] = counts.get(title, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    print(c.bold("Your most common issues:"))
    print()
    for title, n in ranked:
        print("  %s  %s" % (c.yellow("%2d" % n), title))
    return 0


def cmd_alias(args: argparse.Namespace) -> int:
    names = list(args.names)
    try:
        sh = args.shell or shell.detect_shell()
        rc = shell.rc_path(sh)
    except shell.UnsupportedShell as e:
        print("redo: unsupported shell %s: pass --shell zsh|bash|fish|pwsh, or use "
              "--print and add the line yourself" % e, file=sys.stderr)
        return 1

    if args.print:
        for n in names:
            print(shell.alias_line(n, sh))
        return 0

    if not names:
        installed = shell.list_aliases(rc)
        if installed:
            print(c.bold("redo aliases in %s:" % rc))
            for n in installed:
                print("  " + c.cyan(n))
        else:
            print("No redo aliases in %s." % rc)
            print("Add one:  " + c.cyan("redo alias r"))
        return 0

    rc_code = 0
    changed = False
    for n in names:
        try:
            status = (shell.remove_alias if args.remove else shell.add_alias)(n, rc, sh)
        except ValueError as e:
            print("redo: %s" % e, file=sys.stderr)
            rc_code = 1
            continue
        if status == "added":
            print(c.green("+ ") + shell.alias_line(n, sh) + c.dim("  -> %s" % rc))
            changed = True
        elif status == "removed":
            print(c.yellow("- ") + "alias %s" % n + c.dim("  (from %s)" % rc))
            changed = True
        elif status == "exists":
            print(c.dim("= alias %s already installed" % n))
        elif status == "missing":
            print(c.dim("= alias %s not installed by redo, nothing to remove" % n))
        elif status == "conflict":
            print(c.red("! alias %s is already defined in %s (not by redo); "
                        "pick another name" % (n, rc)), file=sys.stderr)
            rc_code = 1
    if changed:
        print()
        print("Reload your shell to use it:  " + c.cyan(_reload_cmd(sh, rc)))
    return rc_code


def _reload_cmd(sh: str, rc) -> str:
    return ". $PROFILE" if sh in shell_mod.POWERSHELLS else "source %s" % rc


def cmd_init(args: argparse.Namespace) -> int:
    sh = args.shell or shell.detect_shell()
    try:
        rc = shell.rc_path(sh)
        if args.print:
            print(shell.hook_source(sh, spool_path()), end="")
            return 0
    except shell.UnsupportedShell as e:
        print("redo: unsupported shell %s: pass --shell zsh|bash|fish|pwsh|powershell" % e,
              file=sys.stderr)
        return 1
    hook = shell.hook_path(sh, home_dir())

    if args.remove:
        if sh == "bash":
            shell.remove_init(rc.with_name(".bashrc"), hook)
        if shell.remove_init(rc, hook, sh) == "removed":
            print(c.yellow("- ") + "removed redo hook" + c.dim("  (from %s)" % rc))
            print("Open a new shell for it to take effect.")
        else:
            print(c.dim("= redo hook is not installed in %s" % rc))
        return 0

    status = shell.add_init(sh, rc, hook, spool_path())
    if sh == "bash" and sys.platform == "darwin":   # non-login bash reads only .bashrc
        shell.add_init(sh, rc.with_name(".bashrc"), hook, spool_path())
    if status == "added":
        print(c.green("+ ") + shell.source_line(hook, sh) + c.dim("  -> %s" % rc))
    else:
        print(c.dim("= hook already in %s; regenerated %s" % (rc, hook)))
    print()
    print("Reload your shell to start capturing:  " + c.cyan(_reload_cmd(sh, rc)))
    print("Then, after a command fails, just type  " + c.cyan("redo"))
    return 0


# ----------------------------------------------------------------- parser

def _add_fix_flags(p: argparse.ArgumentParser, top: bool) -> None:
    # On the sub-parser the defaults are SUPPRESS so `redo fix` doesn't
    # clobber values parsed at the top level (`redo -y`).
    dflt = False if top else argparse.SUPPRESS
    p.add_argument("-y", "--yes", action="store_true", default=dflt,
                   help="run the fix immediately instead of placing it on the prompt "
                        "(needs the shell function from `redo init`)")
    p.add_argument("--no-push", action="store_true", default=dflt,
                   help="only print the fix; don't touch the prompt")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="redo",
        description="Explain why the last command failed and suggest a direct fix.",
    )
    p.add_argument("--version", action="version", version="redo %s" % __version__)
    _add_fix_flags(p, top=True)
    sub = p.add_subparsers(dest="cmd")

    pr = sub.add_parser("run", help="run a command and record it if it fails")
    pr.add_argument("command", nargs=argparse.REMAINDER,
                    help="the command to run, e.g. redo run npm run dev")
    pr.set_defaults(func=cmd_run)

    pf = sub.add_parser("fix", help="suggest a fix for the most recent failure")
    _add_fix_flags(pf, top=False)
    pf.set_defaults(func=cmd_fix)
    sub.add_parser("explain", help="explain the root cause").set_defaults(func=cmd_explain)
    sub.add_parser("learn", help="learn the underlying concept").set_defaults(func=cmd_learn)

    ph = sub.add_parser("history", help="show recent failed commands")
    ph.add_argument("-n", "--limit", type=int, default=10)
    ph.set_defaults(func=cmd_history)

    ps = sub.add_parser("stats", help="show your most common failures")
    ps.add_argument("-n", "--limit", type=int, default=500)
    ps.set_defaults(func=cmd_stats)

    pa = sub.add_parser("alias", help="add a shell alias for redo, e.g. `redo alias r`")
    pa.add_argument("names", nargs="*", help="alias name(s); none = list installed")
    pa.add_argument("--remove", action="store_true", help="remove instead of add")
    pa.add_argument("--print", action="store_true",
                    help="print the alias line(s) without touching any file")
    pa.add_argument("--shell", choices=shell.SUPPORTED,
                    help="override $SHELL detection")
    pa.set_defaults(func=cmd_alias)

    pi = sub.add_parser("init", help="install the shell hook so failures are captured automatically")
    pi.add_argument("--remove", action="store_true", help="uninstall the hook")
    pi.add_argument("--print", action="store_true", help="print the hook source instead of installing")
    pi.add_argument("--shell", choices=shell.SUPPORTED, help="override $SHELL detection")
    pi.set_defaults(func=cmd_init)

    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # Bare `redo` is the headline use: it *is* `redo fix`. Help stays on -h.
    func = getattr(args, "func", cmd_fix)
    try:
        return func(args)
    except KeyboardInterrupt:
        return 130
    except (sqlite3.DatabaseError, OSError) as e:
        # corrupt db, unwritable $REDO_HOME, ... -- one line, no traceback
        print("redo: can't use %s (%s)" % (home_dir(), e), file=sys.stderr)
        return 1
