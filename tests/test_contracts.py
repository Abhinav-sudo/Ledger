import json
from datetime import date
from decimal import Decimal

import pytest
from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from ledger.contracts.decisions import Analysis, Proposal
from ledger.contracts.invoice import ExtractedInvoice, LineItem
from ledger.contracts.state import InvoiceState


def _invoice(**overrides: object) -> ExtractedInvoice:
    fields = {
        "vendor_name": "Kestrel Logistics Ltd",
        "invoice_no": "INV-1",
        "invoice_date": date(2026, 3, 4),
        "po_number": "PO-10001",
        "currency": "USD",
        "lines": [{"sku": "SKU-0001", "description": "Gloves", "qty": "3", "unit_price": "0.10"}],
        "total": "0.30",
        "remit_to_account": "GB00LDGR00000000000001",
        "field_confidence": {"total": 0.99},
    } | overrides
    return ExtractedInvoice.model_validate(fields)


def test_money_is_exact_decimal() -> None:
    inv = _invoice()
    assert inv.total == Decimal("0.30")
    assert inv.lines_total == inv.total


def test_money_rejects_float() -> None:
    with pytest.raises(ValidationError, match="never a float"):
        LineItem(sku="X", description="x", qty=Decimal(1), unit_price=0.1)
    with pytest.raises(ValidationError):
        _invoice(total=0.3)


def test_llm_json_parsed_with_decimal_floats_validates() -> None:
    raw = json.loads(
        '{"action": "pay", "amount": 1234.56, "rationale": "r", "requires_approval": false}',
        parse_float=Decimal,
    )
    assert Proposal.model_validate(raw).amount == Decimal("1234.56")


def test_unknown_action_and_negative_amount_rejected() -> None:
    with pytest.raises(ValidationError):
        Proposal(action="refund", amount=Decimal(1), rationale="", requires_approval=False)
    with pytest.raises(ValidationError):
        Proposal(action="pay", amount=Decimal(-1), rationale="", requires_approval=False)


def test_parallel_analyses_are_merged_by_reducer() -> None:
    def a(_: InvoiceState) -> InvoiceState:
        return {"analyses": [Analysis(agent="a", finding="x", evidence=[], confidence=1)]}

    def b(_: InvoiceState) -> InvoiceState:
        return {"analyses": [Analysis(agent="b", finding="y", evidence=[], confidence=1)]}

    g = StateGraph(InvoiceState)
    g.add_node("a", a)
    g.add_node("b", b)
    g.add_edge(START, "a")
    g.add_edge(START, "b")
    g.add_edge("a", END)
    g.add_edge("b", END)
    out = g.compile().invoke({"invoice_id": "INV-1", "analyses": []})
    assert sorted(x.agent for x in out["analyses"]) == ["a", "b"]
