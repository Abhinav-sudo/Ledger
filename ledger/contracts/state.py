import operator
from typing import Annotated, TypedDict

from ledger.contracts.decisions import Analysis, Approval, AuditEvent, Proposal
from ledger.contracts.invoice import ExtractedInvoice, MatchResult
from ledger.contracts.payment import PaymentIntent


class InvoiceState(TypedDict, total=False):
    invoice_id: str
    pdf_path: str
    email: str | None
    status: str
    extracted: ExtractedInvoice | None
    match: MatchResult | None
    analyses: Annotated[list[Analysis], operator.add]
    proposal: Proposal | None
    approval: Approval | None
    intent: PaymentIntent | None
    audit: Annotated[list[AuditEvent], operator.add]
