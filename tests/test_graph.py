from collections import Counter

import pytest
from helpers import FakeRouter

from evals.truth import extracted
from ledger.graph import build as b
from ledger.graph.nodes import extract
from world.generator import World

EXPECTED_STATUS = {
    "clean": "payment_pending",
    "qty_variance": "payment_pending",
    "duplicate": "rejected",
    "missing_po": "held",
    "credit_note": "credit_note_requested",
    "bank_change": "awaiting_approval",
}


def test_world_runs_through_graph(db: str, world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """Perfect extraction, real matcher, policy and checkpoints, every invoice in arrival order."""
    monkeypatch.setattr(extract, "render", lambda _: "")
    vendors = {v.id: v for v in world.vendors}
    emails = {e.invoice_id: e.body for e in world.emails}
    labels = {lb.invoice_id: lb for lb in world.labels}
    router = FakeRouter()
    pool = b.pool(db)
    graph = b.build(b.postgres_saver(pool), router)
    seen: Counter[str] = Counter()
    wrong = {}
    for inv in world.invoices:
        lb = labels[inv.id]
        router.extractions = [extracted(inv, vendors[inv.vendor_id])]
        config = b.thread(f"graph-{inv.id}")
        state = {"invoice_id": inv.id, "pdf_path": "unused.pdf", "email": emails.get(inv.id)}
        out = graph.invoke(state, config)
        routed = out["match"].exceptions
        expected = EXPECTED_STATUS.get(lb.exception_type) or (
            "payment_pending" if lb.correct_action == "pay" else "held"
        )
        if routed != ([] if lb.exception_type == "clean" else [lb.exception_type]):
            wrong[inv.id] = ("routed", lb.exception_type, routed)
        elif out["status"] != expected:
            wrong[inv.id] = ("status", lb.exception_type, out["status"])
        if lb.exception_type == "bank_change":
            assert graph.get_state(config).next == ("approve",)
        if lb.exception_type == "clean":
            assert "approve" not in [e.actor for e in out["audit"]]
            assert router.calls[-1] == "extraction"
        seen[lb.exception_type] += 1
    pool.close()
    assert wrong == {}
    assert set(seen) == set(EXPECTED_STATUS) | {"price_variance"}
