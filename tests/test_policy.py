from collections import defaultdict
from decimal import Decimal

from helpers import Erp

from evals.truth import extracted
from ledger.contracts.erp import PriorInvoice
from ledger.graph.nodes.policy import decide
from ledger.settings import PRICE_TOLERANCE, QTY_TOLERANCE
from ledger.tools.matcher import match
from world.generator import DEV_SEED, generate


def test_proposals_match_labels() -> None:
    world = generate(500, DEV_SEED)
    erp = Erp(world)
    labels = {lb.invoice_id: lb for lb in world.labels}
    seen: dict[str, list[PriorInvoice]] = defaultdict(list)
    wrong = {}
    for inv in world.invoices:
        ext = extracted(inv, erp.vendors[inv.vendor_id])
        m = match(
            ext,
            inv.id,
            inv.vendor_id,
            erp.pos.get(inv.po_number or ""),
            erp.received[inv.po_number or ""],
            erp.accounts[inv.vendor_id],
            seen[inv.vendor_id],
            erp.emails.get(inv.id),
            PRICE_TOLERANCE,
            QTY_TOLERANCE,
        )
        seen[inv.vendor_id].append(Erp.prior(inv))
        p = decide(ext, m, [])
        lb = labels[inv.id]
        if lb.exception_type == "bank_change":
            # Held for a human: the label's hold is the approval wait, not the proposal.
            ok = p.requires_approval and p.approver_role == "treasury"
        else:
            ok = (p.action, p.amount) == (lb.correct_action, lb.correct_amount)
            ok = ok and not p.requires_approval
        if not ok:
            wrong[inv.id] = (lb.exception_type, p.action, p.amount, lb.correct_amount)
        assert isinstance(p.amount, Decimal)
    assert wrong == {}
