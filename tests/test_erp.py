"""ERP client + mock ERP. Runs the FastAPI app in-process (TestClient): no server, no network."""

import uuid
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from test_decide import ID, PR, QUOTES, TODAY

from mock_erp.app import create_app
from procure.config import load_config
from procure.decide import decide
from procure.erp_client import (
    ErpClient,
    ErpError,
    MissingSupplierError,
    RetryableError,
    build_draft_po,
    idempotency_key,
)
from procure.models import DraftPO, DraftPOItem

ROOT = Path(__file__).resolve().parent.parent
CFG = load_config(ROOT / "config/brunner.yaml")


def draft(supplier: str = "100042", price: str = "4.4600") -> DraftPO:
    return DraftPO(
        CompanyCode="1000",
        Supplier=supplier,
        DocumentCurrency="EUR",
        items=[DraftPOItem(Material="100-6205-2RS", OrderQuantity=Decimal("500"),
                           NetPriceAmount=Decimal(price))],
    )  # fmt: skip


def erp(**kwargs: object) -> tuple[TestClient, ErpClient, list[float]]:
    http = TestClient(create_app(**kwargs))  # type: ignore[arg-type]
    slept: list[float] = []
    return http, ErpClient(http, max_attempts=10, sleep=slept.append), slept


# --- the required test ---


def test_flaky_erp_with_forced_retries_creates_exactly_one_po() -> None:
    """FLAKY_RATE=0.5 incl. lost responses. The same PO is submitted 5 times: retries inside
    each call, plus 4 extra calls as if the buyer clicked approve again. Exactly 1 PO may exist.

    Seed 7 starts with 10 faults in a row, so the first submission gives up after all 10
    attempts even though one of them (a lost response) already created the PO. The buyer's next
    click must find that PO, not create a second one.
    """
    http, client, _ = erp(flaky_rate=0.5, seed=7)
    key = idempotency_key("PR-0001", "100042", Decimal("2230.00"))
    outcomes: list[str | None] = []
    for _ in range(5):
        try:
            outcomes.append(client.create_po(draft(), key)["PurchaseOrder"])
        except RetryableError:
            outcomes.append(None)  # client gave up; the buyer clicks again

    pos = [p for p in client.list_pos() if p["IdempotencyKey"] == key]
    assert len(pos) == 1
    assert outcomes[0] is None, "seed 7: the first submission gives up"
    assert {o for o in outcomes if o is not None} == {pos[0]["PurchaseOrder"]}
    faults = http.app.state.store.faults  # type: ignore[attr-defined]
    assert faults["503_after"] >= 1, "test must exercise a lost response"
    assert sum(faults.values()) >= 10, "test must exercise real retries"


def test_without_stable_key_retries_create_duplicates() -> None:
    """Control experiment: a new random key per attempt (a common mistake) duplicates POs."""
    http, _, _ = erp(script=["503_after", None])
    payload = draft().model_dump(mode="json", by_alias=True)
    for _ in range(2):  # attempt 1: created but response lost; attempt 2: "retry"
        http.post(PO_PATH, json=payload, headers={"Idempotency-Key": str(uuid.uuid4())})
    assert len(http.get(PO_PATH).json()["d"]["results"]) == 2


PO_PATH = "/sap/opu/odata/sap/API_PURCHASEORDER_PROCESS_SRV/A_PurchaseOrder"


# --- idempotency details ---


def test_different_pr_creates_second_po() -> None:
    _, client, _ = erp(flaky_rate=0.0)
    a = client.create_po(draft(), idempotency_key("PR-0001", "100042", Decimal("2230")))
    b = client.create_po(draft(), idempotency_key("PR-0002", "100042", Decimal("2230")))
    assert a["PurchaseOrder"] != b["PurchaseOrder"]
    assert len(client.list_pos()) == 2


def test_same_key_different_body_is_rejected() -> None:
    _, client, _ = erp(flaky_rate=0.0)
    key = idempotency_key("PR-0001", "100042", Decimal("2230"))
    client.create_po(draft(), key)
    with pytest.raises(ErpError, match="409"):
        client.create_po(draft(price="4.8500"), key)


def test_replay_is_marked() -> None:
    http, _, _ = erp(flaky_rate=0.0)
    payload = draft().model_dump(mode="json", by_alias=True)
    first = http.post(PO_PATH, json=payload, headers={"Idempotency-Key": "k"})
    second = http.post(PO_PATH, json=payload, headers={"Idempotency-Key": "k"})
    assert first.status_code == second.status_code == 201
    assert "Idempotent-Replayed" not in first.headers
    assert second.headers["Idempotent-Replayed"] == "true"


def test_missing_key_is_rejected() -> None:
    http, _, _ = erp(flaky_rate=0.0)
    r = http.post(PO_PATH, json=draft().model_dump(mode="json", by_alias=True))
    assert r.status_code == 400


def test_key_format() -> None:
    assert idempotency_key("PR-1", "23", Decimal("5")) != idempotency_key(
        "PR-12", "3", Decimal("5")
    )
    assert idempotency_key("PR-1", "2", Decimal("2230")) == idempotency_key(
        "PR-1", "2", Decimal("2230.00")
    )


# --- retries ---


def test_retry_after_is_honoured() -> None:
    _, client, slept = erp(script=["429", None])
    client.create_po(draft(), "k")
    assert slept == [1.0]
    assert client.attempts_last_call == 2


def test_backoff_without_retry_after() -> None:
    _, client, slept = erp(script=["503_before", "503_before", None])
    client.create_po(draft(), "k")
    assert len(slept) == 2 and all(0 < s <= 8.0 for s in slept)


def test_gives_up_after_max_attempts() -> None:
    http = TestClient(create_app(flaky_rate=1.0, seed=1))
    client = ErpClient(http, max_attempts=3, sleep=lambda _: None)
    with pytest.raises(RetryableError):
        client.create_po(draft(), "k")
    assert client.attempts_last_call == 3


def test_transport_errors_are_retried() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectTimeout("timed out", request=request)
        return httpx.Response(201, json={"d": {"PurchaseOrder": "4500000001"}})

    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://erp")
    client = ErpClient(http, sleep=lambda _: None)
    assert client.create_po(draft(), "k")["PurchaseOrder"] == "4500000001"
    assert calls["n"] == 2


def test_client_errors_are_not_retried() -> None:
    _, client, slept = erp(flaky_rate=0.0)
    bad = draft()
    bad.items[0].Material = ""  # still valid for us, but let the ERP decide
    client.create_po(bad, "k")  # mock accepts it; now reuse the key with a different body
    with pytest.raises(ErpError):
        client.create_po(draft(), "k")
    assert slept == [] and client.attempts_last_call == 1


# --- from decision to draft PO ---


def test_build_draft_po_for_golden_winner() -> None:
    _, candidates = decide(list(QUOTES.values()), PR, CFG, TODAY)
    winner = next(c for c in candidates if c.quote.quote_id == ID["q01_mueller_waelzlager.pdf"])
    po, key = build_draft_po(PR, winner, CFG)
    payload = po.model_dump(mode="json", by_alias=True)
    assert payload == {
        "PurchaseOrderType": "NB",
        "CompanyCode": "1000",
        "Supplier": "100042",
        "DocumentCurrency": "EUR",
        "to_PurchaseOrderItem": [
            {"Material": "100-6205-2RS", "OrderQuantity": "500", "NetPriceAmount": "4.4600"}
        ],
    }
    assert key == idempotency_key("PR-0001", "100042", Decimal("2230.00"))


def test_gross_quote_becomes_net_price_on_po() -> None:
    _, candidates = decide(list(QUOTES.values()), PR, CFG, TODAY)
    alpentech = next(c for c in candidates if c.quote.supplier_name.startswith("AlpenTech"))
    po, _ = build_draft_po(PR, alpentech, CFG)
    assert po.items[0].NetPriceAmount == Decimal("4.6975")  # 5.59 / 1.19


def test_supplier_without_sap_number_gets_no_po() -> None:
    _, candidates = decide(list(QUOTES.values()), PR, CFG, TODAY)
    us = next(c for c in candidates if c.quote.supplier_name == "US Bearing Co.")
    with pytest.raises(MissingSupplierError, match="create the supplier in SAP"):
        build_draft_po(PR, us, CFG)
