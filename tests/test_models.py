from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from procure.models import (
    Decision,
    DraftPO,
    DraftPOItem,
    ExtractedField,
    GoldenDoc,
    Quote,
    QuoteFlag,
)

HASH = "a" * 64


def make_quote(**overrides: object) -> Quote:
    fields: dict[str, object] = dict(
        quote_id="Q-aaaaaaaa",
        doc_hash=HASH,
        supplier_name="Müller Wälzlager GmbH",
        currency="EUR",
        unit_price=ExtractedField[Decimal].ok(Decimal("4.46"), "ab 500 Stk.: 4,46 €"),
        quantity=ExtractedField[Decimal].ok(Decimal("500"), "Menge: 500 Stück"),
        total=ExtractedField[Decimal].ok(Decimal("2230.00"), "2.230,00 €"),
        vat_included=False,
        delivery_date=ExtractedField[date].flagged("no delivery date stated"),
        valid_until=ExtractedField[date].ok(date(2026, 11, 30), "Gültig bis: 30.11.2026"),
        payment_terms_days=ExtractedField[int].ok(30, "30 Tage netto"),
    )
    fields.update(overrides)
    return Quote.model_validate(fields)


# --- ExtractedField: grounded or flagged ---


def test_ok_field_requires_value_and_source_text() -> None:
    with pytest.raises(ValidationError):
        ExtractedField[Decimal](value=Decimal("1"), status="ok")
    with pytest.raises(ValidationError):
        ExtractedField[Decimal](source_text="1,00 €", status="ok")


def test_flagged_field_must_not_carry_a_value() -> None:
    with pytest.raises(ValidationError, match="never guess"):
        ExtractedField[Decimal](value=Decimal("1"), status="flagged", reason="unsure")


def test_flagged_field_requires_reason() -> None:
    with pytest.raises(ValidationError):
        ExtractedField[Decimal](status="flagged")


def test_typed_value_is_parsed() -> None:
    f = ExtractedField[date].model_validate(
        {"value": "2026-12-15", "source_text": "15.12.2026", "status": "ok"}
    )
    assert f.value == date(2026, 12, 15)


def test_decimal_survives_json_round_trip() -> None:
    f = ExtractedField[Decimal].ok(Decimal("4.46"), "4,46 €")
    again = ExtractedField[Decimal].model_validate_json(f.model_dump_json())
    assert again.value == Decimal("4.46")


# --- Quote ---


def test_valid_quote() -> None:
    q = make_quote(flags=["expired"])
    assert q.flags == [QuoteFlag.EXPIRED]


@pytest.mark.parametrize(
    "overrides",
    [
        {"quote_id": "quote-1"},
        {"doc_hash": "not-a-hash"},
        {"currency": "€"},
        {"flags": ["looks_fishy"]},  # unknown flag
        {"surprise_field": 1},  # extra keys are rejected
    ],
)
def test_invalid_quote_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        make_quote(**overrides)


# --- Decision ---


def test_decision_rationale_may_cite_scored_quotes() -> None:
    Decision(
        recommended_quote_id="Q-aaaaaaaa",
        scores={"Q-aaaaaaaa": 0.9, "Q-bbbbbbbb": 0.7},
        policy_violations=[],
        needs_approval=True,
        rationale="Q-aaaaaaaa is cheapest; Q-bbbbbbbb delivers later.",
    )


def test_decision_rationale_citing_unknown_quote_is_rejected() -> None:
    with pytest.raises(ValidationError, match="Q-cccccccc"):
        Decision(
            recommended_quote_id="Q-aaaaaaaa",
            scores={"Q-aaaaaaaa": 0.9},
            policy_violations=[],
            needs_approval=True,
            rationale="Q-aaaaaaaa beats Q-cccccccc.",
        )


def test_decision_recommendation_must_be_scored() -> None:
    with pytest.raises(ValidationError):
        Decision(
            recommended_quote_id="Q-dddddddd",
            scores={"Q-aaaaaaaa": 0.9},
            policy_violations=[],
            needs_approval=True,
            rationale="",
        )


def test_decision_without_recommendation() -> None:
    d = Decision(
        recommended_quote_id=None,
        scores={},
        policy_violations=["fewer than 3 valid quotes"],
        needs_approval=True,
        rationale="Not enough valid quotes.",
    )
    assert d.recommended_quote_id is None


# --- DraftPO ---


def test_draft_po_defaults_to_standard_order_type() -> None:
    po = DraftPO(
        CompanyCode="1000",
        Supplier="100042",
        DocumentCurrency="EUR",
        items=[
            DraftPOItem(
                Material="100-6205-2RS",
                OrderQuantity=Decimal("500"),
                NetPriceAmount=Decimal("4.46"),
            )
        ],
    )
    assert po.PurchaseOrderType == "NB"


def test_draft_po_needs_items_and_positive_amounts() -> None:
    with pytest.raises(ValidationError):
        DraftPO(CompanyCode="1000", Supplier="100042", DocumentCurrency="EUR", items=[])
    with pytest.raises(ValidationError):
        DraftPOItem(Material="x", OrderQuantity=Decimal("0"), NetPriceAmount=Decimal("1"))


# --- GoldenDoc consistency ---


def test_golden_duplicate_needs_duplicate_of() -> None:
    with pytest.raises(ValidationError):
        GoldenDoc(quote=None, flags=[QuoteFlag.DUPLICATE], notes="")


def test_golden_non_quote_has_no_quote() -> None:
    GoldenDoc(quote=None, flags=[QuoteFlag.NOT_A_QUOTE], notes="newsletter")
    with pytest.raises(ValidationError):
        GoldenDoc(quote=None, flags=[], notes="a quote with no fields?")
