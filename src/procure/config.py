"""Load and validate a customer config file (config/<customer>.yaml)."""

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import Field, model_validator

from procure.ingest import collapse_ws
from procure.models import Currency, StrictModel

RequiredField = Literal[
    "unit_price", "quantity", "total", "delivery_date", "valid_until", "payment_terms_days"
]


class BlockedSupplier(StrictModel):
    name: str
    reason: str


class Weights(StrictModel):
    price: Decimal = Field(ge=0)
    delivery: Decimal = Field(ge=0)
    payment_terms: Decimal = Field(ge=0)

    @model_validator(mode="after")
    def _sum_to_one(self) -> Self:
        if self.price + self.delivery + self.payment_terms != 1:
            raise ValueError("weights must add up to 1")
        return self


class Policy(StrictModel):
    approval_threshold_eur: Decimal = Field(gt=0)
    min_valid_quotes: int = Field(ge=1)
    required_fields: list[RequiredField]
    blocked_suppliers: list[BlockedSupplier] = Field(default_factory=list)
    weights: Weights
    delivery_full_score_days: int = Field(gt=0)
    payment_full_score_days: int = Field(gt=0)

    def blocked_reason(self, supplier_name: str) -> str | None:
        """Case- and whitespace-insensitive exact match on the supplier name."""
        key = collapse_ws(supplier_name).casefold()
        for blocked in self.blocked_suppliers:
            if collapse_ws(blocked.name).casefold() == key:
                return blocked.reason
        return None


class CustomerConfig(StrictModel):
    customer: str
    vat_rate: Decimal = Field(ge=0, lt=1)
    fx_as_of: date
    fx_to_eur: dict[Currency, Decimal]
    supplier_ids: dict[str, str] = Field(default_factory=dict)
    policy: Policy

    def supplier_id(self, supplier_name: str) -> str | None:
        """SAP supplier number, matched case- and whitespace-insensitively."""
        key = collapse_ws(supplier_name).casefold()
        for name, supplier_id in self.supplier_ids.items():
            if collapse_ws(name).casefold() == key:
                return supplier_id
        return None

    @model_validator(mode="after")
    def _eur_is_one(self) -> Self:
        if self.fx_to_eur.get("EUR") != 1:
            raise ValueError("fx_to_eur must contain EUR: 1")
        return self


def load_config(path: Path) -> CustomerConfig:
    # safe_load only builds plain types (dict/list/str/number/date). yaml.load could construct
    # arbitrary Python objects from a crafted file.
    data = yaml.safe_load(path.read_text("utf-8"))
    return CustomerConfig.model_validate(data)
