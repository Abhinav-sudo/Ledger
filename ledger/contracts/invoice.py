from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from ledger.contracts.money import CENT, Money

ExceptionType = Literal[
    "price_variance", "qty_variance", "duplicate", "missing_po", "bank_change", "credit_note"
]


class LineItem(BaseModel):
    sku: str
    description: str
    qty: Money
    unit_price: Money = Field(ge=0)

    @property
    def amount(self) -> Decimal:
        return (self.qty * self.unit_price).quantize(CENT)


class ExtractedInvoice(BaseModel):
    vendor_name: str
    invoice_no: str
    invoice_date: date
    po_number: str | None
    currency: str = Field(min_length=3, max_length=3)
    lines: list[LineItem] = Field(min_length=1)
    total: Money
    remit_to_account: str
    field_confidence: dict[str, float]

    @property
    def lines_total(self) -> Decimal:
        return sum((line.amount for line in self.lines), Decimal("0.00"))


class LineDelta(BaseModel):
    line_no: int
    sku: str
    kind: Literal["price", "qty"]
    invoiced: Money
    expected: Money
    within_tolerance: bool


class MatchResult(BaseModel):
    vendor_id: str
    exceptions: list[ExceptionType]
    deltas: list[LineDelta] = []
    evidence: list[str] = []
    reasons: list[str] = []
