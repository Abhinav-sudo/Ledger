import psycopg

from ledger.contracts.decisions import AuditEvent
from ledger.contracts.invoice import ExtractedInvoice
from ledger.contracts.state import InvoiceState
from ledger.settings import PRICE_TOLERANCE, QTY_TOLERANCE, database_url
from ledger.tools import erp_queries, matcher

INSERT_INVOICE = """
INSERT INTO invoices (id, vendor_id, invoice_no, invoice_date, po_number, currency,
                      remit_to_account, total, pdf_path)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT DO NOTHING
RETURNING id
"""
INSERT_LINE = """
INSERT INTO invoice_lines (invoice_id, line_no, sku, description, qty, unit_price)
VALUES (%s, %s, %s, %s, %s, %s)
"""


def record(invoice_id: str, vendor_id: str, inv: ExtractedInvoice, pdf_path: str) -> None:
    """Idempotent: a replay, or a resubmitted invoice number, inserts nothing."""
    with psycopg.connect(database_url()) as con:
        inserted = con.execute(
            INSERT_INVOICE,
            (
                invoice_id,
                vendor_id,
                inv.invoice_no,
                inv.invoice_date,
                inv.po_number,
                inv.currency,
                matcher.normalise_account(inv.remit_to_account),
                inv.total,
                pdf_path,
            ),
        ).fetchone()
        if inserted:
            con.cursor().executemany(
                INSERT_LINE,
                [
                    (invoice_id, n, ln.sku, ln.description, ln.qty, ln.unit_price)
                    for n, ln in enumerate(inv.lines, start=1)
                ],
            )


def run(state: InvoiceState) -> InvoiceState:
    inv = state["extracted"]
    vendor = erp_queries.find_vendor(inv.vendor_name)
    if vendor is None:
        return {
            "status": "needs_human",
            "audit": [
                AuditEvent(actor="match", event="unknown_vendor", detail={"name": inv.vendor_name})
            ],
        }
    po = erp_queries.get_purchase_order(inv.po_number) if inv.po_number else None
    result = matcher.match(
        inv,
        state["invoice_id"],
        vendor.id,
        po,
        erp_queries.get_receipts(po.po_number) if po else {},
        erp_queries.get_vendor_accounts(vendor.id),
        erp_queries.get_vendor_invoices(vendor.id),
        state.get("email"),
        PRICE_TOLERANCE,
        QTY_TOLERANCE,
    )
    record(state["invoice_id"], vendor.id, inv, state["pdf_path"])
    return {
        "match": result,
        "status": "matched",
        "audit": [
            AuditEvent(actor="match", event="matched", detail={"exceptions": result.exceptions})
        ],
    }
