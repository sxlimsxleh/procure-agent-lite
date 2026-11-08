"""End-to-end: recorded extractions -> decision -> approval -> mock SAP, all in-process."""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mock_erp.app import create_app
from procure.audit import AuditLog
from procure.config import load_config
from procure.erp_client import ErpClient
from procure.pipeline import (
    DEFAULT_PATHS,
    ApprovalError,
    approve,
    load_run,
    open_runs,
    propose,
    retry_po,
)

TODAY = date(2026, 11, 8)


def setup(**erp_kwargs: object) -> tuple[AuditLog, ErpClient]:
    erp_kwargs.setdefault("flaky_rate", 0.0)
    http = TestClient(create_app(**erp_kwargs))  # type: ignore[arg-type]
    return AuditLog(), ErpClient(http, max_attempts=5, sleep=lambda _: None)


def po_count(erp: ErpClient) -> int:
    return len(erp.list_pos())


def test_happy_path_creates_one_po_with_full_audit_trail() -> None:
    audit, erp = setup()
    proposal = propose("PR-0001", audit, today=TODAY)
    assert proposal.winner is not None
    assert proposal.winner.quote.supplier_name == "Müller Wälzlager GmbH"
    assert proposal.draft_po is not None and proposal.draft_po.Supplier == "100042"
    assert [s.run_id for s in open_runs(audit)] == [proposal.run_id]

    state = approve(proposal.run_id, "Anna Leitner", audit, erp)
    assert state.status == "po_created" and state.po_number == "4500000001"
    assert [e.step for e in state.events] == ["decision", "proposal", "approval", "po_created"]
    assert state.events[2].actor == "Anna Leitner"
    assert open_runs(audit) == []
    assert po_count(erp) == 1


def test_proposal_keeps_every_source_span() -> None:
    audit, _ = setup()
    proposal = propose("PR-0001", audit, today=TODAY)
    stored = audit.events(proposal.run_id)[1].payload  # the proposal event
    mueller = next(q for q in stored["quotes"] if q["doc_name"] == "q01_mueller_waelzlager.pdf")
    assert mueller["quote"]["unit_price"]["source_text"] == "ab 500 Stk.: 4,46 €"
    assert {s["doc_name"] for s in stored["skipped"]} == {"q09_fwd_again.eml", "q13_newsletter.eml"}


def test_reject_creates_no_po() -> None:
    audit, erp = setup()
    proposal = propose("PR-0001", audit, today=TODAY)
    state = approve(proposal.run_id, "Anna Leitner", audit, erp, approved=False, comment="no")
    assert state.status == "rejected"
    assert po_count(erp) == 0
    with pytest.raises(ApprovalError, match="closed"):
        approve(proposal.run_id, "Anna Leitner", audit, erp)


def test_double_approval_still_one_po() -> None:
    audit, erp = setup()
    proposal = propose("PR-0001", audit, today=TODAY)
    approve(proposal.run_id, "Anna Leitner", audit, erp)
    with pytest.raises(ApprovalError, match="closed"):
        approve(proposal.run_id, "Markus Wolf", audit, erp)
    assert po_count(erp) == 1


def test_empty_approver_is_refused() -> None:
    audit, erp = setup()
    proposal = propose("PR-0001", audit, today=TODAY)
    with pytest.raises(ApprovalError, match="required"):
        approve(proposal.run_id, "   ", audit, erp)


def test_above_threshold_needs_two_different_approvers() -> None:
    audit, erp = setup()
    cfg = load_config(DEFAULT_PATHS.config)
    low_threshold = cfg.model_copy(
        update={"policy": cfg.policy.model_copy(update={"approval_threshold_eur": Decimal("1000")})}
    )
    proposal = propose("PR-0001", audit, today=TODAY, cfg=low_threshold)
    assert proposal.required_approvers == 2

    state = approve(proposal.run_id, "Anna Leitner", audit, erp)
    assert state.status == "awaiting_approval" and po_count(erp) == 0
    with pytest.raises(ApprovalError, match="someone else"):
        approve(proposal.run_id, "Anna Leitner", audit, erp)
    assert po_count(erp) == 0

    state = approve(proposal.run_id, "Head of Purchasing", audit, erp)
    assert state.status == "po_created" and po_count(erp) == 1


def test_sap_down_then_retry_is_safe() -> None:
    # 5 faults: every attempt of the first approval fails; the 4th one is a lost response,
    # so SAP has the PO but we never heard. The retry must find it, not duplicate it.
    audit, erp = setup(script=["503_before", "429", "503_before", "503_after", "503_before"])
    proposal = propose("PR-0001", audit, today=TODAY)
    state = approve(proposal.run_id, "Anna Leitner", audit, erp)
    assert state.status == "erp_unconfirmed"
    assert [s.run_id for s in open_runs(audit)] == [proposal.run_id]  # still in the queue
    assert po_count(erp) == 1  # SAP did create it

    state = retry_po(proposal.run_id, "Anna Leitner", audit, erp)
    assert state.status == "po_created"
    assert po_count(erp) == 1
    steps = [e.step for e in load_run(audit, proposal.run_id).events]
    assert steps == ["decision", "proposal", "approval", "po_failed", "po_retry", "po_created"]


def test_no_recommendation_cannot_be_approved(tmp_path: Path) -> None:
    """Inbox with only 2 valid quotes -> nothing to approve, but rejecting is still possible."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    for name in ["q01_mueller_waelzlager.pdf", "q08_fwd.eml", "q06_lagertechnik_franken.pdf"]:
        (inbox / name).write_bytes((DEFAULT_PATHS.inbox / name).read_bytes())
    paths = type(DEFAULT_PATHS)(inbox=inbox)
    audit, erp = setup()
    proposal = propose("PR-0001", audit, today=TODAY, paths=paths)
    assert proposal.draft_po is None
    assert proposal.po_blocked_reason is not None and "only 2 valid" in proposal.po_blocked_reason
    with pytest.raises(ApprovalError, match="nothing to approve"):
        approve(proposal.run_id, "Anna Leitner", audit, erp)
    assert approve(proposal.run_id, "Anna Leitner", audit, erp, approved=False).status == "rejected"


def test_json_logs_carry_run_id(capsys: pytest.CaptureFixture) -> None:
    audit, erp = setup()
    proposal = propose("PR-0001", audit, today=TODAY)
    approve(proposal.run_id, "Anna Leitner", audit, erp)
    lines = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    steps = {line["step"]: line for line in lines if line["event"] == "step"}
    assert set(steps) == {"ingest", "extract", "decide", "erp_write"}
    assert all(line["run_id"] == proposal.run_id for line in lines)
    assert all(isinstance(s["duration_ms"], int) for s in steps.values())
    assert steps["extract"]["input_tokens"] > 0
    assert steps["erp_write"]["po_number"] == "4500000001"
