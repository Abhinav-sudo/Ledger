import hashlib
import uuid
from decimal import Decimal

from ledger.contracts.money import CENT
from ledger.tools.matcher import normalise_account

NAMESPACE = uuid.UUID("6f1c2d4e-9a7b-4c3d-8e5f-0a1b2c3d4e5f")


def key(invoice_id: str, amount: Decimal, account: str) -> str:
    """sha256(invoice_id | amount | account); 100 and 100.00 give the same key."""
    cents = amount.quantize(CENT)
    if cents != amount:
        raise ValueError(f"amount {amount} has sub-cent precision")
    return hashlib.sha256(
        f"{invoice_id}|{cents:f}|{normalise_account(account)}".encode()
    ).hexdigest()


def intent_id(idempotency_key: str) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, idempotency_key)
