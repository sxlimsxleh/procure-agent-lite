"""Append-only SQLite audit log (design rule 6): who decided, approved and wrote what, when.

Append-only is enforced by the database itself (triggers), not just by convention: an UPDATE
or DELETE on `events` raises an error.
"""

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from procure.models import Decision

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id  TEXT NOT NULL,
    pr_id   TEXT,
    ts      TEXT NOT NULL,            -- ISO 8601, UTC
    step    TEXT NOT NULL,            -- decision | approval | po_created | ...
    actor   TEXT NOT NULL,            -- "system" or the approver's name
    payload TEXT NOT NULL             -- JSON
);
CREATE INDEX IF NOT EXISTS events_run ON events (run_id);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
"""


@dataclass(frozen=True)
class AuditEvent:
    id: int
    run_id: str
    pr_id: str | None
    ts: str
    step: str
    actor: str
    payload: dict[str, Any]


class AuditLog:
    def __init__(
        self,
        path: Path | str = ":memory:",
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.executescript(SCHEMA)
        self.clock = clock

    def record(
        self, run_id: str, step: str, actor: str, payload: dict[str, Any], pr_id: str | None = None
    ) -> int:
        with self.conn:  # commits, or rolls back on error
            cursor = self.conn.execute(
                "INSERT INTO events (run_id, pr_id, ts, step, actor, payload) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    pr_id,
                    self.clock().isoformat(),
                    step,
                    actor,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
                ),
            )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def events(self, run_id: str | None = None) -> list[AuditEvent]:
        query = "SELECT id, run_id, pr_id, ts, step, actor, payload FROM events"
        args: tuple[str, ...] = ()
        if run_id is not None:
            query += " WHERE run_id = ?"
            args = (run_id,)
        rows = self.conn.execute(query + " ORDER BY id", args).fetchall()
        return [AuditEvent(*row[:6], payload=json.loads(row[6])) for row in rows]

    # --- the events the pipeline writes ---

    def record_decision(self, run_id: str, pr_id: str, decision: Decision) -> int:
        return self.record(run_id, "decision", "system", decision.model_dump(mode="json"), pr_id)

    def record_approval(
        self, run_id: str, pr_id: str, approver: str, approved: bool, comment: str = ""
    ) -> int:
        payload = {"approved": approved, "comment": comment}
        return self.record(run_id, "approval", approver, payload, pr_id)

    def record_po_created(
        self, run_id: str, pr_id: str, po_number: str, key: str, attempts: int
    ) -> int:
        payload = {"po_number": po_number, "idempotency_key": key, "attempts": attempts}
        return self.record(run_id, "po_created", "system", payload, pr_id)
