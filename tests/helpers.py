from collections import defaultdict
from decimal import Decimal
from pathlib import Path

import psycopg
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from ledger.contracts.erp import BankAccount, POLine, PriorInvoice, PurchaseOrder
from ledger.contracts.invoice import ExtractedInvoice, LineItem
from ledger.contracts.payment import PaymentIntent
from ledger.llm.router import Reply
from ledger.side_effects import idempotency, outbox
from world.generator import Invoice, Vendor, World
from world.mock_bank.app import create_app as create_bank


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


class FakeRouter:
    """Stands in for ModelRouter: replays a scripted extraction, gives canned analyses."""

    def __init__(self, *extractions: ExtractedInvoice) -> None:
        self.roles = {"extraction": ["fake/weak", "fake/mid", "fake/strong"]}
        self.extractions = list(extractions)
        self.calls: list[str] = []

    def complete(self, role, messages, schema, tools=None, start_at=0):
        self.calls.append(role)
        if role == "extraction":
            value = self.extractions[min(start_at, len(self.extractions) - 1)]
            return Reply(value, self.roles["extraction"][start_at], start_at)
        return Reply(schema(finding="looks as matched", evidence=[], confidence=0.5), "fake/a", 0)


ACCOUNT = "GB80LDGR07117181324261"


def add_intent(con: psycopg.Connection, vendor_id: str, invoice_id: str, amount: str) -> str:
    """An invoice row and its pending payment intent; returns the idempotency key."""
    con.execute(
        "INSERT INTO invoices (id, vendor_id, invoice_no, invoice_date, currency, "
        "remit_to_account, total) VALUES (%s, %s, %s, '2026-01-05', 'USD', %s, %s) "
        "ON CONFLICT DO NOTHING",
        (invoice_id, vendor_id, invoice_id, ACCOUNT, amount),
    )
    key = idempotency.key(invoice_id, Decimal(amount), ACCOUNT)
    intent = PaymentIntent(
        id=idempotency.intent_id(key),
        invoice_id=invoice_id,
        amount=Decimal(amount),
        vendor_account=ACCOUNT,
        idempotency_key=key,
    )
    with con.cursor(row_factory=dict_row) as cur:
        outbox.record(cur, intent)
    return key


def bank(db_path: Path, **rates: float) -> TestClient:
    return TestClient(create_bank(db_path, seed=0, **rates))
