"""Ground-truth stand-ins for the models, so the crash suite measures payments, not extraction."""

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from ledger.contracts.invoice import ExtractedInvoice, LineItem
from ledger.llm.router import Reply
from world.generator import Invoice, Vendor, World


def extracted(inv: Invoice, vendor: Vendor) -> ExtractedInvoice:
    """What a perfect extractor would read off the PDF."""
    return ExtractedInvoice(
        vendor_name=vendor.short_name if inv.short_vendor_name else vendor.name,
        invoice_no=inv.invoice_no,
        invoice_date=inv.invoice_date,
        po_number=inv.po_number,
        currency=inv.currency,
        lines=[
            LineItem(sku=ln.sku, description=ln.description, qty=ln.qty, unit_price=ln.unit_price)
            for ln in inv.lines
        ],
        total=inv.total,
        remit_to_account=inv.remit_to_account,
        field_confidence={"all": 1.0},
    )


class TruthRouter:
    roles = {"extraction": ["truth/fields"]}

    def __init__(self, fields: ExtractedInvoice | None) -> None:
        self.fields = fields

    def complete[T: BaseModel](
        self,
        role: str,
        messages: list[dict[str, Any]],
        schema: type[T],
        tools: list[Callable[..., Any]] | None = None,
        start_at: int = 0,
    ) -> Reply[T]:
        if role == "extraction":
            return Reply(self.fields, "truth/fields", 0)
        return Reply(schema(finding="ground truth run", evidence=[], confidence=1), "truth/a", 0)


def router_for(world: World) -> Callable[[str], TruthRouter]:
    invoices = {inv.id: inv for inv in world.invoices}
    vendors = {v.id: v for v in world.vendors}

    def make(invoice_id: str) -> TruthRouter:
        inv = invoices.get(invoice_id)
        return TruthRouter(extracted(inv, vendors[inv.vendor_id]) if inv else None)

    return make
