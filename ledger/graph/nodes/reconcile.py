from langgraph.types import interrupt

from ledger.contracts.decisions import AuditEvent
from ledger.contracts.state import InvoiceState


def run(state: InvoiceState) -> InvoiceState:
    # Re-runs from the top when the reconciler resumes it, so nothing may happen before this.
    confirmation = interrupt({"invoice_id": state["invoice_id"], "intent": str(state["intent"].id)})
    return {
        "status": "paid",
        "audit": [AuditEvent(actor="reconciler", event="payment.confirmed", detail=confirmation)],
    }
