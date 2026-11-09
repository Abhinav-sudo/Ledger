from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from helpers import FakeRouter, extracted
from langgraph.types import Command

from ledger.graph import build as b
from ledger.graph.nodes import approve
from world import pdf_render
from world.generator import World


def test_approve_replays_on_resume_but_writes_once(
    db: str,
    world: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    label = next(
        lb for lb in world.labels if lb.exception_type == "bank_change" and "remit-to" in lb.notes
    )
    inv = next(i for i in world.invoices if i.id == label.invoice_id)
    vendor = next(v for v in world.vendors if v.id == inv.vendor_id)
    pdf = tmp_path / f"{inv.id}.pdf"
    pdf_render.render(inv, vendor, pdf)

    runs: list[str] = []
    real = approve.run

    def counted(state: dict) -> dict:
        runs.append(state["invoice_id"])
        return real(state)

    monkeypatch.setattr(approve, "run", counted)
    config = b.thread(inv.id)

    pool = b.pool(db)
    graph = b.build(b.postgres_saver(pool), FakeRouter(extracted(inv, vendor)))
    graph.invoke({"invoice_id": inv.id, "pdf_path": str(pdf), "email": None}, config)
    paused = graph.get_state(config)
    assert paused.next == ("approve",)
    assert paused.values["status"] == "awaiting_approval"
    assert paused.interrupts[0].value["proposal"]["approver_role"] == "treasury"
    assert len(runs) == 1
    pool.close()
    del graph

    pool = b.pool(db)
    graph = b.build(b.postgres_saver(pool), FakeRouter())
    out = graph.invoke(Command(resume={"decision": "approve", "by": "treasury@ledger"}), config)
    pool.close()

    assert len(runs) == 2
    events = [e.event for e in out["audit"]]
    assert events.count("approval.approve") == 1
    assert events.count("payment_intent.called") == 1
    assert events.count("matched") == 1
    assert out["approval"].by == "treasury@ledger"
    assert out["status"] == "payment_pending"
    assert out["proposal"].amount == inv.total
    assert isinstance(out["proposal"].amount, Decimal)
    serde = [r.message for r in caplog.records if r.name.startswith("langgraph.checkpoint")]
    assert not [m for m in serde if "unregistered" in m or "Blocked" in m]
    with psycopg.connect(db) as con:
        assert con.execute("SELECT count(*) FROM invoices WHERE id = %s", (inv.id,)).fetchone() == (
            1,
        )
        assert con.execute("SELECT count(*) FROM payment_intents").fetchone() == (0,)
