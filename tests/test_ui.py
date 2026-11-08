"""Streamlit approval UI, driven without a browser (streamlit.testing.v1.AppTest)."""

from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from original_inbox import ORIGINAL_PATHS
from streamlit.testing.v1 import AppTest

from mock_erp.app import create_app
from procure.audit import AuditLog
from procure.erp_client import ErpClient

UI = str(Path(__file__).resolve().parent.parent / "ui" / "approve.py")


def app_with_services() -> tuple[AppTest, SimpleNamespace]:
    svc = SimpleNamespace(
        audit=AuditLog(),
        erp=ErpClient(TestClient(create_app(flaky_rate=0.0)), sleep=lambda _: None),
        paths=ORIGINAL_PATHS,
    )
    at = AppTest.from_file(UI, default_timeout=30)
    at.session_state["services"] = svc
    return at, svc


def test_empty_queue_shows_hint() -> None:
    at, _ = app_with_services()
    at.run()
    assert not at.exception
    assert "No purchase requests waiting" in at.info[0].value


def test_propose_then_approve_creates_po(monkeypatch) -> None:
    monkeypatch.setenv("PROCURE_TODAY", "2026-11-08")
    at, svc = app_with_services()
    at.run()
    at.button(key="propose").click().run()
    assert not at.exception
    assert "Müller Wälzlager GmbH" in at.success[0].value  # the recommendation

    at.text_input(key="approver").input("Anna Leitner").run()
    at.button(key="approve").click().run()
    assert not at.exception
    assert any("PO 4500000001 created in SAP" in s.value for s in at.success)
    assert len(svc.erp.list_pos()) == 1
    keys = {b.key for b in at.button}
    assert "approve" not in keys and "reject" not in keys  # closed runs offer no actions


def test_approve_without_name_shows_error(monkeypatch) -> None:
    monkeypatch.setenv("PROCURE_TODAY", "2026-11-08")
    at, svc = app_with_services()
    at.run()
    at.button(key="propose").click().run()
    at.button(key="approve").click().run()
    assert "approver name is required" in at.error[0].value
    assert svc.erp.list_pos() == []
