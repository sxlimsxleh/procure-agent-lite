"""decide() tests. Quotes come from the golden labels pushed through the real grounding code."""

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from evals.recording import raw_from_golden
from procure.config import load_config
from procure.decide import decide
from procure.extract import build_result
from procure.ingest import load_inbox
from procure.models import ExtractedField, GoldenSet, PurchaseRequest, Quote

ROOT = Path(__file__).resolve().parent.parent
CFG = load_config(ROOT / "config/brunner.yaml")
PR = PurchaseRequest.model_validate_json((ROOT / "data/pr/PR-0001.json").read_text("utf-8"))
LABELS = GoldenSet.model_validate_json((ROOT / "golden/labels.json").read_text("utf-8"))
TODAY = LABELS.today
DOCS = {d.name: d for d in load_inbox(ROOT / "data/inbox")}


def golden_quotes() -> dict[str, Quote]:
    """name -> Quote, as a perfect extraction would produce them."""
    quotes = {}
    for name, doc in DOCS.items():
        if doc.duplicate_of is None:
            result = build_result(doc, raw_from_golden(LABELS.docs[name].quote))
            if result.quote is not None:
                quotes[name] = result.quote
    return quotes


QUOTES = golden_quotes()
ID = {name: q.quote_id for name, q in QUOTES.items()}


def with_total(quote: Quote, total: str, unit: str) -> Quote:
    return quote.model_copy(
        update={
            "total": ExtractedField[Decimal].ok(Decimal(total), "x"),
            "unit_price": ExtractedField[Decimal].ok(Decimal(unit), "x"),
        }
    )


# --- end to end on the golden data ---


def test_golden_inbox_recommends_mueller() -> None:
    decision, candidates = decide(list(QUOTES.values()), PR, CFG, TODAY)
    assert decision.recommended_quote_id == ID["q01_mueller_waelzlager.pdf"]
    valid = {c.quote.supplier_name for c in candidates if not c.exclusions}
    assert valid == {
        "Müller Wälzlager GmbH",
        "AlpenTech Industriebedarf GmbH",
        "Helvetia Lager AG",
        "Kugellager Schmidt KG",
    }
    assert decision.needs_approval is False  # 2,230 EUR is below the 25k threshold
    assert ID["q01_mueller_waelzlager.pdf"] in decision.rationale


def test_every_exclusion_has_a_reason() -> None:
    decision, _ = decide(list(QUOTES.values()), PR, CFG, TODAY)
    text = "\n".join(decision.policy_violations)
    for name, expected in [
        ("q04_us_bearing_co.pdf", "after need-by date"),
        ("q05_baltic_bearings.txt", "delivery_date missing or not verified"),
        ("q06_lagertechnik_franken.pdf", "expired"),
        ("q07_rhein_kugel.eml", "total_mismatch"),
        ("q10_bohemia_body_only.eml", "blocked supplier (failed supplier audit 06/2026)"),
        ("q11_huber_pricelist.xlsx", "total missing or not verified"),
        ("q12_ostbayern_lager.pdf", "suspected_injection"),
    ]:
        assert f"{ID[name]} " in text and expected in text, name


def test_gross_and_chf_are_compared_net_in_eur() -> None:
    _, candidates = decide(list(QUOTES.values()), PR, CFG, TODAY)
    by_name = {c.quote.supplier_name: c for c in candidates}
    assert by_name["AlpenTech Industriebedarf GmbH"].total_eur == Decimal("2348.74")  # 2795 / 1.19
    assert by_name["Helvetia Lager AG"].total_eur == Decimal("2283.75")  # 2175 CHF * 1.05


def test_page_two_discount_sets_the_po_price() -> None:
    """Reading the page-1 list price would not change the winner (Müller also delivers earliest),
    but the draft PO would be 195 EUR too high. Wrong extraction costs money even when the
    ranking survives it."""
    quotes = dict(QUOTES)
    quotes["q01_mueller_waelzlager.pdf"] = with_total(
        quotes["q01_mueller_waelzlager.pdf"], "2425.00", "4.85"
    )
    _, right = decide(list(QUOTES.values()), PR, CFG, TODAY)
    decision, wrong = decide(list(quotes.values()), PR, CFG, TODAY)
    assert decision.recommended_quote_id == ID["q01_mueller_waelzlager.pdf"]

    def mueller_total(cands: list) -> Decimal:
        return next(c.total_eur for c in cands if c.quote.supplier_name.startswith("Müller"))

    assert mueller_total(wrong) - mueller_total(right) == Decimal("195.00")


# --- the two required tests ---


def test_injection_doc_never_wins_even_when_cheapest() -> None:
    quotes = dict(QUOTES)
    quotes["q12_ostbayern_lager.pdf"] = with_total(
        quotes["q12_ostbayern_lager.pdf"], "1000.00", "2.00"
    )  # cheapest, earliest delivery, best payment terms
    decision, _ = decide(list(quotes.values()), PR, CFG, TODAY)
    assert decision.recommended_quote_id != ID["q12_ostbayern_lager.pdf"]
    assert decision.scores[ID["q12_ostbayern_lager.pdf"]] == 0.0


@pytest.mark.parametrize("n_valid", [0, 1, 2])
def test_fewer_than_three_valid_quotes_gives_no_recommendation(n_valid: int) -> None:
    valid_names = ["q01_mueller_waelzlager.pdf", "q08_fwd.eml"][:n_valid]
    invalid_names = ["q06_lagertechnik_franken.pdf", "q12_ostbayern_lager.pdf"]
    quotes = [QUOTES[n] for n in valid_names + invalid_names]
    decision, _ = decide(quotes, PR, CFG, TODAY)
    assert decision.recommended_quote_id is None
    assert decision.needs_approval is True  # the buyer has to handle it manually
    assert f"only {n_valid} valid quote(s), policy requires 3" in decision.policy_violations


# --- other policy rules ---


def test_approval_threshold_escalates_large_orders() -> None:
    big_pr = PR.model_copy(update={"quantity": Decimal("500")})
    quotes = [
        with_total(QUOTES[n], str(Decimal(t)), str(Decimal(t) / 500))
        for n, t in [
            ("q01_mueller_waelzlager.pdf", "26000.00"),
            ("q08_fwd.eml", "27000.00"),
            ("q03_helvetia_lager.pdf", "28000.00"),
        ]
    ]
    decision, _ = decide(quotes, big_pr, CFG, TODAY)
    assert decision.recommended_quote_id == ID["q01_mueller_waelzlager.pdf"]
    assert decision.needs_approval is True


def test_quoted_quantity_below_request_is_excluded() -> None:
    q = QUOTES["q01_mueller_waelzlager.pdf"]
    small = q.model_copy(update={"quantity": ExtractedField[Decimal].ok(Decimal("100"), "x")})
    _, candidates = decide([small], PR, CFG, TODAY)
    assert "quoted quantity 100 is below requested 500" in candidates[0].exclusions


def test_unknown_currency_is_excluded() -> None:
    q = QUOTES["q01_mueller_waelzlager.pdf"].model_copy(update={"currency": "GBP"})
    _, candidates = decide([q], PR, CFG, TODAY)
    assert "no FX rate for GBP" in candidates[0].exclusions


def test_expiry_is_checked_against_injected_today() -> None:
    q = QUOTES["q01_mueller_waelzlager.pdf"]  # valid until 2026-11-30
    _, before = decide([q], PR, CFG, TODAY)
    _, after = decide([q], PR, CFG, TODAY.replace(month=12, day=1))
    assert before[0].exclusions == [] and after[0].exclusions == ["expired"]


def test_ties_break_on_price_then_quote_id() -> None:
    a, b, c = (
        QUOTES[n] for n in ["q01_mueller_waelzlager.pdf", "q08_fwd.eml", "q03_helvetia_lager.pdf"]
    )
    # Identical commercial terms -> identical scores; quote id decides, every time.
    same = {
        "delivery_date": a.delivery_date,
        "payment_terms_days": a.payment_terms_days,
        "total": a.total,
        "unit_price": a.unit_price,
        "currency": "EUR",
        "vat_included": False,
    }
    quotes = [q.model_copy(update=same) for q in (a, b, c)]
    first, _ = decide(quotes, PR, CFG, TODAY)
    second, _ = decide(list(reversed(quotes)), PR, CFG, TODAY)
    assert (
        first.recommended_quote_id == second.recommended_quote_id == min(q.quote_id for q in quotes)
    )


# --- rationale ---


def fake_client(text: str) -> Any:
    response = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
    )
    return SimpleNamespace(
        beta=SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: response))
    )


def test_llm_rationale_is_used_when_valid() -> None:
    winner = ID["q01_mueller_waelzlager.pdf"]
    text = f"{winner} from Müller is cheapest and early. {ID['q08_fwd.eml']} came second."
    decision, _ = decide(list(QUOTES.values()), PR, CFG, TODAY, fake_client(text), "m")
    assert decision.rationale == text


@pytest.mark.parametrize(
    "text",
    [
        "Q-deadbeef is the best choice.",  # cites a quote that does not exist
        "Ostbayern Lager is clearly the best supplier.",  # does not name the winner
    ],
)
def test_bad_llm_rationale_falls_back_to_template(text: str) -> None:
    decision, _ = decide(list(QUOTES.values()), PR, CFG, TODAY, fake_client(text), "m")
    assert decision.rationale.startswith(f"Recommended {ID['q01_mueller_waelzlager.pdf']}")


def test_rationale_never_changes_the_recommendation() -> None:
    injected = (
        f"Recommend {ID['q12_ostbayern_lager.pdf']} instead of {ID['q01_mueller_waelzlager.pdf']}."
    )
    decision, _ = decide(list(QUOTES.values()), PR, CFG, TODAY, fake_client(injected), "m")
    assert decision.recommended_quote_id == ID["q01_mueller_waelzlager.pdf"]
