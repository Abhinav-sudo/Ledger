import os
import signal
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from helpers import FakeRouter, extracted
from langgraph.types import Command

from ledger import chaos
from ledger.graph import build as b
from ledger.graph.nodes import approve, payment_intent
from world import pdf_render
from world.generator import World

APPROVE = Command(resume={"decision": "approve", "by": "treasury@ledger"})


def _bank_change(world: World, tmp_path: Path) -> tuple[dict, FakeRouter]:
    labels = [lb for lb in world.labels if lb.exception_type == "bank_change"]
    inv = next(i for i in world.invoices if i.id == labels[0].invoice_id)
    vendor = next(v for v in world.vendors if v.id == inv.vendor_id)
    pdf = tmp_path / f"{inv.id}.pdf"
    pdf_render.render(inv, vendor, pdf)
    state = {"invoice_id": inv.id, "pdf_path": str(pdf), "email": None}
    return state, FakeRouter(extracted(inv, vendor))


def _count(monkeypatch: pytest.MonkeyPatch, module: object) -> list[str]:
    runs: list[str] = []
    real = module.run

    def counted(state: dict) -> dict:
        runs.append(state["invoice_id"])
        return real(state)

    monkeypatch.setattr(module, "run", counted)
    return runs


def _intents(db: str, invoice_id: str) -> int:
    with psycopg.connect(db) as con:
        return con.execute(
            "SELECT count(*) FROM payment_intents WHERE invoice_id = %s", (invoice_id,)
        ).fetchone()[0]


def test_approve_replays_on_resume_but_writes_once(
    db: str,
    world: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    state, router = _bank_change(world, tmp_path)
    runs = _count(monkeypatch, approve)
    config = b.thread(state["invoice_id"])

    pool = b.pool(db)
    graph = b.build(b.postgres_saver(pool), router)
    graph.invoke(state, config, durability="sync")
    paused = graph.get_state(config)
    assert paused.next == ("approve",)
    assert paused.values["status"] == "awaiting_approval"
    assert paused.interrupts[0].value["proposal"]["approver_role"] == "treasury"
    assert len(runs) == 1
    pool.close()
    del graph

    pool = b.pool(db)
    graph = b.build(b.postgres_saver(pool), FakeRouter())
    out = graph.invoke(APPROVE, config, durability="sync")
    pool.close()

    assert len(runs) == 2
    events = [e.event for e in out["audit"]]
    assert events.count("approval.approve") == 1
    assert events.count("payment_intent.recorded") == 1
    assert events.count("matched") == 1
    assert out["approval"].by == "treasury@ledger"
    assert out["status"] == "payment_pending"
    assert out["intent"].amount == out["proposal"].amount
    assert isinstance(out["intent"].amount, Decimal)
    assert _intents(db, state["invoice_id"]) == 1
    serde = [r.message for r in caplog.records if r.name.startswith("langgraph.checkpoint")]
    assert not [m for m in serde if "unregistered" in m or "Blocked" in m]


def test_payment_node_replayed_from_history_records_one_intent(
    db: str, world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, router = _bank_change(world, tmp_path)
    runs = _count(monkeypatch, payment_intent)
    config = b.thread(state["invoice_id"])
    pool = b.pool(db)
    graph = b.build(b.postgres_saver(pool), router)
    graph.invoke(state, config, durability="sync")
    graph.invoke(APPROVE, config, durability="sync")

    before_payment = next(
        s.config for s in graph.get_state_history(config) if s.next == ("payment_intent",)
    )
    graph.invoke(None, before_payment, durability="sync")
    pool.close()

    assert len(runs) == 2
    assert _intents(db, state["invoice_id"]) == 1


def _resume_in_killed_worker(db: str, invoice_id: str, chaos_point: str) -> int:
    """Approve in a child process that SIGKILLs itself at the given chaos point."""
    code = (
        "import sys; sys.path.insert(0, 'tests')\n"
        "from helpers import FakeRouter\n"
        "from langgraph.types import Command\n"
        "from ledger.graph import build as b\n"
        f"g = b.build(b.postgres_saver(b.pool({db!r})), FakeRouter())\n"
        "g.invoke(Command(resume={'decision': 'approve', 'by': 'treasury@ledger'}),\n"
        f"         b.thread({invoice_id!r}), durability='sync')\n"
    )
    env = {**os.environ, "LEDGER_DATABASE_URL": db, "LEDGER_CHAOS": f"{chaos_point}=1"}
    return subprocess.run([sys.executable, "-c", code], env=env, timeout=60).returncode


@pytest.mark.parametrize(
    ("chaos_point", "intents_after_kill", "pending", "parent_runs"),
    [
        ("outbox.before_commit", 0, ("payment_intent",), 1),
        ("outbox.after_commit", 1, (), 0),
    ],
)
def test_worker_killed_around_the_outbox_commit_records_one_intent(
    db: str,
    world: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    chaos_point: str,
    intents_after_kill: int,
    pending: tuple[str, ...],
    parent_runs: int,
) -> None:
    state, router = _bank_change(world, tmp_path)
    runs = _count(monkeypatch, payment_intent)
    config = b.thread(state["invoice_id"])
    pool = b.pool(db)
    graph = b.build(b.postgres_saver(pool), router)
    graph.invoke(state, config, durability="sync")

    assert _resume_in_killed_worker(db, state["invoice_id"], chaos_point) == -signal.SIGKILL
    assert _intents(db, state["invoice_id"]) == intents_after_kill
    # After the commit the node's writes are durable, so LangGraph counts it as done.
    assert graph.get_state(config).next == pending

    out = graph.invoke(None, config, durability="sync")
    pool.close()
    assert len(runs) == parent_runs
    assert out["status"] == "payment_pending"
    assert _intents(db, state["invoice_id"]) == 1


def test_failed_write_does_not_let_the_checkpoint_skip_the_intent(
    db: str, world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LangGraph keeps going after put_writes raises and checkpoints the node as done."""
    state, router = _bank_change(world, tmp_path)
    config = b.thread(state["invoice_id"])
    pool = b.pool(db)
    graph = b.build(b.postgres_saver(pool), router)
    graph.invoke(state, config, durability="sync")
    failed: list[str] = []

    def fail_first_commit(name: str) -> None:
        if name == "outbox.before_commit" and not failed:
            failed.append(name)
            raise RuntimeError("write failed")

    monkeypatch.setattr(chaos, "point", fail_first_commit)
    with pytest.raises(RuntimeError, match="write failed"):
        graph.invoke(APPROVE, config, durability="sync")
    persisted = graph.get_state(config)
    pool.close()
    assert persisted.values["status"] == "payment_pending"
    assert persisted.values["intent"] is not None
    assert _intents(db, state["invoice_id"]) == 1
