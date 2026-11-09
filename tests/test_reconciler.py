from pathlib import Path

import psycopg
import pytest
from helpers import ACCOUNT, FakeRouter, add_intent, bank, extracted
from langgraph.types import Command
from psycopg.rows import dict_row

from ledger.graph import build as b
from ledger.graph.nodes import extract
from ledger.side_effects import dispatcher
from ledger.workers import reconciler
from world.generator import World


@pytest.fixture
def con(db: str):
    with psycopg.connect(db, autocommit=True, row_factory=dict_row) as c:
        yield c


def test_confirms_paid_intents_and_flags_the_rest(con, world: World, tmp_path: Path) -> None:
    vendor = world.vendors[0].id
    client = bank(tmp_path / "bank.db")
    add_intent(con, vendor, "R-PAID", "10.00")
    dispatcher.run_once(con, client)
    add_intent(con, vendor, "R-UNSENT", "20.00")
    for key in ("stray-1", "stray-2"):
        client.post(
            "/payments",
            json={"invoice_id": "R-TWICE", "amount": "5.00", "account": ACCOUNT},
            headers={"Idempotency-Key": key},
        )

    report = reconciler.reconcile_once(con, client)
    assert report.confirmed == 1
    assert report.unpaid == {"R-UNSENT": "pending"}
    assert len(report.unknown_payments) == 2
    assert report.paid_twice == {"R-TWICE": 2}
    status = con.execute("SELECT status FROM payment_intents WHERE invoice_id = 'R-PAID'")
    assert status.fetchone()["status"] == "confirmed"
    assert reconciler.reconcile_once(con, client).confirmed == 0


def test_thread_resumes_only_after_the_bank_confirms(
    db: str, con, world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(extract, "render", lambda _: "")
    lb = next(lb for lb in world.labels if lb.exception_type == "bank_change")
    inv = next(i for i in world.invoices if i.id == lb.invoice_id)
    vendor = next(v for v in world.vendors if v.id == inv.vendor_id)
    config = b.thread(inv.id)
    pool = b.pool(db)
    graph = b.build(b.postgres_saver(pool), FakeRouter(extracted(inv, vendor)))
    graph.invoke({"invoice_id": inv.id, "pdf_path": "x.pdf", "email": None}, config)
    graph.invoke(Command(resume={"decision": "approve", "by": "t"}), config, durability="sync")
    assert graph.get_state(config).next == ("reconcile",)

    client = bank(tmp_path / "bank.db")
    reconciler.reconcile_once(con, client)
    assert reconciler.resume_confirmed(con, graph) == []

    dispatcher.run_once(con, client)
    reconciler.reconcile_once(con, client)
    assert reconciler.resume_confirmed(con, graph) == [inv.id]
    assert reconciler.resume_confirmed(con, graph) == []
    state = graph.get_state(config)
    pool.close()
    assert state.next == ()
    assert state.values["status"] == "paid"
    assert [e.event for e in state.values["audit"]].count("payment.confirmed") == 1
