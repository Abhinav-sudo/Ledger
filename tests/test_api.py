from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from helpers import FakeRouter, extracted

from ledger.api.main import create_app
from world import pdf_render
from world.generator import World


@pytest.fixture
def client(db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LEDGER_INBOX", str(tmp_path / "inbox"))
    router = FakeRouter()
    with TestClient(create_app(router)) as c:
        c.router_ = router
        yield c


def _post(
    client: TestClient, world: World, tmp_path: Path, exception_type: str, nth: int = 0
) -> str:
    lb = [lb for lb in world.labels if lb.exception_type == exception_type][nth]
    inv = next(i for i in world.invoices if i.id == lb.invoice_id)
    vendor = next(v for v in world.vendors if v.id == inv.vendor_id)
    pdf = tmp_path / f"{inv.id}.pdf"
    pdf_render.render(inv, vendor, pdf)
    client.router_.extractions = [extracted(inv, vendor)]
    thread_id = f"api-{inv.id}"
    resp = client.post("/invoices", files={"pdf": pdf.read_bytes()}, data={"invoice_id": thread_id})
    assert resp.status_code == 202
    assert resp.json() == {"thread_id": thread_id}
    return thread_id


def test_clean_invoice_pays_without_a_human(
    client: TestClient, world: World, tmp_path: Path
) -> None:
    thread_id = _post(client, world, tmp_path, "clean")
    body = client.get(f"/invoices/{thread_id}").json()
    assert body["status"] == "payment_pending"
    assert body["next"] == ["reconcile"]
    assert isinstance(body["state"]["proposal"]["amount"], str)
    assert [e["event"] for e in body["audit"]][-1] == "payment_intent.recorded"
    assert client.post(f"/invoices/{thread_id}/approve", json={"by": "x"}).status_code == 409


def test_bank_change_waits_then_approval_resumes(
    client: TestClient, world: World, tmp_path: Path
) -> None:
    thread_id = _post(client, world, tmp_path, "bank_change")
    body = client.get(f"/invoices/{thread_id}").json()
    assert body["status"] == "awaiting_approval"
    assert body["next"] == ["approve"]
    assert body["interrupt"]["proposal"]["requires_approval"] is True

    assert _post(client, world, tmp_path, "bank_change") == thread_id
    assert client.get(f"/invoices/{thread_id}").json()["next"] == ["approve"]

    resp = client.post(f"/invoices/{thread_id}/approve", json={"by": "treasury@ledger"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "payment_pending"
    assert client.post(f"/invoices/{thread_id}/reject", json={"by": "x"}).status_code == 409


def test_reject_ends_without_payment(client: TestClient, world: World, tmp_path: Path) -> None:
    thread_id = _post(client, world, tmp_path, "bank_change", nth=1)
    client.post(f"/invoices/{thread_id}/reject", json={"by": "treasury@ledger", "note": "fraud"})
    body = client.get(f"/invoices/{thread_id}").json()
    assert body["status"] == "rejected"
    assert "payment_intent.recorded" not in [e["event"] for e in body["audit"]]


def test_unknown_invoice_is_404(client: TestClient) -> None:
    assert client.get("/invoices/nope").status_code == 404
