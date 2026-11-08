"""Checks that the inbox, the PR and the golden labels agree with each other."""

import hashlib
import importlib.util
import json
from decimal import Decimal
from pathlib import Path

import pytest

from procure.ingest import collapse_ws, load_document
from procure.models import GoldenSet, PurchaseRequest, QuoteFlag

ROOT = Path(__file__).resolve().parent.parent
INBOX = ROOT / "data" / "inbox"
LABELS = GoldenSet.model_validate_json((ROOT / "golden" / "labels.json").read_text("utf-8"))
QUOTES = {name: doc.quote for name, doc in LABELS.docs.items() if doc.quote is not None}
FIELDS = ["unit_price", "quantity", "total", "delivery_date", "valid_until", "payment_terms_days"]


def test_every_inbox_file_is_labelled_and_vice_versa() -> None:
    assert {p.name for p in INBOX.iterdir()} == set(LABELS.docs)


def test_dataset_size_is_within_spec() -> None:
    assert 12 <= len(LABELS.docs) <= 17


def test_purchase_request_loads() -> None:
    pr = PurchaseRequest.model_validate_json((ROOT / "data/pr/PR-0001.json").read_text("utf-8"))
    assert pr.pr_id == LABELS.pr_id
    assert pr.quantity == Decimal("500")


@pytest.mark.parametrize("name", sorted(QUOTES))
def test_labelled_source_text_occurs_in_document(name: str) -> None:
    """Catches typos in my own labels: every 'ok' span must really be in the document."""
    text = collapse_ws(load_document(INBOX / name).text)
    quote = QUOTES[name]
    for field in FIELDS:
        label = getattr(quote, field)
        if label.status == "ok":
            assert collapse_ws(label.source_text) in text, f"{name}.{field}: {label.source_text!r}"


def test_expired_flag_matches_valid_until() -> None:
    for name, quote in QUOTES.items():
        expired = quote.valid_until.value is not None and quote.valid_until.value < LABELS.today
        assert expired == (QuoteFlag.EXPIRED in LABELS.docs[name].flags), name


def test_total_mismatch_flag_matches_arithmetic() -> None:
    for name, q in QUOTES.items():
        if q.total.value is None or q.quantity.value is None or q.unit_price.value is None:
            continue
        mismatch = q.quantity.value * q.unit_price.value != q.total.value
        assert mismatch == (QuoteFlag.TOTAL_MISMATCH in LABELS.docs[name].flags), name


def test_injection_doc_contains_the_injection() -> None:
    text = load_document(INBOX / "q12_ostbayern_lager.pdf").text.lower()
    assert "ignore previous instructions" in text


def test_dataset_requirements_are_covered() -> None:
    """The CLAUDE.md checklist for the messy dataset, as assertions."""
    currencies = {q.currency for q in QUOTES.values()}
    assert {"EUR", "CHF", "USD"} <= currencies
    assert any(q.vat_included is True for q in QUOTES.values())  # gross prices
    assert any(q.delivery_date.status == "flagged" for q in QUOTES.values())
    suffixes = {Path(n).suffix for n in LABELS.docs}
    assert {".eml", ".pdf", ".txt", ".xlsx"} <= suffixes
    all_flags = {f for d in LABELS.docs.values() for f in d.flags}
    assert set(QuoteFlag) <= all_flags  # every flag type has at least one example


def test_generator_reproduces_committed_files() -> None:
    """If scripts/make_dataset.py and data/inbox drift apart, regenerate with `make dataset`."""
    spec = importlib.util.spec_from_file_location("make_dataset", ROOT / "scripts/make_dataset.py")
    assert spec is not None and spec.loader is not None
    try:
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        generated = {name: build() for name, build in module.DOCUMENTS.items()}
    except (ImportError, FileNotFoundError, OSError) as exc:  # fpdf2 or font not available
        pytest.skip(f"generator cannot run here: {exc}")
    for name, data in generated.items():
        committed = (INBOX / name).read_bytes()
        assert hashlib.sha256(data).digest() == hashlib.sha256(committed).digest(), name


def test_labels_file_is_pretty_printed_utf8() -> None:
    raw = (ROOT / "golden" / "labels.json").read_text("utf-8")
    assert "Müller" in raw  # not ü-escaped, so it reads well in a diff
    json.loads(raw)
