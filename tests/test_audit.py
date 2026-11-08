import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from procure.audit import AuditLog
from procure.models import Decision

FIXED = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


def decision() -> Decision:
    return Decision(
        recommended_quote_id="Q-aaaaaaaa",
        scores={"Q-aaaaaaaa": 0.87},
        policy_violations=[],
        needs_approval=False,
        rationale="Q-aaaaaaaa is best.",
    )


def test_events_are_recorded_in_order(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.db", clock=lambda: FIXED)
    log.record_decision("run-1", "PR-0001", decision())
    log.record_approval("run-1", "PR-0001", "Anna Leitner", approved=True, comment="ok")
    log.record_po_created("run-1", "PR-0001", "4500000001", "abc", attempts=3)
    log.record("run-2", "decision", "system", {})

    events = log.events("run-1")
    assert [(e.step, e.actor) for e in events] == [
        ("decision", "system"),
        ("approval", "Anna Leitner"),
        ("po_created", "system"),
    ]
    assert events[0].payload["recommended_quote_id"] == "Q-aaaaaaaa"
    assert events[2].payload == {"po_number": "4500000001", "idempotency_key": "abc", "attempts": 3}
    assert events[1].ts == "2026-11-08T12:00:00+00:00"
    assert len(log.events()) == 4


def test_log_survives_reopen(tmp_path: Path) -> None:
    AuditLog(tmp_path / "audit.db").record("run-1", "decision", "system", {"x": 1})
    assert AuditLog(tmp_path / "audit.db").events()[0].payload == {"x": 1}


@pytest.mark.parametrize("sql", ["UPDATE events SET actor = 'someone else'", "DELETE FROM events"])
def test_log_is_append_only(sql: str) -> None:
    log = AuditLog()
    log.record("run-1", "approval", "Anna Leitner", {"approved": True})
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with log.conn:
            log.conn.execute(sql)
    assert log.events()[0].actor == "Anna Leitner"
