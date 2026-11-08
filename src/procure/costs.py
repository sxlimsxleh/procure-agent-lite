"""Estimated API cost per call. USD per million tokens (input, output), list prices.

Not customer configuration (that lives in config/brunner.yaml), so it is a plain table here.
"""

from decimal import Decimal

from procure.models import Usage

PRICES_PER_MTOK: dict[str, tuple[Decimal, Decimal]] = {
    "claude-opus-5-5": (Decimal("4.00"), Decimal("20.00")),
    "claude-sonnet-5-5": (Decimal("2.00"), Decimal("10.00")),
    "claude-haiku-5-5": (Decimal("0.10"), Decimal("0.50")),
}


def estimate_cost_usd(model: str, usage: Usage) -> Decimal | None:
    """None if the model is not in the table (better unknown than wrong)."""
    if model not in PRICES_PER_MTOK:
        return None
    price_in, price_out = PRICES_PER_MTOK[model]
    return (usage.input_tokens * price_in + usage.output_tokens * price_out) / 1_000_000
