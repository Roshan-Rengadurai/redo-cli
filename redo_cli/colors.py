"""Tiny ANSI helper.

Colour is enabled only when stdout is a real TTY and the user hasn't opted
out via NO_COLOR / TERM=dumb. Decided once at import so the hot path stays
branch-cheap, and so piped/captured output (tests, `| less`) is plain text.
"""

from __future__ import annotations

import os
import sys

_ENABLED = (
    hasattr(sys.stdout, "isatty")
    and sys.stdout.isatty()
    and os.environ.get("NO_COLOR") is None
    and os.environ.get("TERM") != "dumb"
    # classic Windows consoles print raw escapes; Windows Terminal / VS Code don't
    and (sys.platform != "win32" or bool(os.environ.get("WT_SESSION") or os.environ.get("TERM_PROGRAM")))
)


def _style(code: str):
    def apply(text: str) -> str:
        if not _ENABLED:
            return text
        return "\033[%sm%s\033[0m" % (code, text)

    return apply


bold = _style("1")
dim = _style("2")
red = _style("31")
green = _style("32")
yellow = _style("33")
cyan = _style("36")
