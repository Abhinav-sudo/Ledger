from collections import defaultdict
from decimal import Decimal

import pytest
from helpers import Erp, extracted

from ledger.contracts.erp import PriorInvoice
from ledger.contracts.invoice import MatchResult
from ledger.settings import PRICE_TOLERANCE, QTY_TOLERANCE
from ledger.tools.matcher import match
from world.generator import DEV_SEED, World, generate


@pytest.fixture(scope="module")
def dev() -> World:
    return generate(500, DEV_SEED)


@pytest.fixture(scope="module")
def results(dev: World) -> dict[str, MatchResult]:
    erp = Erp(dev)
    seen: dict[str, list[PriorInvoice]] = defaultdict(list)
    out = {}
    for inv in dev.invoices:
        out[inv.id] = match(
            extracted(inv, erp.vendors[inv.vendor_id]),
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
    return out


def test_every_label_is_detected_exactly(dev: World, results: dict[str, MatchResult]) -> None:
    wrong = {
        lb.invoice_id: (lb.exception_type, results[lb.invoice_id].exceptions)
        for lb in dev.labels
        if results[lb.invoice_id].exceptions
        != ([] if lb.exception_type == "clean" else [lb.exception_type])
    }
    assert wrong == {}


def test_price_tolerance_is_inclusive(dev: World, results: dict[str, MatchResult]) -> None:
    for lb in dev.labels:
        if lb.exception_type == "price_variance":
            within = all(d.within_tolerance for d in results[lb.invoice_id].deltas)
            assert within == (lb.correct_action == "pay"), lb.notes
    assert any("exactly at" in lb.notes for lb in dev.labels)


def test_legitimate_repeat_orders_are_not_duplicates(
    dev: World, results: dict[str, MatchResult]
) -> None:
    repeats = [lb for lb in dev.labels if lb.exception_type == "clean" and lb.related_invoice]
    assert repeats
    for lb in repeats:
        assert results[lb.invoice_id].exceptions == []


def test_injected_email_account_is_a_bank_change(
    dev: World, results: dict[str, MatchResult]
) -> None:
    injected = {e.invoice_id for e in dev.emails}
    assert len(injected) == 3
    for invoice_id in injected:
        assert results[invoice_id].exceptions == ["bank_change"]


def test_qty_deltas_carry_received_quantity(dev: World, results: dict[str, MatchResult]) -> None:
    invoices = {inv.id: inv for inv in dev.invoices}
    for lb in dev.labels:
        if lb.exception_type == "qty_variance":
            inv = invoices[lb.invoice_id]
            received = {d.line_no: d.expected for d in results[lb.invoice_id].deltas}
            payable = sum(
                (received.get(n, ln.qty) * ln.unit_price).quantize(Decimal("0.01"))
                for n, ln in enumerate(inv.lines, start=1)
            )
            assert payable == lb.correct_amount
