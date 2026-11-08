"""Make quotes comparable and apply document-level checks that need `today`.

VAT rate and FX table are parameters here; from milestone 4 they come from config/brunner.yaml.
FX convention: `fx[currency]` = how many EUR one unit of that currency is worth (EUR -> 1).
"""

import re
from collections.abc import Mapping
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from procure.models import NormalisedQuote, Quote, QuoteFlag

CENT = Decimal("0.01")
UNIT_PRICE_PLACES = Decimal("0.0001")  # unit prices keep 4 decimals so totals do not drift


def to_net(amount: Decimal, vat_included: bool, vat_rate: Decimal) -> Decimal:
    """Gross -> net. Net amounts pass through unchanged. Rounding happens in the caller."""
    return amount / (1 + vat_rate) if vat_included else amount


def to_eur(amount: Decimal, currency: str, fx: Mapping[str, Decimal]) -> Decimal:
    if currency not in fx:
        raise ValueError(f"no FX rate for {currency}")
    return amount * fx[currency]


def is_expired(valid_until: date, today: date) -> bool:
    """A quote valid until today is still valid today."""
    return valid_until < today


def total_matches(quantity: Decimal, unit_price: Decimal, total: Decimal) -> bool:
    """Tolerance of one cent for suppliers who round per line."""
    return abs(quantity * unit_price - total) <= CENT


def check_quote(quote: Quote, today: date) -> Quote:
    """Return a copy of `quote` with EXPIRED / TOTAL_MISMATCH added where they apply."""
    flags = list(quote.flags)
    if quote.valid_until.value is not None and is_expired(quote.valid_until.value, today):
        flags.append(QuoteFlag.EXPIRED)
    q, p, t = quote.quantity.value, quote.unit_price.value, quote.total.value
    if q is not None and p is not None and t is not None and not total_matches(q, p, t):
        flags.append(QuoteFlag.TOTAL_MISMATCH)
    unique = list(dict.fromkeys(flags))  # de-duplicate, keep order
    return quote.model_copy(update={"flags": unique})


def normalise_quote(quote: Quote, vat_rate: Decimal, fx: Mapping[str, Decimal]) -> NormalisedQuote:
    """Net EUR prices. None if a price is flagged or it is unknown whether VAT is included:
    we would rather not compare a quote than compare it on a guessed basis."""

    def convert(amount: Decimal | None, places: Decimal) -> Decimal | None:
        if amount is None or quote.vat_included is None:
            return None
        net = to_net(amount, quote.vat_included, vat_rate)
        return to_eur(net, quote.currency, fx).quantize(places, rounding=ROUND_HALF_UP)

    return NormalisedQuote(
        quote=quote,
        unit_price_net_eur=convert(quote.unit_price.value, UNIT_PRICE_PLACES),
        total_net_eur=convert(quote.total.value, CENT),
    )


# "AN-2026-1187 Rev. 2", "... Revision 3", "...-V2", "... Version 2" -> base number + revision
_REVISION = re.compile(r"[\s\-_/]*(?:rev(?:ision)?\.?|version|v)\s*(\d+)\s*$", re.IGNORECASE)


def split_revision(quote_number: str) -> tuple[str, int]:
    """("AN-2026-1187 Rev. 2") -> ("an-2026-1187", 2). No revision marker -> revision 0."""
    match = _REVISION.search(quote_number)
    base = quote_number[: match.start()] if match else quote_number
    revision = int(match.group(1)) if match else 0
    return " ".join(base.split()).casefold(), revision


def mark_superseded(quotes: list[Quote]) -> list[Quote]:
    """Flag every quote that has a newer revision from the same supplier (same base number).

    Quotes without a quote number are never grouped: we do not guess that two quotes belong
    together. Two quotes with the same number and revision are left alone (no newest one).
    """
    groups: dict[tuple[str, str], list[tuple[int, int]]] = {}  # key -> [(revision, index)]
    for i, q in enumerate(quotes):
        if q.quote_number:
            base, revision = split_revision(q.quote_number)
            key = (" ".join(q.supplier_name.split()).casefold(), base)
            groups.setdefault(key, []).append((revision, i))
    superseded: set[int] = set()
    for members in groups.values():
        newest = max(rev for rev, _ in members)
        if sum(rev == newest for rev, _ in members) == 1:
            superseded |= {i for rev, i in members if rev < newest}
    return [
        q.model_copy(update={"flags": list(dict.fromkeys([*q.flags, QuoteFlag.SUPERSEDED]))})
        if i in superseded
        else q
        for i, q in enumerate(quotes)
    ]
