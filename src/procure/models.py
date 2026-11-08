"""Data contracts shared by every pipeline step.

Money and quantities are `Decimal`, never `float`: 0.1 + 0.2 != 0.3 in binary floating point,
and a PO amount that is off by a cent is a real bug. Pydantic serialises Decimal to JSON as a
string ("4.46"), so values survive a round trip through files and the audit log unchanged.
"""

import re
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Generic, Literal, Self, TypeVar

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

# ISO 4217 code such as "EUR", "CHF", "USD".
Currency = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]

# Quote ids look like "Q-1a2b3c4d" (derived from the document hash in ingest).
# The pattern lives here so Decision can find quote ids inside free-text rationales.
QUOTE_ID_PATTERN = r"Q-[0-9a-f]{8}"
_QUOTE_ID_RE = re.compile(rf"\b{QUOTE_ID_PATTERN}\b")

T = TypeVar("T")


class StrictModel(BaseModel):
    # forbid: a typo in a field name (e.g. from LLM output or a hand-written label) is an error,
    # not a silently ignored key.
    model_config = ConfigDict(extra="forbid")


class QuoteFlag(StrEnum):
    """Document-level problems. Missing/ungrounded single fields are flagged on the field itself."""

    EXPIRED = "expired"  # valid_until is before `today`
    TOTAL_MISMATCH = "total_mismatch"  # stated total != quantity x unit price
    DUPLICATE = "duplicate"  # same document already seen (sha256 match)
    SUSPECTED_INJECTION = "suspected_injection"  # document text tries to instruct the system
    NOT_A_QUOTE = "not_a_quote"  # e.g. a newsletter that landed in the inbox
    SUPERSEDED = "superseded"  # the supplier sent a newer revision of this quote


class ExtractedField(StrictModel, Generic[T]):
    """One value read from a document, plus the evidence for it.

    Invariant (design rule 2, "grounded or flagged"):
      - status "ok"      -> value and verbatim source_text are both present
      - status "flagged" -> value is None and a reason says why
    """

    value: T | None = None
    source_text: str | None = None
    status: Literal["ok", "flagged"]
    reason: str | None = None

    @model_validator(mode="after")
    def _check_invariant(self) -> Self:
        if self.status == "ok":
            if self.value is None or not self.source_text:
                raise ValueError("an 'ok' field needs both a value and source_text")
        else:
            if self.value is not None:
                raise ValueError("a 'flagged' field must not carry a value (never guess)")
            if not self.reason:
                raise ValueError("a 'flagged' field needs a reason")
        return self

    @classmethod
    def ok(cls, value: T, source_text: str) -> Self:
        return cls(value=value, source_text=source_text, status="ok")

    @classmethod
    def flagged(cls, reason: str, source_text: str | None = None) -> Self:
        return cls(status="flagged", reason=reason, source_text=source_text)


class PurchaseRequest(StrictModel):
    pr_id: str = Field(pattern=r"^PR-\d{4}$")
    requester: str
    company_code: str
    plant: str
    material: str
    description: str
    quantity: Decimal = Field(gt=0)
    unit: str
    need_by: date
    budget_eur: Decimal | None = Field(default=None, gt=0)


class Quote(StrictModel):
    quote_id: str = Field(pattern=rf"^{QUOTE_ID_PATTERN}$")
    doc_hash: str = Field(pattern=r"^[0-9a-f]{64}$")  # sha256 hex
    supplier_name: str
    currency: Currency
    unit_price: ExtractedField[Decimal]
    quantity: ExtractedField[Decimal]
    total: ExtractedField[Decimal]
    vat_included: bool | None  # None = the document does not say
    delivery_date: ExtractedField[date]
    valid_until: ExtractedField[date]
    payment_terms_days: ExtractedField[int]
    flags: list[QuoteFlag] = Field(default_factory=list)


class Decision(StrictModel):
    recommended_quote_id: str | None
    scores: dict[str, float]
    policy_violations: list[str]
    needs_approval: bool
    rationale: str

    @model_validator(mode="after")
    def _ids_must_exist(self) -> Self:
        # The rationale is written by the LLM; it must not cite quotes we never scored.
        known = set(self.scores)
        if self.recommended_quote_id is not None and self.recommended_quote_id not in known:
            raise ValueError(f"recommended quote {self.recommended_quote_id} has no score")
        unknown = set(_QUOTE_ID_RE.findall(self.rationale)) - known
        if unknown:
            raise ValueError(f"rationale cites unknown quote ids: {sorted(unknown)}")
        return self


# SAP field names are kept as-is (PascalCase) so the payload maps 1:1 to the OData API.
class DraftPOItem(StrictModel):
    Material: str
    OrderQuantity: Decimal = Field(gt=0)
    NetPriceAmount: Decimal = Field(gt=0)


class DraftPO(StrictModel):
    PurchaseOrderType: Literal["NB"] = "NB"  # NB = standard purchase order in SAP
    CompanyCode: str
    Supplier: str = Field(min_length=1)
    DocumentCurrency: Currency
    # SAP's deep-insert navigation property for items; we keep the short name in Python.
    items: list[DraftPOItem] = Field(min_length=1, serialization_alias="to_PurchaseOrderItem")


class Usage(StrictModel):
    """Token counts of one LLM call, for cost logging."""

    input_tokens: int = 0
    output_tokens: int = 0


class ExtractionResult(StrictModel):
    """Outcome of processing one inbox document. Exactly one of quote / skipped / error is set."""

    doc_name: str
    doc_hash: str
    quote: Quote | None = None
    skipped: Literal[QuoteFlag.DUPLICATE, QuoteFlag.NOT_A_QUOTE] | None = None
    duplicate_of: str | None = None
    error: str | None = None  # e.g. the model did not call record_quote
    usage: Usage = Field(default_factory=Usage)

    @model_validator(mode="after")
    def _exactly_one_outcome(self) -> Self:
        outcomes = [self.quote is not None, self.skipped is not None, self.error is not None]
        if sum(outcomes) != 1:
            raise ValueError("exactly one of quote, skipped, error must be set")
        return self

    @property
    def flags(self) -> list[QuoteFlag]:
        """Document-level flags, comparable with GoldenDoc.flags."""
        if self.quote is not None:
            return list(self.quote.flags)
        return [self.skipped] if self.skipped is not None else []


class NormalisedQuote(StrictModel):
    """A quote with prices made comparable: net of VAT and in EUR. None = cannot be computed."""

    quote: Quote
    unit_price_net_eur: Decimal | None
    total_net_eur: Decimal | None


# --- Golden labels (golden/labels.json) ---------------------------------------------------------
# Hand-labelled truth for evals. Labels reuse ExtractedField, so a label that breaks the
# "grounded or flagged" invariant fails to load.


class GoldenQuote(StrictModel):
    """What a perfect extraction of one document looks like (Quote minus ids and flags)."""

    supplier_name: str
    currency: Currency
    unit_price: ExtractedField[Decimal]
    quantity: ExtractedField[Decimal]
    total: ExtractedField[Decimal]
    vat_included: bool | None
    delivery_date: ExtractedField[date]
    valid_until: ExtractedField[date]
    payment_terms_days: ExtractedField[int]


class GoldenDoc(StrictModel):
    quote: GoldenQuote | None  # None for non-quotes and for duplicates
    flags: list[QuoteFlag]
    duplicate_of: str | None = None  # filename of the first copy
    notes: str  # what this document is meant to test

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        is_dup = QuoteFlag.DUPLICATE in self.flags
        if is_dup != (self.duplicate_of is not None):
            raise ValueError("'duplicate' flag and duplicate_of must go together")
        not_quote = QuoteFlag.NOT_A_QUOTE in self.flags
        if (self.quote is None) != (is_dup or not_quote):
            raise ValueError("quote must be None exactly for duplicates and non-quotes")
        return self


class GoldenSet(StrictModel):
    today: date  # the injected `today` the expected flags (e.g. expired) assume
    pr_id: str
    docs: dict[str, GoldenDoc]  # key: filename in data/inbox/
