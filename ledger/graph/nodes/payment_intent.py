from ledger.contracts.decisions import AuditEvent
from ledger.contracts.state import InvoiceState
from ledger.tools.matcher import normalise_account


def run(state: InvoiceState) -> InvoiceState:
    """Stub until the outbox lands: records that a payment would be created, writes nothing."""
    return {
        "status": "payment_pending",
        "audit": [
            AuditEvent(
                actor="payment_intent",
                event="payment_intent.called",
                detail={
                    "amount": str(state["proposal"].amount),
                    "account": normalise_account(state["extracted"].remit_to_account),
                },
            )
        ],
    }
