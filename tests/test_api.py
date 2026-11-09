from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from helpers import FakeRouter, extracted
from psycopg.rows import dict_row

from ledger.api.main import create_app
from ledger.graph import build as b
from ledger.workers import graph_worker
from world import pdf_render
from world.generator import World


@pytest.fixture
def router() -> FakeRouter:
    return FakeRouter()


@pytest.fixture
def client(db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, router: FakeRouter):
    monkeypatch.setenv("LEDGER_INBOX", str(tmp_path / "inbox"))
    with TestClient(create_app(router)) as c:
        yield c


@pytest.fixture
def drain(db: str, router: FakeRouter):
    pool = b.pool(db)
    graph = b.build(b.postgres_saver(pool), router)

    def run() -> int:
        with psycopg.connect(db, autocommit=True, row_factory=dict_row) as con:
            jobs = 0
            while graph_worker.run_once(con, lambda _: graph):
                jobs += 1
            return jobs

    yield run
    pool.close()


def _post(
    client: TestClient, router: FakeRouter, world: World, tmp_path: Path, kind: str, nth: int = 0
) -> str:
    lb = [lb for lb in world.labels if lb.exception_type == kind][nth]
    inv = next(i for i in world.invoices if i.id == lb.invoice_id)
    vendor = next(v for v in world.vendors if v.id == inv.vendor_id)
    pdf = tmp_path / f"{inv.id}.pdf"
    pdf_render.render(inv, vendor, pdf)
    router.extractions = [extracted(inv, vendor)]
    resp = client.post("/invoices", files={"pdf": pdf.read_bytes()}, data={"invoice_id": inv.id})
    assert resp.status_code == 202
    assert resp.json() == {"thread_id": inv.id}
    return inv.id


def test_clean_invoice_pays_without_a_human(
    client: TestClient, router: FakeRouter, drain, world: World, tmp_path: Path
) -> None:
    thread_id = _post(client, router, world, tmp_path, "clean")
    assert client.get(f"/invoices/{thread_id}").status_code == 404
    assert drain() == 1
    body = client.get(f"/invoices/{thread_id}").json()
    assert body["status"] == "payment_pending"
    assert body["next"] == ["reconcile"]
    assert isinstance(body["state"]["proposal"]["amount"], str)
    assert [e["event"] for e in body["audit"]][-1] == "payment_intent.recorded"
    assert client.post(f"/invoices/{thread_id}/approve", json={"by": "x"}).status_code == 409


def test_bank_change_waits_then_approval_resumes(
    client: TestClient, router: FakeRouter, drain, world: World, tmp_path: Path
) -> None:
    thread_id = _post(client, router, world, tmp_path, "bank_change")
    drain()
    body = client.get(f"/invoices/{thread_id}").json()
    assert body["status"] == "awaiting_approval"
    assert body["next"] == ["approve"]
    assert body["interrupt"]["proposal"]["requires_approval"] is True

    assert _post(client, router, world, tmp_path, "bank_change") == thread_id
    assert drain() == 0

    resp = client.post(f"/invoices/{thread_id}/approve", json={"by": "treasury@ledger"})
    assert resp.status_code == 202
    assert client.post(f"/invoices/{thread_id}/reject", json={"by": "x"}).status_code == 409
    assert drain() == 1
    assert client.get(f"/invoices/{thread_id}").json()["status"] == "payment_pending"


def test_reject_ends_without_payment(
    client: TestClient, router: FakeRouter, drain, world: World, tmp_path: Path
) -> None:
    thread_id = _post(client, router, world, tmp_path, "bank_change", nth=1)
    drain()
    client.post(f"/invoices/{thread_id}/reject", json={"by": "treasury@ledger", "note": "fraud"})
    drain()
    body = client.get(f"/invoices/{thread_id}").json()
    assert body["status"] == "rejected"
    assert "payment_intent.recorded" not in [e["event"] for e in body["audit"]]


def test_unknown_invoice_is_404(client: TestClient) -> None:
    assert client.get("/invoices/nope").status_code == 404
