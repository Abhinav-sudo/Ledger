from ledger.contracts.decisions import AuditEvent
from ledger.contracts.payment import PaymentIntent
from ledger.contracts.state import InvoiceState
from ledger.side_effects import idempotency, outbox
from ledger.tools.matcher import normalise_account


def run(state: InvoiceState) -> InvoiceState:
    """Returns the intent; OutboxSaver commits it with this node's state write. No bank call."""
    amount = state["proposal"].amount
    account = normalise_account(state["extracted"].remit_to_account)
    key = idempotency.key(state["invoice_id"], amount, account)
    existing = outbox.find(state["invoice_id"])
    if existing and existing.idempotency_key != key:
        return {
            "status": "needs_human",
            "audit": [
                AuditEvent(
                    actor="payment_intent",
                    event="payment_intent.conflict",
                    detail={"existing": str(existing.id), "amount": str(amount)},
                )
            ],
        }
    intent = PaymentIntent(
        id=idempotency.intent_id(key),
        invoice_id=state["invoice_id"],
        amount=amount,
        vendor_account=account,
        idempotency_key=key,
    )
    return {
        "intent": intent,
        "status": "payment_pending",
        "audit": [
            AuditEvent(
                actor="payment_intent",
                event="payment_intent.recorded",
                detail={"intent": str(intent.id), "amount": str(amount), "account": account},
            )
        ],
    }
