"""The matcher: turn a FailureEvent into a Diagnosis.

Deliberately boring — walk the ordered rule list, take the first hit, else
fall back. Determinism is the whole point: the same failure always yields the
same fix, and every rule is independently unit-testable.
"""

from __future__ import annotations

import dataclasses
import re

from .models import Confidence, Diagnosis, FailureEvent
from .rules import RULES, _edit_distance, changes_state, fallback, is_risky, match_history


def diagnose(event: FailureEvent) -> Diagnosis:
    result = next((d for d in (rule(event) for rule in RULES) if d), None)
    if result is None or result.confidence == Confidence.LOW:
        # nothing confident: your own history is the next-best dictionary
        result = match_history(event) or result
    return _guard(result, event.command) if result is not None else fallback(event)


def _verb_typed(fix: str, typed: str) -> bool:
    """Was the fix's verb (`upgrade` in `npm upgrade`) what the user was
    typing anyway? `urgrade`, `npminstall` yes; `runlint` -> unlink no."""
    verb = (fix.split()[1:2] or [""])[0]
    return bool(verb) and any(verb in w or _edit_distance(verb, w, 1) <= 1 for w in typed.split())


def _guard(d: Diagnosis, typed: str = "") -> Diagnosis:
    """One safety net for every rule: a fix with an unfilled `<placeholder>`
    is advice, not a command; a fix that deletes, moves, overwrites or kills
    is never high confidence and never auto-run."""
    if d.matched and re.search(r"<\w+>", d.fix_command) and not d.fix_command.startswith("#"):
        d = dataclasses.replace(d, fix_command="# " + d.fix_command, options=(),
                                confidence=Confidence.LOW)
    if (d.matched and d.confidence == Confidence.HIGH and typed
            and changes_state(d.fix_command) and not changes_state(typed)
            and not _verb_typed(d.fix_command, typed)):
        # the "fix" changes what the command does (`runlint` -> `unlink`)
        d = dataclasses.replace(d, confidence=Confidence.MEDIUM)
    if not d.matched or not any(map(is_risky, (d.fix_command,) + d.options)):
        return d
    conf = Confidence.MEDIUM if d.confidence == Confidence.HIGH else d.confidence
    return dataclasses.replace(d, confidence=conf, risky=True)
