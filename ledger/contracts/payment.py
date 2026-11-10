from uuid import UUID

from pydantic import BaseModel, Field

from ledger.contracts.money import Money


class PaymentIntent(BaseModel):
    id: UUID
    invoice_id: str
    amount: Money = Field(gt=0)
    vendor_account: str
    idempotency_key: str
