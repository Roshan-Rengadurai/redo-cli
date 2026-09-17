"""Shell integration: rc-file detection and `redo alias` management.

Everything that touches the user's shell config lives here so it has one
owner. Lines we write carry a trailing ``# redo-cli`` marker; that marker is
how we recognise our own entries on later runs (idempotent add, safe remove,
list) and how we avoid clobbering an alias the user defined by hand.

Stage 2's ``redo init`` hook will be managed the same way.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import List, Mapping, Optional

MARKER = "# redo-cli"
SUPPORTED = ("zsh", "bash", "fish")

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


class UnsupportedShell(Exception):
    pass


# ---------------------------------------------------------------- detection

def detect_shell(env: Optional[Mapping[str, str]] = None) -> str:
    """Basename of $SHELL ('' when unset)."""
    env = os.environ if env is None else env
    return os.path.basename(env.get("SHELL", ""))


def rc_path(shell: str, home: Optional[Path] = None, platform: Optional[str] = None) -> Path:
    home = Path(home) if home is not None else Path.home()
    platform = platform or sys.platform
    if shell == "zsh":
        return home / ".zshrc"
    if shell == "bash":
        # macOS terminals open login shells, which read .bash_profile and
        # usually never .bashrc.
        return home / (".bash_profile" if platform == "darwin" else ".bashrc")
    if shell == "fish":
        return home / ".config" / "fish" / "config.fish"
    raise UnsupportedShell(shell or "(unset)")


# ------------------------------------------------------------------ aliases

def alias_line(name: str) -> str:
    # `alias NAME=DEF` is accepted by zsh, bash and fish alike.
    return "alias %s='redo'  %s" % (name, MARKER)


def _validate(name: str) -> None:
    if not _NAME_RE.match(name):
        raise ValueError("invalid alias name: %r" % name)


def _read(rc: Path) -> str:
    return rc.read_text() if rc.exists() else ""


def _foreign_alias_re(name: str) -> "re.Pattern[str]":
    return re.compile(r"^\s*alias\s+%s(=|\s)" % re.escape(name), re.M)


def add_alias(name: str, rc: Path) -> str:
    """Append our alias line. Returns 'added' | 'exists' | 'conflict'."""
    _validate(name)
    text = _read(rc)
    if alias_line(name) in text.splitlines():
        return "exists"
    if _foreign_alias_re(name).search(text):
        return "conflict"
    rc.parent.mkdir(parents=True, exist_ok=True)
    with rc.open("a") as f:
        if text and not text.endswith("\n"):
            f.write("\n")
        f.write(alias_line(name) + "\n")
    return "added"


def remove_alias(name: str, rc: Path) -> str:
    """Drop our alias line (never a user-written one). Returns 'removed' | 'missing'."""
    _validate(name)
    lines = _read(rc).splitlines(keepends=True)
    kept = [ln for ln in lines if ln.rstrip("\n") != alias_line(name)]
    if len(kept) == len(lines):
        return "missing"
    rc.write_text("".join(kept))
    return "removed"


def list_aliases(rc: Path) -> List[str]:
    pat = re.compile(r"^alias\s+([A-Za-z_][A-Za-z0-9_-]*)='redo'\s+%s$" % re.escape(MARKER))
    out = []
    for ln in _read(rc).splitlines():
        m = pat.match(ln)
        if m:
            out.append(m.group(1))
    return out
