from datetime import date
from decimal import Decimal

import pytest

from procure.models import ExtractedField, Quote, QuoteFlag
from procure.normalise import (
    check_quote,
    is_expired,
    normalise_quote,
    to_eur,
    to_net,
    total_matches,
)

VAT = Decimal("0.19")
FX = {"EUR": Decimal("1"), "CHF": Decimal("1.05"), "USD": Decimal("0.92")}
TODAY = date(2026, 11, 8)


def quote(**overrides: object) -> Quote:
    def ok(v: object, s: str = "src") -> dict:
        return {"value": v, "source_text": s, "status": "ok"}

    fields: dict[str, object] = dict(
        quote_id="Q-00000000",
        doc_hash="0" * 64,
        supplier_name="X",
        currency="EUR",
        unit_price=ok("4.70"),
        quantity=ok("500"),
        total=ok("2350.00"),
        vat_included=False,
        delivery_date=ok("2026-12-01"),
        valid_until=ok("2026-12-31"),
        payment_terms_days=ok(30),
    )
    fields.update(overrides)
    return Quote.model_validate(fields)


def test_gross_to_net() -> None:
    assert to_net(Decimal("2795.00"), True, VAT).quantize(Decimal("0.01")) == Decimal("2348.74")
    assert to_net(Decimal("2795.00"), False, VAT) == Decimal("2795.00")


def test_fx() -> None:
    assert to_eur(Decimal("100"), "CHF", FX) == Decimal("105.00")
    with pytest.raises(ValueError, match="GBP"):
        to_eur(Decimal("1"), "GBP", FX)


def test_expiry_boundary() -> None:
    assert is_expired(date(2026, 11, 7), TODAY)
    assert not is_expired(TODAY, TODAY)  # valid until today = still valid today


def test_total_tolerance_is_one_cent() -> None:
    assert total_matches(Decimal("3"), Decimal("0.333"), Decimal("1.00"))  # 0.999
    assert not total_matches(Decimal("500"), Decimal("4.70"), Decimal("2530.00"))


def test_check_quote_adds_flags_once() -> None:
    q = quote(
        total={"value": "2530.00", "source_text": "s", "status": "ok"},
        valid_until={"value": "2026-09-30", "source_text": "s", "status": "ok"},
    )
    checked = check_quote(check_quote(q, TODAY), TODAY)
    assert checked.flags == [QuoteFlag.EXPIRED, QuoteFlag.TOTAL_MISMATCH]
    assert q.flags == []  # original unchanged


def test_check_quote_skips_flagged_fields() -> None:
    q = quote(total=ExtractedField.flagged("not stated").model_dump())
    assert check_quote(q, TODAY).flags == []


def test_normalise_gross_chf() -> None:
    q = quote(
        currency="CHF",
        vat_included=True,
        unit_price={"value": "5.95", "source_text": "s", "status": "ok"},
        total={"value": "2975.00", "source_text": "s", "status": "ok"},
    )
    n = normalise_quote(q, VAT, FX)
    assert n.total_net_eur == Decimal("2625.00")  # 2975 / 1.19 = 2500 CHF net * 1.05
    assert n.unit_price_net_eur == Decimal("5.2500")


def test_normalise_refuses_to_guess_vat() -> None:
    n = normalise_quote(quote(vat_included=None), VAT, FX)
    assert n.unit_price_net_eur is None and n.total_net_eur is None
