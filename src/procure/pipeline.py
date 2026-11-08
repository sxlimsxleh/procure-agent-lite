"""The end-to-end flow, in two phases because approval waits for a person.

  propose(pr_id)   inbox -> extract -> decide -> draft PO; written to the audit log as a proposal
  approve(run_id)  a human approves or rejects; once enough approvers said yes, write to SAP
  retry_po(run_id) SAP never confirmed -> send again (safe: same idempotency key)

The approval queue is *derived from the audit log*: a run is open until it has a po_created or a
rejection event. There is no separate status field that could disagree with the audit trail.

CLI:
  python -m procure.pipeline propose PR-0001 [--source recorded|live]
  python -m procure.pipeline queue
  python -m procure.pipeline approve <run_id> --approver "Anna Leitner" [--reject]
  python -m procure.pipeline retry <run_id> --actor "Anna Leitner"
  python -m procure.pipeline demo PR-0001 --approver "Anna Leitner" [--yes]
"""

import argparse
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import httpx

from procure.audit import AuditEvent, AuditLog
from procure.config import CustomerConfig, load_config
from procure.costs import estimate_cost_usd
from procure.decide import decide
from procure.erp_client import (
    ErpClient,
    ErpError,
    MissingSupplierError,
    RetryableError,
    build_draft_po,
)
from procure.extract import build_result, duplicate_result, extract_document
from procure.ingest import Document, load_inbox
from procure.logging import log_event, log_step
from procure.models import (
    Decision,
    DraftPO,
    ExtractionResult,
    PurchaseRequest,
    Quote,
    StrictModel,
    Usage,
)
from procure.recordings import load_recording

ROOT = Path(__file__).resolve().parents[2]
Source = Literal["recorded", "live"]


@dataclass(frozen=True)
class Paths:
    inbox: Path = ROOT / "data" / "inbox"
    prs: Path = ROOT / "data" / "pr"
    config: Path = ROOT / "config" / "brunner.yaml"
    recordings: Path = ROOT / "evals" / "recordings"


DEFAULT_PATHS = Paths()  # frozen, so safe to share as a default argument


# --- What the approver sees ---------------------------------------------------------------------


class QuoteView(StrictModel):
    doc_name: str
    quote: Quote
    total_net_eur: Decimal | None
    score: float
    exclusions: list[str]


class SkippedDoc(StrictModel):
    doc_name: str
    reason: str


class Proposal(StrictModel):
    """Everything needed to approve a run, stored in the audit log as one event."""

    run_id: str
    pr: PurchaseRequest
    decision: Decision
    quotes: list[QuoteView]
    skipped: list[SkippedDoc]
    draft_po: DraftPO | None
    idempotency_key: str | None
    po_blocked_reason: str | None  # why there is nothing to approve, if so
    required_approvers: int
    usage: Usage
    est_cost_usd: Decimal | None

    @property
    def winner(self) -> QuoteView | None:
        rid = self.decision.recommended_quote_id
        return next((q for q in self.quotes if q.quote.quote_id == rid), None)


# --- Phase 1: propose -----------------------------------------------------------------------


def new_run_id() -> str:
    return f"run-{uuid.uuid4().hex[:12]}"


def load_pr(pr_id: str, paths: Paths = DEFAULT_PATHS) -> PurchaseRequest:
    return PurchaseRequest.model_validate_json((paths.prs / f"{pr_id}.json").read_text("utf-8"))


def _extract(
    doc: Document, pr: PurchaseRequest, source: Source, paths: Paths, client: Any, model: str | None
) -> ExtractionResult:
    if doc.duplicate_of is not None:
        return duplicate_result(doc)
    if source == "live":
        return extract_document(client, doc, pr, model)
    rec = load_recording(paths.recordings, doc)
    return build_result(doc, rec.tool_input, rec.usage)


def _skip_reason(result: ExtractionResult) -> str:
    if result.duplicate_of:
        return f"duplicate of {result.duplicate_of}"
    if result.skipped:
        return result.skipped.value
    return f"extraction error: {result.error}"


def propose(
    pr_id: str,
    audit: AuditLog,
    *,
    today: date,
    source: Source = "recorded",
    client: Any | None = None,
    model: str | None = None,
    paths: Paths = DEFAULT_PATHS,
    cfg: CustomerConfig | None = None,
) -> Proposal:
    run_id = new_run_id()
    cfg = cfg or load_config(paths.config)
    pr = load_pr(pr_id, paths)
    log_event("run_started", run_id=run_id, pr_id=pr_id, source=source)

    with log_step("ingest", run_id) as extra:
        docs = load_inbox(paths.inbox)
        extra["documents"] = len(docs)
        extra["duplicates"] = sum(d.duplicate_of is not None for d in docs)

    with log_step("extract", run_id, source=source) as extra:
        results = [_extract(doc, pr, source, paths, client, model) for doc in docs]
        usage = Usage(
            input_tokens=sum(r.usage.input_tokens for r in results),
            output_tokens=sum(r.usage.output_tokens for r in results),
        )
        cost = estimate_cost_usd(model, usage) if source == "live" and model else None
        extra.update(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            est_cost_usd=cost,
            quotes=sum(r.quote is not None for r in results),
        )

    quotes = [r.quote for r in results if r.quote is not None]
    doc_of = {r.quote.quote_id: r.doc_name for r in results if r.quote is not None}
    with log_step("decide", run_id) as extra:
        rationale_client = client if source == "live" else None  # recorded runs use the template
        decision, candidates = decide(quotes, pr, cfg, today, rationale_client, model)
        extra.update(
            recommended=decision.recommended_quote_id, needs_approval=decision.needs_approval
        )

    draft_po, key, blocked = None, None, None
    winner = next(
        (c for c in candidates if c.quote.quote_id == decision.recommended_quote_id), None
    )
    if winner is None:
        blocked = "no recommendation: " + "; ".join(
            v for v in decision.policy_violations if v.startswith("only ")
        )
    else:
        try:
            draft_po, key = build_draft_po(pr, winner, cfg)
        except MissingSupplierError as exc:
            blocked = str(exc)

    proposal = Proposal(
        run_id=run_id,
        pr=pr,
        decision=decision,
        quotes=[
            QuoteView(
                doc_name=doc_of[c.quote.quote_id],
                quote=c.quote,
                total_net_eur=c.total_eur,
                score=c.score,
                exclusions=c.exclusions,
            )
            for c in sorted(candidates, key=lambda c: (-c.score, c.quote.quote_id))
        ],
        skipped=[
            SkippedDoc(doc_name=r.doc_name, reason=_skip_reason(r))
            for r in results
            if r.quote is None
        ],
        draft_po=draft_po,
        idempotency_key=key,
        po_blocked_reason=blocked,
        required_approvers=2 if decision.needs_approval and winner is not None else 1,
        usage=usage,
        est_cost_usd=cost,
    )
    audit.record_decision(run_id, pr_id, decision)
    audit.record(run_id, "proposal", "system", proposal.model_dump(mode="json"), pr_id)
    log_event("proposal_ready", run_id=run_id, pr_id=pr_id, blocked=blocked)
    return proposal


# --- Run state, derived from the audit log --------------------------------------------------

Status = Literal["awaiting_approval", "erp_unconfirmed", "erp_error", "po_created", "rejected"]


@dataclass
class RunState:
    run_id: str
    proposal: Proposal
    events: list[AuditEvent]
    approvers: list[str] = field(default_factory=list)
    status: Status = "awaiting_approval"
    po_number: str | None = None

    @property
    def is_open(self) -> bool:
        return self.status not in ("po_created", "rejected")


def run_state(events: list[AuditEvent]) -> RunState:
    """Replay a run's events in order to get its current state."""
    proposal_event = next(e for e in events if e.step == "proposal")
    state = RunState(
        run_id=proposal_event.run_id,
        proposal=Proposal.model_validate(proposal_event.payload),
        events=events,
    )
    for e in events:
        if e.step == "approval" and e.payload["approved"]:
            if e.actor not in state.approvers:
                state.approvers.append(e.actor)
        elif e.step == "approval":
            state.status = "rejected"
        elif e.step == "po_failed":
            state.status = "erp_unconfirmed" if e.payload["retryable"] else "erp_error"
        elif e.step == "po_created":
            state.status = "po_created"
            state.po_number = e.payload["po_number"]
    return state


def load_run(audit: AuditLog, run_id: str) -> RunState:
    events = audit.events(run_id)
    if not any(e.step == "proposal" for e in events):
        raise KeyError(f"unknown run {run_id}")
    return run_state(events)


def open_runs(audit: AuditLog) -> list[RunState]:
    """The approval queue: every run with a proposal that is not closed, oldest first."""
    by_run: dict[str, list[AuditEvent]] = {}
    for e in audit.events():
        by_run.setdefault(e.run_id, []).append(e)
    states = [run_state(evs) for evs in by_run.values() if any(e.step == "proposal" for e in evs)]
    return [s for s in states if s.is_open]


# --- Phase 2: approve and write ------------------------------------------------------------


class ApprovalError(ValueError):
    """The approval cannot be accepted (closed run, nothing to approve, same approver twice)."""


def write_po(state: RunState, audit: AuditLog, erp: ErpClient) -> RunState:
    p = state.proposal
    assert p.draft_po is not None and p.idempotency_key is not None
    with log_step("erp_write", p.run_id, idempotency_key=p.idempotency_key[:12]) as extra:
        try:
            po = erp.create_po(p.draft_po, p.idempotency_key)
        except ErpError as exc:
            retryable = isinstance(exc, RetryableError)
            extra.update(attempts=erp.attempts_last_call, result="failed", retryable=retryable)
            audit.record(
                p.run_id,
                "po_failed",
                "system",
                {
                    "error": str(exc),
                    "attempts": erp.attempts_last_call,
                    "retryable": retryable,
                    "idempotency_key": p.idempotency_key,
                },
                p.pr.pr_id,
            )
        else:
            extra.update(
                attempts=erp.attempts_last_call, result="created", po_number=po["PurchaseOrder"]
            )
            audit.record_po_created(
                p.run_id,
                p.pr.pr_id,
                po["PurchaseOrder"],
                p.idempotency_key,
                erp.attempts_last_call,
            )
    return load_run(audit, p.run_id)


def approve(
    run_id: str,
    approver: str,
    audit: AuditLog,
    erp: ErpClient,
    *,
    approved: bool = True,
    comment: str = "",
) -> RunState:
    approver = approver.strip()
    if not approver:
        raise ApprovalError("approver name is required")
    state = load_run(audit, run_id)
    if not state.is_open:
        raise ApprovalError(f"run is closed ({state.status})")
    if state.status in ("erp_unconfirmed", "erp_error"):
        raise ApprovalError("already approved; use retry to send it to SAP again")
    if approved and state.proposal.draft_po is None:
        raise ApprovalError(f"nothing to approve: {state.proposal.po_blocked_reason}")
    if approved and approver in state.approvers:
        raise ApprovalError(f"{approver} already approved; a second approver must be someone else")

    audit.record_approval(run_id, state.proposal.pr.pr_id, approver, approved, comment)
    state = load_run(audit, run_id)
    if approved and len(state.approvers) >= state.proposal.required_approvers:
        state = write_po(state, audit, erp)
    return state


def retry_po(run_id: str, actor: str, audit: AuditLog, erp: ErpClient) -> RunState:
    state = load_run(audit, run_id)
    if state.status != "erp_unconfirmed":
        raise ApprovalError(f"nothing to retry (status {state.status})")
    audit.record(run_id, "po_retry", actor.strip() or "unknown", {}, state.proposal.pr.pr_id)
    return write_po(state, audit, erp)


# --- Wiring for CLI and UI ------------------------------------------------------------------


def default_audit() -> AuditLog:
    path = Path(os.environ.get("AUDIT_DB", ROOT / "var" / "audit.db"))
    path.parent.mkdir(parents=True, exist_ok=True)
    return AuditLog(path)


def default_erp() -> ErpClient:
    url = os.environ.get("ERP_URL", "http://localhost:8001")
    return ErpClient(httpx.Client(base_url=url, timeout=10.0))


def default_today() -> date:
    """PROCURE_TODAY=2026-11-08 pins the date (e.g. to replay a demo after quotes expired)."""
    value = os.environ.get("PROCURE_TODAY")
    return date.fromisoformat(value) if value else date.today()


def live_client() -> tuple[Any, str]:
    import anthropic

    return anthropic.Anthropic(), os.environ["ANTHROPIC_MODEL"]


def wait_for_erp(erp: ErpClient, timeout_s: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            if erp.http.get("/health").status_code == 200:
                return
        except httpx.TransportError:
            pass
        if time.monotonic() > deadline:
            raise RuntimeError("mock ERP is not reachable; start it with `make erp`")
        time.sleep(0.3)


# --- Human-readable output ------------------------------------------------------------------


def _money(value: Decimal | None) -> str:
    return "–" if value is None else f"{value:,.2f}"


def format_proposal(p: Proposal) -> str:
    lines = [
        f"Run {p.run_id} · {p.pr.pr_id}: {p.pr.quantity} {p.pr.unit} {p.pr.description}, "
        f"need by {p.pr.need_by}",
        "",
        f"{'Quote':<11} {'Supplier':<32} {'Net EUR':>10} {'Delivery':<10} {'Score':>6}  Status",
    ]
    for q in p.quotes:
        status = "VALID" if not q.exclusions else "excluded: " + "; ".join(q.exclusions)
        supplier, delivery = q.quote.supplier_name[:32], str(q.quote.delivery_date.value or "–")
        lines.append(
            f"{q.quote.quote_id:<11} {supplier:<32} {_money(q.total_net_eur):>10} "
            f"{delivery:<10} {q.score:>6.3f}  {status}"
        )
    for s in p.skipped:
        lines.append(f"{'–':<11} {s.doc_name:<32} {'':>10} {'':<10} {'':>6}  skipped: {s.reason}")
    lines += ["", f"Rationale: {p.decision.rationale}"]
    if p.draft_po is None:
        lines.append(f"No PO can be created: {p.po_blocked_reason}")
    else:
        item = p.draft_po.items[0]
        lines.append(
            f"Draft PO: supplier {p.draft_po.Supplier}, {item.OrderQuantity} x {item.Material} "
            f"at {item.NetPriceAmount} {p.draft_po.DocumentCurrency} net. "
            f"Approvers needed: {p.required_approvers}."
        )
    return "\n".join(lines)


def format_trail(events: list[AuditEvent]) -> str:
    lines = ["Audit trail:"]
    for e in events:
        detail = {
            "decision": lambda p: f"recommended {p['recommended_quote_id']}",
            "proposal": lambda p: "proposal shown to approver",
            "approval": lambda p: (
                ("approved" if p["approved"] else "REJECTED")
                + (f" ({p['comment']})" if p["comment"] else "")
            ),
            "po_created": lambda p: f"PO {p['po_number']} after {p['attempts']} attempt(s)",
            "po_failed": lambda p: f"SAP did not confirm after {p['attempts']} attempt(s)",
            "po_retry": lambda p: "retry requested",
        }.get(e.step, lambda p: "")(e.payload)
        lines.append(f"  {e.ts[:19]}  {e.step:<11} {e.actor:<14} {detail}")
    return "\n".join(lines)


def format_outcome(state: RunState) -> str:
    match state.status:
        case "po_created":
            return f"PO {state.po_number} created in SAP."
        case "erp_unconfirmed":
            return (
                "SAP did not confirm. It may or may not have created the PO; retrying is safe "
                f"(same idempotency key): python -m procure.pipeline retry {state.run_id} "
                '--actor "<your name>"'
            )
        case "erp_error":
            return "SAP rejected the PO. A human needs to look at it (see audit trail)."
        case "rejected":
            return "Rejected. No PO was created."
        case _:
            need = state.proposal.required_approvers - len(state.approvers)
            return f"Waiting for {need} more approver(s)."


# --- CLI ------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="procure.pipeline", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("propose", "demo"):
        p = sub.add_parser(name)
        p.add_argument("pr_id")
        p.add_argument("--source", choices=["recorded", "live"], default="recorded")
        if name == "demo":
            p.add_argument("--approver", default="Anna Leitner")
            p.add_argument("--yes", action="store_true", help="approve without asking")
    sub.add_parser("queue")
    p = sub.add_parser("approve")
    p.add_argument("run_id")
    p.add_argument("--approver", required=True)
    p.add_argument("--reject", action="store_true")
    p.add_argument("--comment", default="")
    p = sub.add_parser("retry")
    p.add_argument("run_id")
    p.add_argument("--actor", required=True)
    args = parser.parse_args(argv)

    audit = default_audit()
    if args.command in ("propose", "demo"):
        client, model = live_client() if args.source == "live" else (None, None)
        proposal = propose(
            args.pr_id, audit, today=default_today(), source=args.source, client=client, model=model
        )
        print(format_proposal(proposal))
        if args.command == "propose" or proposal.draft_po is None:
            return 0
        erp = default_erp()
        wait_for_erp(erp)
        state = load_run(audit, proposal.run_id)
        while state.is_open and state.status == "awaiting_approval":
            total = proposal.winner.total_net_eur if proposal.winner else None
            question = (
                f"\n{args.approver}: approve PO to supplier {proposal.draft_po.Supplier} "
                f"for {_money(total)} EUR net? [y/N] "
            )
            yes = args.yes or input(question).strip().lower() == "y"
            state = approve(proposal.run_id, args.approver, audit, erp, approved=yes)
            if state.status == "awaiting_approval":  # a second, different approver is needed
                args.approver = "Head of Purchasing"
        print("\n" + format_outcome(state) + "\n")
        print(format_trail(state.events))
        return 0

    if args.command == "queue":
        for s in open_runs(audit):
            print(f"{s.run_id}  {s.proposal.pr.pr_id}  {s.status}  approvers: {s.approvers}")
        return 0

    erp = default_erp()
    if args.command == "approve":
        state = approve(
            args.run_id, args.approver, audit, erp, approved=not args.reject, comment=args.comment
        )
    else:
        state = retry_po(args.run_id, args.actor, audit, erp)
    print(format_outcome(state))
    return 0


if __name__ == "__main__":
    sys.exit(main())
