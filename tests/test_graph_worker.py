import psycopg
import pytest
from helpers import FakeRouter
from psycopg.rows import dict_row

from evals.truth import extracted
from ledger.graph import build as b
from ledger.graph.nodes import extract
from ledger.workers import graph_worker
from world.generator import World


@pytest.fixture
def con(db: str):
    with psycopg.connect(db, autocommit=True, row_factory=dict_row) as c:
        yield c


def test_a_dead_workers_job_is_claimed_again_after_its_lease(con) -> None:
    graph_worker.enqueue(con, "W-1", "run", {})
    first = con.execute(graph_worker.CLAIM, (60, graph_worker.MAX_ATTEMPTS)).fetchone()
    assert con.execute(graph_worker.CLAIM, (60, graph_worker.MAX_ATTEMPTS)).fetchone() is None
    con.execute("UPDATE graph_jobs SET lease_until = now() - interval '1 second'")
    again = con.execute(graph_worker.CLAIM, (60, graph_worker.MAX_ATTEMPTS)).fetchone()
    assert again["id"] == first["id"]


def test_enqueue_is_idempotent_per_kind(con) -> None:
    assert graph_worker.enqueue(con, "W-2", "run", {})
    assert not graph_worker.enqueue(con, "W-2", "run", {})
    assert graph_worker.enqueue(con, "W-2", "resume", {})


def test_input_follows_the_checkpoint(
    db: str, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(extract, "render", lambda _: "")
    lb = next(lb for lb in world.labels if lb.exception_type == "bank_change")
    inv = next(i for i in world.invoices if i.id == lb.invoice_id)
    vendor = next(v for v in world.vendors if v.id == inv.vendor_id)
    pool = b.pool(db)
    graph = b.build(b.postgres_saver(pool), FakeRouter(extracted(inv, vendor)))
    run = {"invoice_id": inv.id, "kind": "run", "payload": {"pdf_path": "x", "email": None}}
    resume = {"invoice_id": inv.id, "kind": "resume", "payload": {"decision": "approve", "by": "t"}}

    go, value = graph_worker._input(graph, run)
    assert go and value["invoice_id"] == inv.id
    graph.invoke(value, b.thread(inv.id), durability="sync")
    assert graph_worker._input(graph, run) == (False, None)

    go, value = graph_worker._input(graph, resume)
    assert go and value.resume["decision"] == "approve"
    graph.invoke(value, b.thread(inv.id), durability="sync")
    assert graph_worker._input(graph, resume) == (False, None)
    pool.close()
