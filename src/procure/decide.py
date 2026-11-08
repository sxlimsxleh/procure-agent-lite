"""Policy checks and deterministic scoring (design rule 1: code decides, the LLM only explains).

decide() takes grounded quotes and returns a Decision:
  1. exclude quotes that break a policy, recording why
  2. if fewer than `min_valid_quotes` remain: no recommendation, the buyer handles it manually
  3. score the remaining quotes; best score wins, ties -> lower price, then quote id
  4. write a rationale: LLM text if it passes validation, otherwise a fixed template

The LLM sees only this module's structured result, never document text, and cannot change the
recommendation: its text is checked to cite only scored quote ids and to name the winner.
"""

import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

import anthropic
from pydantic import ValidationError

from procure.config import CustomerConfig
from procure.models import Decision, NormalisedQuote, PurchaseRequest, Quote, QuoteFlag
from procure.normalise import check_quote, normalise_quote

ALWAYS_EXCLUDED_FLAGS = [QuoteFlag.SUSPECTED_INJECTION, QuoteFlag.EXPIRED, QuoteFlag.TOTAL_MISMATCH]


@dataclass
class Candidate:
    """One quote on its way through decide(). Kept for the approval UI (milestone 6)."""

    quote: Quote
    normalised: NormalisedQuote | None  # None if the currency has no FX rate
    exclusions: list[str] = field(default_factory=list)
    score: float = 0.0

    @property
    def total_eur(self) -> Decimal | None:
        return self.normalised.total_net_eur if self.normalised else None


# --- 1. Exclusions ----------------------------------------------------------------------------


def exclusion_reasons(
    quote: Quote, normalised: NormalisedQuote | None, pr: PurchaseRequest, cfg: CustomerConfig
) -> list[str]:
    reasons = [flag.value for flag in ALWAYS_EXCLUDED_FLAGS if flag in quote.flags]
    for name in cfg.policy.required_fields:
        if getattr(quote, name).status != "ok":
            reasons.append(f"{name} missing or not verified")
    blocked = cfg.policy.blocked_reason(quote.supplier_name)
    if blocked:
        reasons.append(f"blocked supplier ({blocked})")
    delivery = quote.delivery_date.value
    if delivery is not None and delivery > pr.need_by:
        reasons.append(f"delivery {delivery} is after need-by date {pr.need_by}")
    quantity = quote.quantity.value
    if quantity is not None and quantity < pr.quantity:
        reasons.append(f"quoted quantity {quantity} is below requested {pr.quantity}")
    if normalised is None:
        reasons.append(f"no FX rate for {quote.currency}")
    elif normalised.total_net_eur is None and not reasons:
        reasons.append("price not comparable (net/gross unknown)")
    return reasons


# --- 3. Scoring -------------------------------------------------------------------------------


def score(c: Candidate, cheapest: Decimal, pr: PurchaseRequest, cfg: CustomerConfig) -> float:
    """Weighted sum of price, delivery and payment sub-scores, each in [0, 1]."""
    p = cfg.policy
    assert c.total_eur is not None and c.quote.delivery_date.value is not None
    price_score = cheapest / c.total_eur
    days_early = (pr.need_by - c.quote.delivery_date.value).days
    full_delivery, full_payment = p.delivery_full_score_days, p.payment_full_score_days
    delivery_score = Decimal(min(days_early, full_delivery)) / full_delivery
    payment_days = c.quote.payment_terms_days.value or 0  # not stated -> no credit for it
    payment_score = Decimal(min(payment_days, full_payment)) / full_payment
    total = (
        p.weights.price * price_score
        + p.weights.delivery * delivery_score
        + p.weights.payment_terms * payment_score
    )
    return float(round(total, 4))


def rank(valid: list[Candidate]) -> list[Candidate]:
    """Best first. Ties: lower price wins, then quote id, so the order never depends on luck."""
    return sorted(valid, key=lambda c: (-c.score, c.total_eur, c.quote.quote_id))


# --- 4. Rationale -----------------------------------------------------------------------------


def decision_facts(
    winner: Candidate | None,
    ranked: list[Candidate],
    excluded: list[Candidate],
    pr: PurchaseRequest,
    cfg: CustomerConfig,
) -> dict[str, Any]:
    """Everything the rationale may talk about. Plain data, no document text."""
    return {
        "purchase_request": (
            f"{pr.pr_id}: {pr.quantity} {pr.unit} {pr.description}, need by {pr.need_by}"
        ),
        "min_valid_quotes": cfg.policy.min_valid_quotes,
        "recommended": winner.quote.quote_id if winner else None,
        "valid_quotes": [
            {
                "quote_id": c.quote.quote_id,
                "supplier": c.quote.supplier_name,
                "score": c.score,
                "total_net_eur": str(c.total_eur),
                "delivery_date": str(c.quote.delivery_date.value),
                "payment_terms_days": c.quote.payment_terms_days.value,
            }
            for c in ranked
        ],
        "excluded_quotes": [
            {
                "quote_id": c.quote.quote_id,
                "supplier": c.quote.supplier_name,
                "reasons": c.exclusions,
            }
            for c in excluded
        ],
    }


def template_rationale(facts: dict[str, Any]) -> str:
    valid, excluded = facts["valid_quotes"], facts["excluded_quotes"]
    if facts["recommended"] is None:
        text = (
            f"No recommendation: only {len(valid)} valid quote(s), policy requires "
            f"{facts['min_valid_quotes']}."
        )
    else:
        best = valid[0]
        text = (
            f"Recommended {best['quote_id']} ({best['supplier']}): highest score {best['score']} "
            f"with {best['total_net_eur']} EUR net, delivery {best['delivery_date']}."
        )
        if len(valid) > 1:
            runner_up = valid[1]
            text += (
                f" Runner-up {runner_up['quote_id']} ({runner_up['supplier']}) "
                f"scored {runner_up['score']}."
            )
    for e in excluded:
        text += f" Excluded {e['quote_id']} ({e['supplier']}): {'; '.join(e['reasons'])}."
    return text


RATIONALE_SYSTEM_PROMPT = """\
You write short explanations of purchasing decisions for a buyer at Brunner Antriebstechnik GmbH.
The decision has already been made by policy code; you only explain it. Never recommend a \
different quote. Write 3 to 5 plain sentences. Refer to quotes by their quote_id (like \
Q-1a2b3c4d) and supplier name. Mention the recommended quote, why it won, and the most important \
exclusions. Use only the facts given."""


def llm_rationale(client: Any, model: str, facts: dict[str, Any]) -> str | None:
    """Ask Claude for a rationale. No tools: it can only return text. None on any problem."""
    response = client.beta.messages.create(
        model=model,
        max_tokens=1024,
        system=RATIONALE_SYSTEM_PROMPT,
        output_config={"effort": "low"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=[{"role": "user", "content": json.dumps(facts, indent=2, ensure_ascii=False)}],
    )
    if response.stop_reason == "refusal":
        return None
    text = " ".join(b.text for b in response.content if b.type == "text").strip()
    return text or None


# --- decide() ---------------------------------------------------------------------------------


def decide(
    quotes: list[Quote],
    pr: PurchaseRequest,
    cfg: CustomerConfig,
    today: date,
    client: Any | None = None,
    model: str | None = None,
) -> tuple[Decision, list[Candidate]]:
    """Return the Decision and the candidates behind it (for the approval UI and audit log).

    Without a client the rationale is the template, so tests and CI need no API key.
    """
    candidates = []
    for quote in quotes:
        quote = check_quote(quote, today)  # idempotent: expiry is always judged against `today`
        normalised = (
            normalise_quote(quote, cfg.vat_rate, cfg.fx_to_eur)
            if quote.currency in cfg.fx_to_eur
            else None
        )
        c = Candidate(quote=quote, normalised=normalised)
        c.exclusions = exclusion_reasons(quote, normalised, pr, cfg)
        candidates.append(c)

    valid = [c for c in candidates if not c.exclusions]
    excluded = [c for c in candidates if c.exclusions]
    if valid:
        cheapest = min(c.total_eur for c in valid if c.total_eur is not None)
        for c in valid:
            c.score = score(c, cheapest, pr, cfg)
    ranked = rank(valid)

    violations = [
        f"{c.quote.quote_id} ({c.quote.supplier_name}): {reason}"
        for c in excluded
        for reason in c.exclusions
    ]
    winner = ranked[0] if len(ranked) >= cfg.policy.min_valid_quotes else None
    if winner is None:
        violations.append(
            f"only {len(ranked)} valid quote(s), policy requires {cfg.policy.min_valid_quotes}"
        )
    needs_approval = (
        winner is None  # manual handling by the buyer
        or (winner.total_eur is not None and winner.total_eur > cfg.policy.approval_threshold_eur)
    )
    fields: dict[str, Any] = {
        "recommended_quote_id": winner.quote.quote_id if winner else None,
        "scores": {c.quote.quote_id: c.score for c in candidates},
        "policy_violations": violations,
        "needs_approval": needs_approval,
    }

    facts = decision_facts(winner, ranked, excluded, pr, cfg)
    template = template_rationale(facts)
    if client is not None and model is not None:
        try:
            text = llm_rationale(client, model, facts)
        except anthropic.APIError:
            text = None  # the decision stands; only the explanation falls back to the template
        if text and (winner is None or winner.quote.quote_id in text):
            try:
                return Decision(**fields, rationale=text), candidates
            except ValidationError:
                pass  # cited an unknown quote id -> fall back to the template
    return Decision(**fields, rationale=template), candidates
