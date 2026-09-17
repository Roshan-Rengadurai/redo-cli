"""The matcher: turn a FailureEvent into a Diagnosis.

Deliberately boring — walk the ordered rule list, take the first hit, else
fall back. Determinism is the whole point: the same failure always yields the
same fix, and every rule is independently unit-testable.
"""

from __future__ import annotations

from .models import Diagnosis, FailureEvent
from .rules import RULES, fallback


def diagnose(event: FailureEvent) -> Diagnosis:
    for rule in RULES:
        result = rule(event)
        if result is not None:
            return result
    return fallback(event)
