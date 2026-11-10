from decimal import Decimal

from langgraph.graph import END

from ledger.contracts.decisions import Analysis, AuditEvent, Proposal
from ledger.contracts.invoice import ExtractedInvoice, MatchResult
from ledger.contracts.money import CENT
from ledger.contracts.state import InvoiceState

PAYS = {"pay", "pay_partial"}
SETTLED = {
    "pay": "payable",
    "pay_partial": "payable",
    "hold": "held",
    "reject": "rejected",
    "request_credit_note": "credit_note_requested",
}


def _partial(inv: ExtractedInvoice, m: MatchResult) -> Decimal:
    received = {d.line_no: d.expected for d in m.deltas if d.kind == "qty"}
    return sum(
        (
            (min(ln.qty, received.get(n, ln.qty)) * ln.unit_price).quantize(CENT)
            for n, ln in enumerate(inv.lines, start=1)
        ),
        Decimal("0.00"),
    )


def decide(inv: ExtractedInvoice, m: MatchResult, analyses: list[Analysis]) -> Proposal:
    """The approval matrix. Amounts are selected from validated fields, never generated."""
    ex = set(m.exceptions)
    zero = Decimal("0.00")
    beyond = any(d.kind == "price" and not d.within_tolerance for d in m.deltas)
    if "duplicate" in ex:
        action, amount = "reject", zero
    elif "missing_po" in ex or beyond:
        action, amount = "hold", zero
    elif "credit_note" in ex:
        action, amount = (
            "request_credit_note",
            sum((-ln.amount for ln in inv.lines if ln.qty < 0), zero),
        )
    elif "qty_variance" in ex:
        action, amount = "pay_partial", _partial(inv, m)
    else:
        action, amount = "pay", inv.total
    bank_change = "bank_change" in ex
    rationale = "; ".join(m.reasons) or "invoice matches PO and receipts"
    if analyses:
        rationale += "\n" + "\n".join(f"{a.agent}: {a.finding}" for a in analyses)
    return Proposal(
        action=action,
        amount=amount,
        rationale=rationale,
        requires_approval=bank_change,
        approver_role="treasury" if bank_change else None,
    )


def run(state: InvoiceState) -> InvoiceState:
    proposal = decide(state["extracted"], state["match"], state.get("analyses", []))
    status = "awaiting_approval" if proposal.requires_approval else SETTLED[proposal.action]
    return {
        "proposal": proposal,
        "status": status,
        "audit": [
            AuditEvent(
                actor="policy",
                event="proposed",
                detail={"action": proposal.action, "amount": str(proposal.amount)},
            )
        ],
    }


def next_step(state: InvoiceState) -> str:
    proposal = state["proposal"]
    if proposal.requires_approval:
        return "approve"
    return "payment_intent" if proposal.action in PAYS else END
