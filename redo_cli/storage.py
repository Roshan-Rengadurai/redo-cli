"""SQLite-backed history of failed commands.

The store holds *only raw events*. Diagnoses are never persisted — `stats`,
`explain` and `learn` all re-run the matcher over these rows, so there is a
single source of truth and nothing to migrate when rules change.

Location: ``~/.redo/history.db`` (override the parent dir with $REDO_HOME,
which is also how the test suite avoids touching a real home directory).
The shell hook appends failures to ``~/.redo/spool``; ``ingest_spool`` folds
them in on the next run.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import List, Optional

from .models import FailureEvent


SPOOL_FS = "\x1f"   # unit separator between fields
SPOOL_RS = "\x1e"   # record separator after each record


def home_dir() -> Path:
    home = os.environ.get("REDO_HOME")
    return Path(home).expanduser() if home else Path.home() / ".redo"


def default_db_path() -> Path:
    return home_dir() / "history.db"


def spool_path() -> Path:
    """Where the shell hook appends failures (see shell.py). Pure-shell
    writers, Python reader."""
    return home_dir() / "spool"


def parse_spool(text: str) -> List[FailureEvent]:
    """Decode spool records. Anything malformed (wrong field count, bad
    numbers, a partial record without its terminator) is dropped."""
    events = []
    for rec in text.split(SPOOL_RS)[:-1]:   # last chunk is "" or a partial
        parts = rec.split(SPOOL_FS)
        if len(parts) != 5:
            continue
        shell, code, cwd, ts, command = parts
        try:
            exit_code, timestamp = int(code), float(ts)
        except ValueError:
            continue
        if not command.strip():
            continue
        events.append(FailureEvent(
            id=None, command=command, exit_code=exit_code, stderr="",
            cwd=cwd, timestamp=timestamp, shell=shell,
        ))
    return events


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

    def ingest_spool(self, path) -> int:
        """Move hook-captured failures from the spool file into the db.

        The file is renamed first so shells that append while we read
        start a fresh spool instead of racing the truncate."""
        path = Path(path)
        if not path.exists():
            return 0
        work = path.with_suffix(".ingest")
        try:
            os.replace(path, work)
        except OSError:
            return 0
        try:
            events = parse_spool(work.read_text(errors="replace"))
        finally:
            try:
                work.unlink()
            except OSError:
                pass
        for e in events:
            self.record(e)
        return len(events)

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
