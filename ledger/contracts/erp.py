from datetime import date

from pydantic import BaseModel

from ledger.contracts.money import Money


class POLine(BaseModel):
    line_no: int
    sku: str
    description: str
    qty: Money
    unit_price: Money


class PurchaseOrder(BaseModel):
    po_number: str
    vendor_id: str
    status: str
    lines: list[POLine]


class BankAccount(BaseModel):
    account: str
    valid_from: date
    valid_to: date | None


class PriorInvoice(BaseModel):
    id: str
    invoice_no: str
    invoice_date: date
    total: Money
    po_number: str | None
