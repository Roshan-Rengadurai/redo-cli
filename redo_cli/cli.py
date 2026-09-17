"""argparse front-end + rendering.

Commands: run / fix / explain / learn / history / stats. `fix` is the headline
(bare `redo` runs it); everything else is built around the same diagnose() call. Shell integration
(`redo init`) lands in Stage 2 — until then `redo run <cmd>` is the capture
path.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
import time
from typing import Optional

from . import __version__
from . import colors as c
from . import shell
from .engine import diagnose
from .models import Confidence, Diagnosis, FailureEvent
from .storage import Storage, default_db_path, home_dir, spool_path

_CONF_COLOR = {
    Confidence.HIGH: c.green,
    Confidence.MEDIUM: c.yellow,
    Confidence.LOW: c.red,
}


# --------------------------------------------------------------- rendering

def _no_failures() -> str:
    return (
        "No failures captured yet.\n"
        "Wrap a command to record one:\n"
        "  " + c.cyan("redo run <command>")
    )


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
    if "\n" in fix or fix.startswith("#"):
        return None                      # multi-step or advice-only: show, don't push
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


def _write_push(mode: str, command: str) -> bool:
    path = os.environ.get("REDO_PUSH_FILE")
    if not path:
        return False
    with open(path, "w") as f:
        f.write(mode + "\n" + command + "\n")
    return True


def _push_footer(mode: str, rerun: bool) -> str:
    bash = os.environ.get("REDO_SHELL") == "bash"
    if mode == "run":
        return c.dim("running the fix...")
    what = "re-run it with output captured" if rerun else "run the fix"
    if bash:
        return c.dim("\u2191  press up to load it, then enter to %s" % what)
    return c.dim("\u21b5  it's on your prompt -- press enter to %s" % what)


def _print_fix(event: FailureEvent, d: Diagnosis, footer: str = "") -> None:
    lines = [c.bold("You ran:"), "  " + c.cyan(event.command), ""]
    if d.matched:
        lines += [c.bold("Failure:"), "  " + d.summary, ""]
        color = _CONF_COLOR[d.confidence]
        lines.append(c.bold("Fix ") + color("(%s confidence):" % d.confidence.label()))
        for ln in d.fix_command.splitlines():
            if ln.strip().startswith("#"):
                lines.append("  " + c.dim(ln))
            else:
                lines.append("  " + c.green(ln))
        lines += ["", c.dim("why? -> redo explain      learn? -> redo learn")]
    else:
        lines += [
            c.bold("Failure detected, but no exact match was found."),
            "",
            "Likely causes:",
            c.dim("  - missing dependency"),
            c.dim("  - environment mismatch"),
            c.dim("  - incorrect command usage"),
            "",
            c.bold("Suggested next step:"),
        ]
        if event.stderr.strip():
            lines.append("  " + c.green("redo explain"))
        else:
            lines.append("  " + c.green(_rerun_command(event)))
            lines.append(c.dim("  (no output was captured; re-running with "
                               "`redo run` gets the full diagnosis)"))
    if footer:
        lines.append(footer)
    print("\n".join(lines))


# ---------------------------------------------------------------- commands

def cmd_run(args: argparse.Namespace) -> int:
    parts = list(args.command)
    if parts and parts[0] == "--":
        parts = parts[1:]
    if not parts:
        print("usage: redo run <command> [args...]", file=sys.stderr)
        return 2

    command = shlex.join(parts)
    shell = os.environ.get("SHELL", "/bin/sh")

    # stdout/stdin are inherited (stay interactive); stderr is teed so we both
    # show it live and keep a copy to diagnose.
    proc = subprocess.Popen([shell, "-c", command], stderr=subprocess.PIPE, text=True)
    captured = []
    if proc.stderr is not None:
        for line in proc.stderr:
            sys.stderr.write(line)
            captured.append(line)
    proc.wait()
    code = proc.returncode

    if code != 0:
        store = Storage(default_db_path())
        store.record(FailureEvent(
            id=None,
            command=command,
            exit_code=code,
            stderr="".join(captured).strip(),
            cwd=os.getcwd(),
            timestamp=time.time(),
            shell=os.path.basename(shell),
        ))
        store.close()
        print(c.dim("\n  redo: captured failure (exit %d) -- run `redo fix`" % code),
              file=sys.stderr)
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


def cmd_fix(args: argparse.Namespace) -> int:
    event = _latest()
    if event is None:
        print(_no_failures())
        return 0
    d = diagnose(event)

    footer = ""
    if not getattr(args, "no_push", False):
        mode = "run" if getattr(args, "yes", False) else "push"
        fix = _pushable_fix(d)
        rerun = False
        if fix is None and not d.matched and not event.stderr.strip():
            fix, mode, rerun = _rerun_command(event), "push", True
        if fix is not None and _write_push(mode, fix):
            footer = _push_footer(mode, rerun)
    _print_fix(event, d, footer)
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    event = _latest()
    if event is None:
        print(_no_failures())
        return 0
    d = diagnose(event)
    print(c.bold("Command:"))
    print("  " + c.cyan(event.command))
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
        print(e.command)
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
        rc = shell.rc_path(args.shell or shell.detect_shell())
    except shell.UnsupportedShell as e:
        print("redo: unsupported shell %s -- pass --shell zsh|bash|fish, or use "
              "--print and add the line yourself" % e, file=sys.stderr)
        return 1

    if args.print:
        for n in names:
            print(shell.alias_line(n))
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
            status = (shell.remove_alias if args.remove else shell.add_alias)(n, rc)
        except ValueError as e:
            print("redo: %s" % e, file=sys.stderr)
            rc_code = 1
            continue
        if status == "added":
            print(c.green("+ ") + shell.alias_line(n) + c.dim("  -> %s" % rc))
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
        print("Reload your shell to use it:  " + c.cyan("source %s" % rc))
    return rc_code


def cmd_init(args: argparse.Namespace) -> int:
    sh = args.shell or shell.detect_shell()
    try:
        rc = shell.rc_path(sh)
        if args.print:
            print(shell.hook_source(sh, spool_path()), end="")
            return 0
    except shell.UnsupportedShell as e:
        print("redo: unsupported shell %s -- pass --shell zsh|bash|fish" % e,
              file=sys.stderr)
        return 1
    hook = shell.hook_path(sh, home_dir())

    if args.remove:
        if shell.remove_init(rc, hook) == "removed":
            print(c.yellow("- ") + "removed redo hook" + c.dim("  (from %s)" % rc))
            print("Open a new shell for it to take effect.")
        else:
            print(c.dim("= redo hook is not installed in %s" % rc))
        return 0

    status = shell.add_init(sh, rc, hook, spool_path())
    if status == "added":
        print(c.green("+ ") + shell.source_line(hook) + c.dim("  -> %s" % rc))
    else:
        print(c.dim("= hook already in %s; regenerated %s" % (rc, hook)))
    print()
    print("Reload your shell to start capturing:  " + c.cyan("source %s" % rc))
    print("Then, after a command fails, just type  " + c.cyan("redo"))
    return 0


# ----------------------------------------------------------------- parser

def _add_fix_flags(p: argparse.ArgumentParser, top: bool) -> None:
    # On the sub-parser the defaults are SUPPRESS so `redo fix` doesn't
    # clobber values parsed at the top level (`redo -y`).
    dflt = False if top else argparse.SUPPRESS
    p.add_argument("-y", "--yes", action="store_true", default=dflt,
                   help="run the fix immediately instead of placing it on the prompt")
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
    return func(args)
