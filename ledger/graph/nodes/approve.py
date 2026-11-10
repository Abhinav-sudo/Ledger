from datetime import UTC, datetime

from langgraph.graph import END
from langgraph.types import interrupt

from ledger.contracts.decisions import Approval, AuditEvent
from ledger.contracts.state import InvoiceState
from ledger.graph.nodes.policy import PAYS, SETTLED


def run(state: InvoiceState) -> InvoiceState:
    # This node re-runs from the top on resume, so nothing may happen before interrupt().
    decision = interrupt(
        {
            "invoice_id": state["invoice_id"],
            "proposal": state["proposal"].model_dump(mode="json"),
            "analyses": [a.model_dump(mode="json") for a in state.get("analyses", [])],
        }
    )
    approval = Approval(at=datetime.now(UTC), **decision)
    approved = approval.decision == "approve"
    return {
        "approval": approval,
        "status": SETTLED[state["proposal"].action] if approved else "rejected",
        "audit": [
            AuditEvent(
                actor=approval.by,
                event=f"approval.{approval.decision}",
                detail={"note": approval.note},
            )
        ],
    }


def next_step(state: InvoiceState) -> str:
    approved = state["approval"].decision == "approve"
    return "payment_intent" if approved and state["proposal"].action in PAYS else END
