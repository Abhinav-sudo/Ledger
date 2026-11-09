import re
from decimal import Decimal

from ledger.contracts.erp import BankAccount, PriorInvoice, PurchaseOrder
from ledger.contracts.invoice import ExceptionType, ExtractedInvoice, LineDelta, MatchResult

ACCOUNT = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")


def normalise_account(account: str) -> str:
    return re.sub(r"\s+", "", account).upper()


def match(
    inv: ExtractedInvoice,
    invoice_id: str,
    vendor_id: str,
    po: PurchaseOrder | None,
    received: dict[int, Decimal],
    accounts: list[BankAccount],
    priors: list[PriorInvoice],
    email: str | None,
    price_tol: Decimal,
    qty_tol: Decimal,
) -> MatchResult:
    """Deterministic 3-way match: invoice against PO, goods receipts and vendor history."""
    exceptions: list[ExceptionType] = []
    deltas: list[LineDelta] = []
    evidence: list[str] = []
    reasons: list[str] = []

    if po is None or po.status != "open" or po.vendor_id != vendor_id:
        exceptions.append("missing_po")
        if inv.po_number is None:
            reasons.append("invoice cites no PO")
        elif po is None:
            reasons.append(f"{inv.po_number} does not exist")
        else:
            evidence.append(po.po_number)
            reasons.append(f"{po.po_number} is {po.status}, vendor {po.vendor_id}")
    else:
        evidence.append(po.po_number)
        po_lines = {pl.sku: pl for pl in po.lines}
        for no, line in enumerate(inv.lines, start=1):
            if line.qty <= 0:
                continue
            pl = po_lines.get(line.sku)
            if pl is None:
                deltas.append(
                    LineDelta(
                        line_no=no,
                        sku=line.sku,
                        kind="qty",
                        invoiced=line.qty,
                        expected=Decimal(0),
                        within_tolerance=False,
                    )
                )
                continue
            if line.unit_price != pl.unit_price:
                within = abs(line.unit_price - pl.unit_price) <= price_tol * pl.unit_price
                deltas.append(
                    LineDelta(
                        line_no=no,
                        sku=line.sku,
                        kind="price",
                        invoiced=line.unit_price,
                        expected=pl.unit_price,
                        within_tolerance=within,
                    )
                )
            got = received.get(pl.line_no, Decimal(0))
            if line.qty - got > qty_tol:
                deltas.append(
                    LineDelta(
                        line_no=no,
                        sku=line.sku,
                        kind="qty",
                        invoiced=line.qty,
                        expected=got,
                        within_tolerance=False,
                    )
                )
        if any(d.kind == "price" for d in deltas):
            exceptions.append("price_variance")
        if any(d.kind == "qty" for d in deltas):
            exceptions.append("qty_variance")

    if any(line.qty < 0 for line in inv.lines):
        exceptions.append("credit_note")
        reasons.append("invoice nets a credit line instead of a credit note")

    open_accounts = {a.account for a in accounts if a.valid_to is None}
    claimed = {normalise_account(inv.remit_to_account)}
    if email:
        claimed |= set(ACCOUNT.findall(email))
    unknown = sorted(claimed - open_accounts)
    if unknown:
        exceptions.append("bank_change")
        evidence += unknown
        reasons.append(f"payment requested to {', '.join(unknown)}, not the account on file")

    for p in priors:
        same_no = p.invoice_no == inv.invoice_no
        if p.id != invoice_id and (
            same_no or (p.invoice_date, p.total) == (inv.invoice_date, inv.total)
        ):
            exceptions.append("duplicate")
            evidence.append(p.id)
            reasons.append(f"same vendor and {'number' if same_no else 'date and total'} as {p.id}")
            break

    return MatchResult(
        vendor_id=vendor_id,
        exceptions=exceptions,
        deltas=deltas,
        evidence=evidence,
        reasons=reasons,
    )
