"""Core data types shared across the engine.

Two structs do all the work:

* ``FailureEvent`` — what we captured (the raw fact). This is what lives in
  the SQLite history and is the single source of truth.
* ``Diagnosis``    — what a rule concluded (always derived, never stored).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Confidence(Enum):
    HIGH = "high"      # direct and safe fix
    MEDIUM = "medium"  # likely fix, but context dependent
    LOW = "low"        # multiple possible causes

    def label(self) -> str:
        return self.value


@dataclass
class FailureEvent:
    """A single captured command failure."""

    id: Optional[int]
    command: str
    exit_code: int
    stderr: str
    cwd: str
    timestamp: float
    shell: str = ""


@dataclass
class Diagnosis:
    """The result of running the matcher over a FailureEvent."""

    rule_id: str          # stable id, "" for the fallback
    title: str            # short category label (used by `stats`)
    summary: str          # the one-line "Failure:" string
    fix_command: str      # the suggested recovery (may be multi-line)
    confidence: Confidence
    explanation: str      # root-cause reasoning, for `redo explain`
    lesson: str           # the concept, for `redo learn`
    matched: bool = True  # False == fallback, no rule fired
    options: tuple = ()   # 2+ candidate fixes -> `redo fix` prompts a pick
    risky: bool = False   # fix destroys/overwrites data: never auto-run
