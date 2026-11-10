from datetime import date
from decimal import Decimal

import pytest
from helpers import FakeRouter

from ledger.contracts.invoice import ExtractedInvoice
from ledger.graph.nodes import extract


def _inv(confidence: float = 0.99, total: str = "20.00") -> ExtractedInvoice:
    return ExtractedInvoice(
        vendor_name="V",
        invoice_no="1",
        invoice_date=date(2026, 1, 5),
        po_number=None,
        currency="USD",
        lines=[{"sku": "S", "description": "d", "qty": "2", "unit_price": "10.00"}],
        total=Decimal(total),
        remit_to_account="GB00",
        field_confidence={"total": confidence},
    )


@pytest.fixture(autouse=True)
def no_render(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extract, "render", lambda _: "")


def _run(router: FakeRouter) -> dict:
    return extract.run({"invoice_id": "I", "pdf_path": "x.pdf"}, router)


def test_confident_extraction_stays_on_primary() -> None:
    out = _run(FakeRouter(_inv()))
    assert out["status"] == "extracted"
    assert [e.detail["model"] for e in out["audit"]] == ["fake/weak"]


def test_low_confidence_escalates_to_stronger_model() -> None:
    out = _run(FakeRouter(_inv(0.4), _inv()))
    assert out["status"] == "extracted"
    assert [e.detail["model"] for e in out["audit"]] == ["fake/weak", "fake/mid"]


def test_arithmetic_mismatch_counts_as_low_confidence() -> None:
    out = _run(FakeRouter(_inv(total="21.00")))
    assert out["status"] == "needs_human"
    assert len(out["audit"]) == 3
    assert "lines sum to 20.00" in out["audit"][-1].detail["low"][-1]
