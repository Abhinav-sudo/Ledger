"""Read-only ERP lookups. The only tools an analyst may be bound to.

Every connection is opened with default_transaction_read_only, so Postgres rejects a write
even if a query here were changed to attempt one.
"""

import difflib
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.rows import dict_row

from ledger.contracts.erp import BankAccount, POLine, PriorInvoice, PurchaseOrder, Vendor
from ledger.settings import database_url

READ_ONLY = "-c default_transaction_read_only=on"

VENDORS_SQL = "SELECT id, name, short_name FROM vendors ORDER BY id"
PO_SQL = "SELECT po_number, vendor_id, status FROM purchase_orders WHERE po_number = %s"
PO_LINES_SQL = (
    "SELECT line_no, sku, description, qty, unit_price FROM po_lines "
    "WHERE po_number = %s ORDER BY line_no"
)
RECEIPTS_SQL = (
    "SELECT line_no, sum(qty_received) AS qty FROM goods_receipts "
    "WHERE po_number = %s GROUP BY line_no ORDER BY line_no"
)
ACCOUNTS_SQL = (
    "SELECT account, valid_from, valid_to FROM vendor_bank_accounts "
    "WHERE vendor_id = %s ORDER BY valid_from"
)
VENDOR_INVOICES_SQL = (
    "SELECT id, invoice_no, invoice_date, total, po_number FROM invoices "
    "WHERE vendor_id = %s ORDER BY received_at"
)
INVOICE_SQL = "SELECT id, invoice_no, invoice_date, total, po_number FROM invoices WHERE id = %s"


def connect() -> psycopg.Connection[dict[str, Any]]:
    return psycopg.connect(database_url(), options=READ_ONLY, row_factory=dict_row)


def _rows(sql: str, *params: object) -> list[dict[str, Any]]:
    with connect() as con:
        return con.execute(sql, params).fetchall()


def find_vendor(name: str) -> Vendor | None:
    """Find the vendor whose full or abbreviated name best matches the name on an invoice."""
    vendors = [Vendor(**r) for r in _rows(VENDORS_SQL)]
    names = {n.casefold(): v for v in vendors for n in (v.name, v.short_name)}
    close = difflib.get_close_matches(name.casefold(), names, n=1, cutoff=0.8)
    return names[close[0]] if close else None


def get_purchase_order(po_number: str) -> PurchaseOrder | None:
    """Get a purchase order with its lines, or null if no such PO exists."""
    header = _rows(PO_SQL, po_number)
    if not header:
        return None
    lines = [POLine(**r) for r in _rows(PO_LINES_SQL, po_number)]
    return PurchaseOrder(**header[0], lines=lines)


def get_receipts(po_number: str) -> dict[int, Decimal]:
    """Get the total quantity received so far for each line of a purchase order."""
    return {r["line_no"]: r["qty"] for r in _rows(RECEIPTS_SQL, po_number)}


def get_vendor_accounts(vendor_id: str) -> list[BankAccount]:
    """Get a vendor's bank account history; the open account has no valid_to."""
    return [BankAccount(**r) for r in _rows(ACCOUNTS_SQL, vendor_id)]


def get_vendor_invoices(vendor_id: str) -> list[PriorInvoice]:
    """List every invoice already received from a vendor, oldest first."""
    return [PriorInvoice(**r) for r in _rows(VENDOR_INVOICES_SQL, vendor_id)]


def get_invoice(invoice_id: str) -> PriorInvoice | None:
    """Get one previously received invoice by its id."""
    rows = _rows(INVOICE_SQL, invoice_id)
    return PriorInvoice(**rows[0]) if rows else None
