import base64
import io

import pypdfium2

from ledger.contracts.decisions import AuditEvent
from ledger.contracts.invoice import ExtractedInvoice
from ledger.contracts.money import CENT
from ledger.contracts.state import InvoiceState
from ledger.llm.router import AllModelsFailed, ModelRouter
from ledger.settings import CONFIDENCE_THRESHOLD

PROMPT = """You read supplier invoices and copy fields exactly as printed.
- vendor_name: the supplier at the top of the invoice, never the bill-to party.
- invoice_date: ISO format YYYY-MM-DD, whatever format is printed.
- Amounts and quantities: plain decimal strings with a dot and no thousands separators.
  Some invoices print European numbers, where 1.234,56 means 1234.56.
- A credit line has a negative quantity.
- po_number: null when no PO is printed.
- remit_to_account: the account number to pay, without spaces.
- field_confidence: 0 to 1 for each of vendor_name, invoice_no, invoice_date, po_number,
  lines, total and remit_to_account. Be honest; low confidence sends the invoice to a
  stronger reader."""


def render(pdf_path: str) -> str:
    page = pypdfium2.PdfDocument(pdf_path)[0]
    buf = io.BytesIO()
    page.render(scale=2).to_pil().save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def doubts(inv: ExtractedInvoice) -> list[str]:
    low = sorted(k for k, c in inv.field_confidence.items() if c < CONFIDENCE_THRESHOLD)
    if abs(inv.lines_total - inv.total) > CENT:
        low.append(f"lines sum to {inv.lines_total}, total reads {inv.total}")
    return low


def run(state: InvoiceState, router: ModelRouter) -> InvoiceState:
    messages = [
        {"role": "system", "content": PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Extract this invoice."},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{render(state['pdf_path'])}"},
                },
            ],
        },
    ]
    audit = []
    index = 0
    while True:
        try:
            reply = router.complete("extraction", messages, ExtractedInvoice, start_at=index)
        except AllModelsFailed:
            if index == 0:
                raise
            return {"status": "needs_human", "audit": audit}
        low = doubts(reply.value)
        audit.append(
            AuditEvent(
                actor="extract", event="extracted", detail={"model": reply.model, "low": low}
            )
        )
        if not low:
            return {"extracted": reply.value, "status": "extracted", "audit": audit}
        index = reply.index + 1
        if index >= len(router.roles["extraction"]):
            return {"extracted": reply.value, "status": "needs_human", "audit": audit}
