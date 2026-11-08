"""Extraction tests. No API key needed: the Claude client is replaced by a fake."""

from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from procure.extract import (
    FIELD_KINDS,
    RECORD_QUOTE_TOOL,
    build_request,
    build_result,
    extract_document,
)
from procure.ingest import Document, load_inbox
from procure.models import GoldenQuote, GoldenSet, PurchaseRequest, QuoteFlag
from procure.normalise import check_quote

ROOT = Path(__file__).resolve().parent.parent
PR = PurchaseRequest.model_validate_json((ROOT / "data/pr/PR-0001.json").read_text("utf-8"))
LABELS = GoldenSet.model_validate_json((ROOT / "golden/labels.json").read_text("utf-8"))
DOCS = {d.name: d for d in load_inbox(ROOT / "data/inbox")}


def raw_from_golden(quote: GoldenQuote | None) -> dict[str, Any]:
    """What a perfect model would send to record_quote for this document."""
    if quote is None:
        empty = {"value": None, "source_text": None}
        return {
            "is_quote": False, "supplier_name": None, "currency": None, "vat_included": None,
            **{name: empty for name in FIELD_KINDS},
            "suspected_injection": False, "injection_text": None,
        }  # fmt: skip
    raw: dict[str, Any] = {
        "is_quote": True,
        "supplier_name": quote.supplier_name,
        "currency": quote.currency,
        "vat_included": quote.vat_included,
        # Deliberately False everywhere: proves the code-side tripwire alone catches q12.
        "suspected_injection": False,
        "injection_text": None,
    }
    for name in FIELD_KINDS:
        field = getattr(quote, name)
        value = field.value
        if isinstance(value, date):
            value = value.isoformat()
        raw[name] = {
            "value": None if value is None else str(value),
            "source_text": field.source_text,
        }
    return raw


class FakeClient:
    """Mimics client.beta.messages.create and records the request it got."""

    def __init__(self, content: list[Any], stop_reason: str = "tool_use") -> None:
        self.requests: list[dict[str, Any]] = []
        response = SimpleNamespace(
            content=content,
            stop_reason=stop_reason,
            usage=SimpleNamespace(input_tokens=1200, output_tokens=300),
        )

        def create(**kwargs: Any) -> SimpleNamespace:
            self.requests.append(kwargs)
            return response

        self.beta = SimpleNamespace(messages=SimpleNamespace(create=create))


def tool_use(raw: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", name="record_quote", input=raw)


# --- the request ---


def test_request_exposes_exactly_one_strict_recording_tool() -> None:
    req = build_request(DOCS["q01_mueller_waelzlager.pdf"], PR, "claude-opus-5-5")
    assert req["tools"] == [RECORD_QUOTE_TOOL]
    assert RECORD_QUOTE_TOOL["strict"] is True
    assert RECORD_QUOTE_TOOL["input_schema"]["additionalProperties"] is False
    assert req["tool_choice"] == {"type": "auto"}  # forced tool use is rejected by current models
    assert "temperature" not in req  # rejected by current models


def test_request_wraps_document_as_data() -> None:
    doc = DOCS["q12_ostbayern_lager.pdf"]
    content = build_request(doc, PR, "m")["messages"][0]["content"]
    assert f'<document name="{doc.name}">' in content
    assert content.rstrip().endswith("</document>")
    assert "untrusted data" in build_request(doc, PR, "m")["system"]


# --- calling the (fake) model ---


def test_extract_document_records_usage_and_grounds_fields() -> None:
    doc = DOCS["q01_mueller_waelzlager.pdf"]
    raw = raw_from_golden(LABELS.docs[doc.name].quote)
    client = FakeClient([SimpleNamespace(type="thinking"), tool_use(raw)])
    result = extract_document(client, doc, PR, model="claude-opus-5-5")
    assert result.quote is not None
    assert result.quote.unit_price.status == "ok"
    assert result.usage.input_tokens == 1200 and result.usage.output_tokens == 300
    assert client.requests[0]["model"] == "claude-opus-5-5"


def test_duplicate_is_skipped_without_llm_call() -> None:
    client = FakeClient([])
    result = extract_document(client, DOCS["q09_fwd_again.eml"], PR, model="m")
    assert result.skipped == QuoteFlag.DUPLICATE
    assert result.duplicate_of == "q08_fwd.eml"
    assert client.requests == []


@pytest.mark.parametrize(
    ("content", "stop_reason"),
    [
        ([SimpleNamespace(type="text", text="Here is the quote...")], "end_turn"),
        ([], "refusal"),
    ],
)
def test_no_tool_call_gives_error_not_guess(content: list[Any], stop_reason: str) -> None:
    client = FakeClient(content, stop_reason)
    result = extract_document(client, DOCS["q05_baltic_bearings.txt"], PR, model="m")
    assert result.quote is None and result.error is not None


def test_model_cannot_ground_an_invented_value() -> None:
    """The LLM 'corrects' q07's wrong total. Grounding must flag it, not accept it."""
    doc = DOCS["q07_rhein_kugel.eml"]
    raw = raw_from_golden(LABELS.docs[doc.name].quote)
    raw["total"] = {"value": "2350.00", "source_text": "Gesamtsumme: 2.350,00 €"}
    quote = build_result(doc, raw).quote
    assert quote is not None
    assert quote.total.status == "flagged" and quote.total.value is None


def test_model_cannot_fill_missing_value_from_purchase_request() -> None:
    doc = DOCS["q11_huber_pricelist.xlsx"]
    raw = raw_from_golden(LABELS.docs[doc.name].quote)
    raw["quantity"] = {"value": "500", "source_text": "Menge: 500"}  # copied from the PR
    quote = build_result(doc, raw).quote
    assert quote is not None and quote.quantity.status == "flagged"


def test_llm_reported_injection_is_flagged_even_without_tripwire_match() -> None:
    doc = DOCS["q03_helvetia_lager.pdf"]
    raw = raw_from_golden(LABELS.docs[doc.name].quote) | {"suspected_injection": True}
    quote = build_result(doc, raw).quote
    assert quote is not None and QuoteFlag.SUSPECTED_INJECTION in quote.flags


# --- golden replay: perfect model output through the real code ---


@pytest.mark.parametrize("name", sorted(LABELS.docs))
def test_golden_replay(name: str) -> None:
    """If the LLM answered exactly like the labels, would the code reproduce every label?

    Proves the code half (grounding, flags, dedupe) is right without an API key. Milestone 3
    measures the LLM half against the same labels.
    """
    doc: Document = DOCS[name]
    golden = LABELS.docs[name]
    if doc.duplicate_of is not None:
        result = extract_document(FakeClient([]), doc, PR, model="m")
    else:
        result = build_result(doc, raw_from_golden(golden.quote))
    if result.quote is not None:
        result = result.model_copy(update={"quote": check_quote(result.quote, LABELS.today)})

    assert sorted(result.flags) == sorted(golden.flags)
    assert result.duplicate_of == golden.duplicate_of
    assert (result.quote is None) == (golden.quote is None)
    if result.quote is None or golden.quote is None:
        return
    for field in FIELD_KINDS:
        got, want = getattr(result.quote, field), getattr(golden.quote, field)
        assert (got.status, got.value) == (want.status, want.value), field
    assert result.quote.quote_id == doc.quote_id
