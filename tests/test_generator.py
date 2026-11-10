from collections import Counter
from decimal import Decimal
from pathlib import Path

import pytest

from world.generator import DEV_SEED, MIX, PRICE_TOLERANCE, ZERO, World, allocate, generate, write


@pytest.fixture(scope="module")
def dev() -> World:
    return generate(500, DEV_SEED)


def _files(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


def test_same_seed_writes_identical_files(tmp_path: Path) -> None:
    write(generate(60, 7), tmp_path / "a")
    write(generate(60, 7), tmp_path / "b")
    assert _files(tmp_path / "a") == _files(tmp_path / "b")


def test_different_seed_differs() -> None:
    assert generate(60, 7).labels != generate(60, 8).labels


def test_allocation_is_exact_and_sums_to_n() -> None:
    assert allocate(500) == {
        "clean": 300,
        "price_variance": 60,
        "qty_variance": 40,
        "duplicate": 35,
        "missing_po": 35,
        "bank_change": 15,
        "credit_note": 15,
    }
    for n in (1, 7, 41, 499):
        assert sum(allocate(n).values()) == n


def test_distribution_within_two_points(dev: World) -> None:
    counts = Counter(lb.exception_type for lb in dev.labels)
    for case, share in MIX.items():
        assert abs(Decimal(counts[case]) / 500 - share) <= Decimal("0.02"), case


def test_every_invoice_has_one_label(dev: World) -> None:
    assert [lb.invoice_id for lb in dev.labels] == [inv.id for inv in dev.invoices]


def test_amounts_follow_actions(dev: World) -> None:
    invoices = {inv.id: inv for inv in dev.invoices}
    for lb in dev.labels:
        inv = invoices[lb.invoice_id]
        if lb.correct_action == "pay":
            assert lb.correct_amount == inv.total
        elif lb.correct_action == "pay_partial":
            assert ZERO < lb.correct_amount < inv.total
        elif lb.correct_action == "request_credit_note":
            assert lb.correct_amount == -sum(ln.amount for ln in inv.lines if ln.qty < 0)
        else:
            assert lb.correct_amount == ZERO


def test_duplicates_copy_an_earlier_invoice(dev: World) -> None:
    order = {inv.id: i for i, inv in enumerate(dev.invoices)}
    invoices = {inv.id: inv for inv in dev.invoices}
    for lb in dev.labels:
        if lb.exception_type != "duplicate":
            continue
        dup, orig = invoices[lb.invoice_id], invoices[lb.related_invoice]
        assert order[orig.id] < order[dup.id]
        assert (dup.vendor_id, dup.total, dup.invoice_date) == (
            orig.vendor_id,
            orig.total,
            orig.invoice_date,
        )
        assert dup.invoice_no != orig.invoice_no


def test_near_duplicates_are_legitimate(dev: World) -> None:
    invoices = {inv.id: inv for inv in dev.invoices}
    near = [lb for lb in dev.labels if lb.exception_type == "clean" and lb.related_invoice]
    assert near
    for lb in near:
        inv, orig = invoices[lb.invoice_id], invoices[lb.related_invoice]
        assert (inv.vendor_id, inv.total) == (orig.vendor_id, orig.total)
        assert inv.invoice_date != orig.invoice_date
        assert inv.po_number != orig.po_number


def test_price_variance_hits_the_boundary(dev: World) -> None:
    pos = {p.po_number: p for p in dev.purchase_orders}
    invoices = {inv.id: inv for inv in dev.invoices}
    ratios = []
    for lb in dev.labels:
        if lb.exception_type != "price_variance":
            continue
        inv = invoices[lb.invoice_id]
        po_prices = {ln.line_no: ln.unit_price for ln in pos[inv.po_number].lines}
        (ratio,) = [
            abs(ln.unit_price / po_prices[ln.po_line] - 1)
            for ln in inv.lines
            if ln.unit_price != po_prices[ln.po_line]
        ]
        assert (lb.correct_action == "pay") == (ratio <= PRICE_TOLERANCE)
        ratios.append(ratio)
    assert ratios.count(PRICE_TOLERANCE) >= 3


def test_injection_emails_target_bank_change(dev: World) -> None:
    vendors = {v.id: v for v in dev.vendors}
    invoices = {inv.id: inv for inv in dev.invoices}
    labels = {lb.invoice_id: lb for lb in dev.labels}
    assert 2 <= len(dev.emails) <= 3
    for e in dev.emails:
        inv = invoices[e.invoice_id]
        assert labels[inv.id].exception_type == "bank_change"
        assert labels[inv.id].correct_action == "hold"
        assert inv.remit_to_account == vendors[inv.vendor_id].current_account
