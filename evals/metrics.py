"""Scoring extraction results against golden/labels.json. Pure functions, no I/O.

Field outcomes (per document, per field):
  correct          value matches the label, or both are flagged (correctly flagged)
  wrong_accepted   status "ok" but the value is wrong or should not exist   <- the dangerous one
  wrongly_flagged  label has a value but we flagged it (safe, costs the buyer time)
"""

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

from procure.extract import FIELD_KINDS
from procure.ingest import collapse_ws
from procure.models import ExtractionResult, GoldenDoc, GoldenSet, Quote, QuoteFlag

PLAIN_FIELDS = ["supplier_name", "currency", "vat_included"]
ALL_FIELDS = PLAIN_FIELDS + list(FIELD_KINDS)
# Scored and shown, but not part of "All fields" (and so not of the gate): it does not carry
# money itself and only matters through the `superseded` flag, which is scored separately.
# Keeping it out also keeps "All fields" comparable with the baselines before milestone 3b.
EXTRA_FIELDS = ["quote_number"]

Outcome = Literal["correct", "wrong_accepted", "wrongly_flagged"]


@dataclass
class FieldScore:
    correct: int = 0
    wrong_accepted: int = 0
    wrongly_flagged: int = 0

    @property
    def total(self) -> int:
        return self.correct + self.wrong_accepted + self.wrongly_flagged

    @property
    def accuracy(self) -> float | None:
        return self.correct / self.total if self.total else None

    def add(self, outcome: Outcome) -> None:
        setattr(self, outcome, getattr(self, outcome) + 1)


@dataclass
class FlagScore:
    tp: int = 0
    fp: int = 0
    fn: int = 0

    @property
    def precision(self) -> float | None:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else None

    @property
    def recall(self) -> float | None:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else None


@dataclass
class Failure:
    doc: str
    field: str
    outcome: Outcome | str
    expected: Any
    got: Any
    reason: str | None = None


@dataclass
class EvalReport:
    fields: dict[str, FieldScore] = field(default_factory=dict)
    flags: dict[str, FlagScore] = field(default_factory=dict)
    docs_correct: int = 0
    docs_total: int = 0
    failures: list[Failure] = field(default_factory=list)

    @property
    def overall(self) -> FieldScore:
        total = FieldScore()
        for name, s in self.fields.items():
            if name in EXTRA_FIELDS:
                continue
            total.correct += s.correct
            total.wrong_accepted += s.wrong_accepted
            total.wrongly_flagged += s.wrongly_flagged
        return total


def _same_plain(name: str, got: Any, want: Any) -> bool:
    if name == "supplier_name" and got is not None and want is not None:
        return collapse_ws(got).casefold() == collapse_ws(want).casefold()
    if name == "quote_number" and got is not None and want is not None:
        # "#USB-77812" / "Nr. USB-77812" / "USB-77812" are the same number
        def bare(n: str) -> str:
            return re.sub(r"^(#|nr\.?|no\.?)\s*", "", collapse_ws(n).casefold())

        return bare(got) == bare(want)
    return got == want


def _same_value(got: Any, want: Any) -> bool:
    if isinstance(got, Decimal | int) and isinstance(want, Decimal | int):
        return Decimal(got) == Decimal(want)
    return got == want


def score_field(
    name: str, got_quote: Quote | None, want: GoldenDoc
) -> tuple[Outcome, Failure | None]:
    """Score one field of one document. got_quote None = we produced no quote for this doc."""
    want_quote = want.quote
    if name in PLAIN_FIELDS or name in EXTRA_FIELDS:
        got_v = getattr(got_quote, name) if got_quote else None
        want_v = getattr(want_quote, name) if want_quote else None
        if got_quote is None:
            outcome: Outcome = "correct" if want_quote is None else "wrongly_flagged"
        elif _same_plain(name, got_v, want_v):
            outcome = "correct"
        else:
            outcome = "wrong_accepted"
        return outcome, None if outcome == "correct" else Failure("", name, outcome, want_v, got_v)

    got_f = getattr(got_quote, name) if got_quote else None
    want_f = getattr(want_quote, name) if want_quote else None
    got_ok = got_f is not None and got_f.status == "ok"
    want_ok = want_f is not None and want_f.status == "ok"
    if got_ok and want_ok and _same_value(got_f.value, want_f.value):  # type: ignore[union-attr]
        outcome = "correct"
    elif got_ok:
        outcome = "wrong_accepted"  # wrong value, or a value where the label says there is none
    elif want_ok:
        outcome = "wrongly_flagged"
    else:
        outcome = "correct"  # correctly flagged / correctly absent
    if outcome == "correct":
        return outcome, None
    return outcome, Failure(
        "",
        name,
        outcome,
        want_f.value if want_f else None,
        got_f.value if got_f else None,
        got_f.reason if got_f else "no quote produced",
    )


def _doc_kind(flags: list[QuoteFlag], has_quote: bool, error: str | None) -> str:
    if error:
        return "error"
    if QuoteFlag.DUPLICATE in flags:
        return "duplicate"
    if QuoteFlag.NOT_A_QUOTE in flags:
        return "not_a_quote"
    return "quote" if has_quote else "none"


def evaluate(results: dict[str, ExtractionResult], labels: GoldenSet) -> EvalReport:
    """Score `results` (keyed by doc name) against the labels. Missing results count as errors."""
    report = EvalReport(
        fields={name: FieldScore() for name in ALL_FIELDS + EXTRA_FIELDS},
        flags={flag.value: FlagScore() for flag in QuoteFlag},
    )
    field_flags = FlagScore()  # field-level: status "flagged" as the positive class

    for doc_name, want in sorted(labels.docs.items()):
        result = results.get(doc_name)
        got_quote = result.quote if result else None
        got_flags = result.flags if result else []
        error = (result.error if result else "no result") or None

        # Document outcome
        report.docs_total += 1
        want_kind = _doc_kind(want.flags, want.quote is not None, None)
        got_kind = _doc_kind(got_flags, got_quote is not None, error)
        if want_kind == got_kind and (result is None or result.duplicate_of == want.duplicate_of):
            report.docs_correct += 1
        else:
            report.failures.append(
                Failure(doc_name, "(document)", "wrong outcome", want_kind, got_kind, error)
            )

        # Fields: scored for every doc that should be, or wrongly became, a quote
        if want.quote is not None or got_quote is not None:
            for name in ALL_FIELDS + EXTRA_FIELDS:
                outcome, failure = score_field(name, got_quote, want)
                report.fields[name].add(outcome)
                if failure:
                    failure.doc = doc_name
                    report.failures.append(failure)
                if name in FIELD_KINDS:
                    got_flagged = got_quote is None or getattr(got_quote, name).status == "flagged"
                    want_flagged = (
                        want.quote is None or getattr(want.quote, name).status == "flagged"
                    )
                    field_flags.tp += got_flagged and want_flagged
                    field_flags.fp += got_flagged and not want_flagged
                    field_flags.fn += want_flagged and not got_flagged

        # Document flags
        for flag in QuoteFlag:
            got_has, want_has = flag in got_flags, flag in want.flags
            score = report.flags[flag.value]
            score.tp += got_has and want_has
            score.fp += got_has and not want_has
            score.fn += want_has and not got_has

    report.flags["field flagged (any field)"] = field_flags
    return report
