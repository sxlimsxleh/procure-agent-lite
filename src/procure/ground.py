"""Grounding: accept an extracted value only if the document really says it (design rule 2).

Approach: we never parse a document blind. The LLM proposes a value plus the verbatim span it
read it from; code then checks (1) the span occurs in the document and (2) the value is one of
the possible readings of a number/date in that span. "Possible readings" means we do not need to
know whether a document uses German or English formats: "2.230,00" can only be 2230.00 (the
English reading is invalid), while "1.000" could be 1000 or 1.000 and both are accepted.
"""

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Literal

from procure.ingest import collapse_ws
from procure.models import ExtractedField

FieldKind = Literal["decimal", "int", "date"]

# --- Numbers --------------------------------------------------------------------------------

_NUMBER_TOKEN = re.compile(r"\d+(?:[.,']\d+)*")

# (thousands separator, decimal separator)
_NUMBER_STYLES = [
    (".", ","),  # German: 1.234,56
    (",", "."),  # English: 1,234.56
    ("'", "."),  # Swiss: 1'234.56
]


def _read_number(token: str, thousands: str, decimal: str) -> Decimal | None:
    """Read token in one style, or None if the token is not valid in that style."""
    if token.count(decimal) > 1:
        return None
    int_part, _, frac = token.partition(decimal)
    groups = int_part.split(thousands)
    # With thousands separators, every group after the first must have exactly 3 digits.
    # This is what stops "4,46" from being read as 446 in English style.
    if len(groups) > 1 and (not 1 <= len(groups[0]) <= 3 or any(len(g) != 3 for g in groups[1:])):
        return None
    digits = "".join(groups)
    if not digits.isdigit() or (frac and not frac.isdigit()):
        return None
    try:
        return Decimal(f"{digits}.{frac}" if frac else digits)
    except InvalidOperation:
        return None


def number_candidates(text: str) -> set[Decimal]:
    """Every value any number in `text` could mean, across German/English/Swiss formats."""
    found: set[Decimal] = set()
    for token in _NUMBER_TOKEN.findall(text):
        for thousands, decimal in _NUMBER_STYLES:
            value = _read_number(token, thousands, decimal)
            if value is not None:
                found.add(value)
    return found


# --- Dates ----------------------------------------------------------------------------------

MONTHS = {
    # English
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    # German (+ Austrian "Jänner")
    "januar": 1, "jänner": 1, "februar": 2, "märz": 3, "maerz": 3, "mai": 5, "juni": 6,
    "juli": 7, "oktober": 10, "dezember": 12,
}  # fmt: skip

_DMY_DOTS = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")  # 15.12.2026
_ISO = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")  # 2026-12-15
_SLASHES = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")  # 12/15/2026 (US) or 15/12/2026
_DAY_MONTH = re.compile(r"\b(\d{1,2})\.?\s+([^\W\d_]+)\s+(\d{4})\b")  # 9 December 2026
_MONTH_DAY = re.compile(r"\b([^\W\d_]+)\s+(\d{1,2}),?\s+(\d{4})\b")  # December 18, 2026


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def date_candidates(text: str) -> set[date]:
    """Every date `text` could mean. Slash dates are ambiguous, so both readings are kept."""
    found: list[date | None] = []
    for d, m, y in _DMY_DOTS.findall(text):
        found.append(_safe_date(int(y), int(m), int(d)))
    for y, m, d in _ISO.findall(text):
        found.append(_safe_date(int(y), int(m), int(d)))
    for a, b, y in _SLASHES.findall(text):
        found.append(_safe_date(int(y), int(a), int(b)))  # MM/DD
        found.append(_safe_date(int(y), int(b), int(a)))  # DD/MM
    for d, month, y in _DAY_MONTH.findall(text):
        if month.lower() in MONTHS:
            found.append(_safe_date(int(y), MONTHS[month.lower()], int(d)))
    for month, d, y in _MONTH_DAY.findall(text):
        if month.lower() in MONTHS:
            found.append(_safe_date(int(y), MONTHS[month.lower()], int(d)))
    return {d for d in found if d is not None}


# --- Grounding ------------------------------------------------------------------------------


def _parse_value(raw: str, kind: FieldKind) -> Decimal | int | date | None:
    """Parse the LLM's canonical value ("2230.00", "30", "2026-12-15")."""
    try:
        match kind:
            case "decimal":
                return Decimal(raw)
            case "int":
                return int(raw)
            case "date":
                return date.fromisoformat(raw)
    except (InvalidOperation, ValueError):
        return None


def _value_in_span(value: Decimal | int | date, span: str, kind: FieldKind) -> bool:
    if kind == "date":
        return value in date_candidates(span)
    return Decimal(value) in number_candidates(span)  # type: ignore[arg-type]


def ground_field(
    raw_value: str | None, source_text: str | None, doc_text: str, kind: FieldKind
) -> ExtractedField:
    """Turn an LLM-proposed (value, source_text) pair into an ok or flagged ExtractedField.

    The LLM never decides the status; this function does.
    """
    if raw_value is None:
        return ExtractedField.flagged("not stated in document", source_text)
    if not source_text:
        return ExtractedField.flagged("no source_text given")
    value = _parse_value(raw_value, kind)
    if value is None:
        return ExtractedField.flagged(f"value {raw_value!r} is not a valid {kind}", source_text)
    if collapse_ws(source_text) not in collapse_ws(doc_text):
        return ExtractedField.flagged("source_text not found in document", source_text)
    if not _value_in_span(value, source_text, kind):
        return ExtractedField.flagged(f"value {raw_value} not found in source_text", source_text)
    return ExtractedField.ok(value, source_text)


# --- Prompt injection -----------------------------------------------------------------------

# Deliberately simple and readable. This is a tripwire that works even if the LLM misses the
# injection, not a complete defence: the real defence is that the LLM can only record data and
# never chooses a supplier or triggers a write.
_INJECTION_PATTERNS = [
    r"ignore\s+(all\s+|any\s+)?(the\s+)?(previous|prior|above|earlier)\s+instructions",
    r"disregard\s+.{0,40}instructions",
    r"\b(ai|ki|llm)[\s-]+(assist|agent|model)\w*",  # also "KI-Assistenten"
    r"system\s*prompt",
    r"ignorier\w*\s+.{0,40}(anweisungen|instruktionen)",
    r"you\s+are\s+now\b",
]
_INJECTION_RE = re.compile("|".join(_INJECTION_PATTERNS), re.IGNORECASE)


def find_injection(text: str) -> str | None:
    """Return the first suspicious snippet in `text`, or None."""
    match = _INJECTION_RE.search(collapse_ws(text))
    return match.group(0) if match else None
