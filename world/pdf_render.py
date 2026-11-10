from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import Table, TableStyle

if TYPE_CHECKING:
    from world.generator import Invoice, Vendor

BILL_TO = ("Ledger Demo Corp", "Accounts Payable", "1 Example Way, Springfield")
WIDTH, HEIGHT = A4


def _money(amount: Decimal, eu: bool = False) -> str:
    s = f"{amount:,.2f}"
    if eu:
        s = s.replace(",", " ").replace(".", ",").replace(" ", ".")
    return s


def _rows(inv: Invoice, eu: bool = False) -> list[list[str]]:
    return [
        [ln.sku, ln.description, f"{ln.qty:g}", _money(ln.unit_price, eu), _money(ln.amount, eu)]
        for ln in inv.lines
    ]


def _table(c: Canvas, rows: list[list[str]], x: float, y: float, font_size: int = 9) -> float:
    table = Table(rows, colWidths=[24 * mm, 76 * mm, 16 * mm, 26 * mm, 28 * mm])
    table.setStyle(
        TableStyle(
            [
                ("FONT", (0, 0), (-1, -1), "Helvetica", font_size),
                ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", font_size),
                ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
                ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.black),
            ]
        )
    )
    _, h = table.wrapOn(c, WIDTH, HEIGHT)
    table.drawOn(c, x, y - h)
    return y - h


def _lines(
    c: Canvas, x: float, y: float, lines: list[str], size: int = 9, lead: float = 4.5
) -> None:
    c.setFont("Helvetica", size)
    for i, text in enumerate(lines):
        c.drawString(x, y - i * lead * mm, text)


def _classic(c: Canvas, inv: Invoice, vendor_name: str, issued: str) -> None:
    c.setFont("Helvetica-Bold", 16)
    c.drawString(20 * mm, HEIGHT - 25 * mm, vendor_name)
    c.setFont("Helvetica-Bold", 22)
    c.drawRightString(WIDTH - 20 * mm, HEIGHT - 25 * mm, "INVOICE")
    _lines(c, 20 * mm, HEIGHT - 45 * mm, ["Bill to:", *BILL_TO])
    meta = [f"Invoice no: {inv.invoice_no}", f"Date: {issued}"]
    if inv.po_number:
        meta.append(f"PO: {inv.po_number}")
    _lines(c, 125 * mm, HEIGHT - 45 * mm, meta)
    header = [["Item", "Description", "Qty", "Unit price", "Amount"]]
    bottom = _table(c, header + _rows(inv), 20 * mm, HEIGHT - 75 * mm)
    c.setFont("Helvetica-Bold", 12)
    c.drawRightString(
        WIDTH - 20 * mm, bottom - 12 * mm, f"Total {inv.currency} {_money(inv.total)}"
    )
    _lines(c, 20 * mm, 30 * mm, ["Please remit payment to account:", inv.remit_to_account])


def _letterhead(c: Canvas, inv: Invoice, vendor_name: str, issued: str) -> None:
    c.setFillColor(colors.HexColor("#1f3a5f"))
    c.rect(0, HEIGHT - 30 * mm, WIDTH, 30 * mm, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 18)
    c.drawString(15 * mm, HEIGHT - 18 * mm, vendor_name.upper())
    c.setFillColor(colors.black)
    c.rect(WIDTH - 80 * mm, HEIGHT - 70 * mm, 65 * mm, 32 * mm)
    c.setFont("Helvetica", 9)
    c.drawString(WIDTH - 77 * mm, HEIGHT - 45 * mm, "AMOUNT DUE")
    c.setFont("Helvetica-Bold", 16)
    c.drawString(WIDTH - 77 * mm, HEIGHT - 55 * mm, f"${_money(inv.total)}")
    c.setFont("Helvetica", 8)
    c.drawString(WIDTH - 77 * mm, HEIGHT - 64 * mm, f"Pay to {inv.remit_to_account}")
    meta = [f"Invoice # {inv.invoice_no}", f"Issued {issued}"]
    meta.append(f"Your order {inv.po_number}" if inv.po_number else "Your order: n/a")
    _lines(c, 15 * mm, HEIGHT - 45 * mm, [*meta, "", "Customer:", BILL_TO[0]])
    header = [["Code", "Item", "Units", "Price", "Line total"]]
    _table(c, header + _rows(inv), 15 * mm, HEIGHT - 90 * mm)


def _compact(c: Canvas, inv: Invoice, vendor_name: str, issued: str) -> None:
    c.setFont("Helvetica-Bold", 11)
    c.drawString(15 * mm, HEIGHT - 15 * mm, f"{vendor_name} - Invoice {inv.invoice_no}")
    ref = f"  |  Ref: {inv.po_number}" if inv.po_number else ""
    _lines(c, 15 * mm, HEIGHT - 21 * mm, [f"{issued}  |  To: {BILL_TO[0]}{ref}"], size=8)
    header = [["SKU", "Description", "Qty", "Rate", "Amount"]]
    total = [["", "TOTAL DUE", "", inv.currency, _money(inv.total)]]
    bottom = _table(c, header + _rows(inv) + total, 15 * mm, HEIGHT - 28 * mm, font_size=8)
    _lines(c, 15 * mm, bottom - 8 * mm, [f"Remit to {inv.remit_to_account}"], size=8)


def _eu(c: Canvas, inv: Invoice, vendor_name: str, issued: str) -> None:
    c.setFont("Helvetica", 9)
    c.drawRightString(WIDTH - 20 * mm, HEIGHT - 20 * mm, vendor_name)
    c.drawRightString(WIDTH - 20 * mm, HEIGHT - 25 * mm, f"Invoice date: {issued}")
    _lines(c, 20 * mm, HEIGHT - 40 * mm, list(BILL_TO))
    c.setFont("Helvetica-Bold", 14)
    c.drawString(20 * mm, HEIGHT - 65 * mm, f"Tax invoice {inv.invoice_no}")
    if inv.po_number:
        _lines(c, 20 * mm, HEIGHT - 72 * mm, [f"Order reference: {inv.po_number}"])
    header = [["Art. no.", "Description", "Qty", "Unit price", "Net"]]
    bottom = _table(c, header + _rows(inv, eu=True), 20 * mm, HEIGHT - 80 * mm)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(20 * mm, bottom - 15 * mm, "Amount due")
    c.drawString(20 * mm, bottom - 21 * mm, f"{_money(inv.total, eu=True)} {inv.currency}")
    _lines(c, 20 * mm, 25 * mm, [f"Bank details: {inv.remit_to_account}"], size=8)


LAYOUTS = (_classic, _letterhead, _compact, _eu)


def render(inv: Invoice, vendor: Vendor, path: Path) -> None:
    # invariant=1 drops timestamps and random IDs so a seed always yields identical bytes.
    c = Canvas(str(path), pagesize=A4, invariant=1)
    name = vendor.short_name if inv.short_vendor_name else vendor.name
    LAYOUTS[inv.layout](c, inv, name, inv.invoice_date.strftime(inv.date_format))
    c.showPage()
    c.save()
