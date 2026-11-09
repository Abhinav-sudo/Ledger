from langgraph.graph import END
from langgraph.types import Send

from ledger.contracts.state import InvoiceState

ANALYST_FOR = {
    "price_variance": "variance",
    "qty_variance": "variance",
    "credit_note": "variance",
    "duplicate": "duplicate_fraud",
    "bank_change": "duplicate_fraud",
    "missing_po": "missing_po",
}


def route(state: InvoiceState) -> str | list[Send]:
    if state["status"] == "needs_human":
        return END
    targets = sorted({ANALYST_FOR[e] for e in state["match"].exceptions})
    if not targets:
        return "policy"
    return [Send(t, state) for t in targets]
