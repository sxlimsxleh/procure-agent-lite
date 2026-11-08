"""Eval helper: a "perfect model answer" built from the golden labels.

The recording format itself lives in procure/recordings.py (the pipeline replays recordings too).
"""

from datetime import date
from typing import Any

from procure.extract import FIELD_KINDS
from procure.models import GoldenQuote


def raw_from_golden(quote: GoldenQuote | None) -> dict[str, Any]:
    """What a perfect model would send to record_quote, built from the hand labels.

    suspected_injection is always False, so the code-side tripwire alone must catch q12.
    """
    raw: dict[str, Any] = {
        "is_quote": quote is not None,
        "supplier_name": quote.supplier_name if quote else None,
        "currency": quote.currency if quote else None,
        "vat_included": quote.vat_included if quote else None,
        "price_unit": quote.price_unit if quote else None,
        "quote_number": quote.quote_number if quote else None,
        "suspected_injection": False,
        "injection_text": None,
    }
    for name in FIELD_KINDS:
        field = getattr(quote, name) if quote else None
        value = field.value if field else None
        if isinstance(value, date):
            value = value.isoformat()
        if name == "unit_price" and value is not None and quote and quote.price_unit:
            value = value * quote.price_unit  # the document states the price per N pieces
        raw[name] = {
            "value": None if value is None else str(value),
            "source_text": field.source_text if field else None,
        }
    return raw
