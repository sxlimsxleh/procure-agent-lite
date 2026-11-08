"""Approval queue (Streamlit). Run with: make ui   (needs the mock ERP: make erp)

The buyer sees the recommendation, every quote with the exact source text behind each number,
the draft PO as it will be sent to SAP, and approves or rejects. Nothing is written to SAP
without an approval, and above the threshold not without two different approvers.
"""

from dataclasses import dataclass
from typing import Any

import streamlit as st

from procure.audit import AuditLog
from procure.erp_client import ErpClient
from procure.pipeline import (
    DEFAULT_PATHS,
    ApprovalError,
    Paths,
    Proposal,
    RunState,
    approve,
    default_audit,
    default_erp,
    default_today,
    format_outcome,
    load_run,
    open_runs,
    propose,
    retry_po,
)

FIELDS = ["unit_price", "quantity", "total", "delivery_date", "valid_until", "payment_terms_days"]


@dataclass
class Services:
    audit: AuditLog
    erp: ErpClient
    paths: Paths = DEFAULT_PATHS


def services() -> Services:
    """Tests put their own Services into session_state; the real app builds them from env."""
    if "services" not in st.session_state:
        st.session_state["services"] = Services(audit=default_audit(), erp=default_erp())
    return st.session_state["services"]


def money(value: Any) -> str:
    return "–" if value is None else f"{value:,.2f}"


# --- Sections ---------------------------------------------------------------------------------


def show_recommendation(p: Proposal) -> None:
    pr = p.pr
    st.subheader(f"{pr.pr_id}: {pr.quantity} {pr.unit} × {pr.description}")
    st.caption(f"Requested by {pr.requester} · need by {pr.need_by} · run `{p.run_id}`")
    winner = p.winner
    if winner is not None and p.draft_po is not None:
        st.success(
            f"Recommended: **{winner.quote.supplier_name}** ({winner.quote.quote_id}), "
            f"{money(winner.total_net_eur)} EUR net, delivery {winner.quote.delivery_date.value}"
        )
    else:
        st.error(f"No PO can be created: {p.po_blocked_reason}")
    if p.required_approvers > 1:
        st.warning("Above the approval threshold: two different approvers are required.")
    st.markdown(f"**Why:** {p.decision.rationale}")


def show_quotes(p: Proposal) -> None:
    st.markdown("#### All quotes")
    rows = [
        {
            "Quote": q.quote.quote_id,
            "Supplier": q.quote.supplier_name,
            "Document": q.doc_name,
            "Net EUR": money(q.total_net_eur),
            "Delivery": str(q.quote.delivery_date.value or "–"),
            "Payment days": q.quote.payment_terms_days.value,
            "Score": round(q.score, 3),
            "Status": "valid" if not q.exclusions else "excluded: " + "; ".join(q.exclusions),
        }
        for q in p.quotes
    ]
    st.dataframe(rows, hide_index=True, use_container_width=True)
    for s in p.skipped:
        st.caption(f"Skipped {s.doc_name}: {s.reason}")

    st.markdown("#### Where every number comes from")
    for q in p.quotes:
        with st.expander(f"{q.quote.quote_id} · {q.quote.supplier_name} · {q.doc_name}"):
            if q.quote.flags:
                st.warning("Flags: " + ", ".join(f.value for f in q.quote.flags))
            st.table(
                [
                    {
                        "Field": name,
                        "Value": str(getattr(q.quote, name).value or "–"),
                        "Status": getattr(q.quote, name).status,
                        "Source text in document": getattr(q.quote, name).source_text or "–",
                        "Reason": getattr(q.quote, name).reason or "",
                    }
                    for name in FIELDS
                ]
            )


def show_actions(state: RunState, svc: Services) -> None:
    p = state.proposal
    st.markdown("#### Draft PO for SAP")
    if p.draft_po is not None:
        st.json(p.draft_po.model_dump(mode="json", by_alias=True))
        st.caption(f"Idempotency-Key: `{p.idempotency_key}`")

    st.markdown("#### Decision")
    st.info(format_outcome(state))
    if state.approvers:
        st.caption("Approved so far by: " + ", ".join(state.approvers))

    if not state.is_open:
        return  # PO created or rejected: nothing left to do here

    name = st.text_input("Your name", key="approver")
    comment = st.text_input("Comment (optional)", key="comment")
    col_approve, col_reject, col_retry = st.columns(3)
    try:
        if state.status == "erp_unconfirmed":
            if col_retry.button("Retry sending to SAP", key="retry", type="primary"):
                new = retry_po(state.run_id, name or "unknown", svc.audit, svc.erp)
                st.session_state["flash"] = format_outcome(new)
                st.rerun()
        else:
            if col_approve.button(
                "Approve", key="approve", type="primary", disabled=p.draft_po is None
            ):
                new = approve(state.run_id, name, svc.audit, svc.erp, comment=comment)
                st.session_state["flash"] = format_outcome(new)
                st.rerun()
            if col_reject.button("Reject", key="reject"):
                new = approve(
                    state.run_id, name, svc.audit, svc.erp, approved=False, comment=comment
                )
                st.session_state["flash"] = format_outcome(new)
                st.rerun()
    except ApprovalError as exc:
        st.error(str(exc))


def show_trail(state: RunState) -> None:
    st.markdown("#### Audit trail")
    detail_keys = ("approved", "comment", "po_number", "attempts", "error")
    rows = []
    for e in state.events:
        details = ", ".join(f"{k}: {e.payload[k]}" for k in detail_keys if e.payload.get(k))
        rows.append({"Time (UTC)": e.ts[:19], "Step": e.step, "Actor": e.actor, "Details": details})
    st.dataframe(rows, hide_index=True, use_container_width=True)


# --- Page -------------------------------------------------------------------------------------


def main() -> None:
    st.set_page_config(page_title="Approval queue · Brunner", layout="wide")
    st.title("Purchase order approvals")
    svc = services()

    with st.sidebar:
        st.markdown("**New proposal**")
        pr_ids = sorted(p.stem for p in svc.paths.prs.glob("PR-*.json"))
        pr_id = st.selectbox("Purchase request", pr_ids, key="pr_id")
        if st.button("Process inbox", key="propose"):
            proposal = propose(pr_id, svc.audit, today=default_today(), paths=svc.paths)
            st.session_state["selected"] = proposal.run_id
            st.rerun()
        st.caption("Uses recorded extractions, so no API key is needed.")

    if "flash" in st.session_state:
        st.success(st.session_state.pop("flash"))

    queue = open_runs(svc.audit)
    selected = st.session_state.get("selected")
    if not queue and selected is None:
        st.info("No purchase requests waiting for approval. Use **Process inbox** on the left.")
        return
    ids = [s.run_id for s in queue]
    if selected and selected not in ids:
        ids.append(selected)  # just closed: keep showing it so the outcome is visible
    choice = st.radio(
        "Open runs",
        ids,
        index=ids.index(selected) if selected in ids else 0,
        format_func=lambda rid: f"{rid} ({load_run(svc.audit, rid).status})",
        horizontal=True,
        key="run_choice",
    )
    st.session_state["selected"] = choice
    state = load_run(svc.audit, choice)
    show_recommendation(state.proposal)
    show_quotes(state.proposal)
    show_actions(state, svc)
    show_trail(state)


main()
