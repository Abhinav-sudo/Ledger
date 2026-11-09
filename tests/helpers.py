from collections import defaultdict
from decimal import Decimal

from ledger.contracts.erp import BankAccount, POLine, PriorInvoice, PurchaseOrder
from ledger.contracts.invoice import ExtractedInvoice, LineItem
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


class Erp:
    """The ERP view of a generated world, without a database."""

    def __init__(self, world: World) -> None:
        self.vendors = {v.id: v for v in world.vendors}
        self.pos = {
            p.po_number: PurchaseOrder(
                po_number=p.po_number,
                vendor_id=p.vendor_id,
                status=p.status,
                lines=[POLine(**vars(ln)) for ln in p.lines],
            )
            for p in world.purchase_orders
        }
        self.received: dict[str, dict[int, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
        for r in world.receipts:
            self.received[r.po_number][r.line_no] += r.qty_received
        self.accounts = {v.id: [BankAccount(**vars(a)) for a in v.accounts] for v in world.vendors}
        self.emails = {e.invoice_id: e.body for e in world.emails}

    @staticmethod
    def prior(inv: Invoice) -> PriorInvoice:
        return PriorInvoice(
            id=inv.id,
            invoice_no=inv.invoice_no,
            invoice_date=inv.invoice_date,
            total=inv.total,
            po_number=inv.po_number,
        )
