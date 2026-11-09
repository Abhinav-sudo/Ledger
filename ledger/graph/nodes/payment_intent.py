import uuid
from decimal import Decimal

import httpx

from ledger import chaos
from ledger.contracts.decisions import AuditEvent
from ledger.contracts.payment import PaymentIntent
from ledger.contracts.state import InvoiceState
from ledger.settings import bank_url, outbox_mode
from ledger.side_effects import idempotency, outbox
from ledger.tools.matcher import normalise_account


def _bank() -> httpx.Client:
    return httpx.Client(base_url=bank_url(), timeout=10)


def _pay_inline(state: InvoiceState, amount: Decimal, account: str, key: str) -> InvoiceState:
    """Ablation only: what this node would do without the outbox. Never the default."""
    with _bank() as client:
        resp = client.post(
            "/payments",
            json={"invoice_id": state["invoice_id"], "amount": f"{amount:.2f}", "account": account},
            headers={"Idempotency-Key": key},
        )
    resp.raise_for_status()
    chaos.point("inline.after_bank_call")
    return {
        "status": "paid",
        "audit": [
            AuditEvent(
                actor="payment_intent",
                event="payment.inline",
                detail={"payment_id": resp.json()["payment_id"], "amount": str(amount)},
            )
        ],
    }


def run(state: InvoiceState) -> InvoiceState:
    """Returns the intent; OutboxSaver commits it with this node's state write. No bank call."""
    amount = state["proposal"].amount
    account = normalise_account(state["extracted"].remit_to_account)
    key = idempotency.key(state["invoice_id"], amount, account)
    mode = outbox_mode()
    if mode != "on":
        # off sends a fresh request id per attempt, as inline code without a durable key does.
        return _pay_inline(state, amount, account, key if mode == "off-keyed" else uuid.uuid4().hex)
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
