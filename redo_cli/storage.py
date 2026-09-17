"""SQLite-backed history of failed commands.

The store holds *only raw events*. Diagnoses are never persisted — `stats`,
`explain` and `learn` all re-run the matcher over these rows, so there is a
single source of truth and nothing to migrate when rules change.

Location: ``~/.redo/history.db`` (override the parent dir with $REDO_HOME,
which is also how the test suite avoids touching a real home directory).
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import List, Optional

from .models import FailureEvent


def default_db_path() -> Path:
    home = os.environ.get("REDO_HOME")
    base = Path(home).expanduser() if home else Path.home() / ".redo"
    return base / "history.db"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS failures (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    command    TEXT    NOT NULL,
    exit_code  INTEGER NOT NULL,
    stderr     TEXT    NOT NULL DEFAULT '',
    cwd        TEXT    NOT NULL DEFAULT '',
    shell      TEXT    NOT NULL DEFAULT '',
    timestamp  REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_failures_ts ON failures(timestamp DESC);
"""


class Storage:
    def __init__(self, path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def record(self, event: FailureEvent) -> int:
        cur = self._conn.execute(
            "INSERT INTO failures (command, exit_code, stderr, cwd, shell, timestamp)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (event.command, event.exit_code, event.stderr,
             event.cwd, event.shell, event.timestamp),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def latest(self) -> Optional[FailureEvent]:
        row = self._conn.execute(
            "SELECT * FROM failures ORDER BY timestamp DESC, id DESC LIMIT 1"
        ).fetchone()
        return _row_to_event(row) if row else None

    def recent(self, limit: int = 10) -> List[FailureEvent]:
        rows = self._conn.execute(
            "SELECT * FROM failures ORDER BY timestamp DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_row_to_event(r) for r in rows]

    def clear(self) -> None:
        self._conn.execute("DELETE FROM failures")
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


def _row_to_event(row: sqlite3.Row) -> FailureEvent:
    return FailureEvent(
        id=row["id"],
        command=row["command"],
        exit_code=row["exit_code"],
        stderr=row["stderr"],
        cwd=row["cwd"],
        timestamp=row["timestamp"],
        shell=row["shell"],
    )
