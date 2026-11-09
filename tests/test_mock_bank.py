import asyncio
import tempfile
from collections import Counter
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st

from world.mock_bank.app import create_app

KEYS = [f"key-{i}" for i in range(4)]
BODY = {
    k: {"invoice_id": f"INV-{i:05d}", "amount": "1250.00", "account": "GB00LDGR1"}
    for i, k in enumerate(KEYS)
}


def _client(tmp_path: Path, **injection: float) -> TestClient:
    return TestClient(create_app(tmp_path / "bank.db", seed=0, **injection))


def _pay(client: TestClient, key: str | None, body: dict[str, str]) -> httpx.Response:
    headers = {"Idempotency-Key": key} if key else {}
    return client.post("/payments", json=body, headers=headers)


def test_replay_returns_original_payment(tmp_path: Path) -> None:
    client = _client(tmp_path)
    first = _pay(client, "k1", BODY["key-0"])
    again = _pay(client, "k1", BODY["key-0"])
    assert (first.status_code, again.status_code) == (201, 200)
    assert first.json() == again.json()
    assert len(client.get("/statement").json()["payments"]) == 1


def test_key_reuse_with_different_body_is_rejected(tmp_path: Path) -> None:
    client = _client(tmp_path)
    _pay(client, "k1", BODY["key-0"])
    assert _pay(client, "k1", {**BODY["key-0"], "amount": "1250.01"}).status_code == 422
    assert len(client.get("/statement").json()["payments"]) == 1


def test_missing_key_is_rejected(tmp_path: Path) -> None:
    client = _client(tmp_path)
    assert _pay(client, None, BODY["key-0"]).status_code == 400
    assert client.get("/statement").json()["payments"] == []


def test_injected_failure_pays_nothing(tmp_path: Path) -> None:
    client = _client(tmp_path, fail_rate=1.0)
    assert _pay(client, "k1", BODY["key-0"]).status_code == 500
    assert client.get("/statement").json()["payments"] == []


def test_dropped_response_still_pays_once(tmp_path: Path) -> None:
    client = _client(tmp_path, drop_rate=1.0)
    assert [_pay(client, "k1", BODY["key-0"]).status_code for _ in range(3)] == [503] * 3
    assert len(client.get("/statement").json()["payments"]) == 1


def test_timeout_mode_still_processes(tmp_path: Path) -> None:
    client = _client(tmp_path, timeout_rate=1.0, timeout_s=0.01)
    assert _pay(client, "k1", BODY["key-0"]).status_code == 201


def test_keys_survive_a_bank_restart(tmp_path: Path) -> None:
    first = _pay(_client(tmp_path), "k1", BODY["key-0"])
    again = _pay(_client(tmp_path), "k1", BODY["key-0"])
    assert again.status_code == 200
    assert again.json()["payment_id"] == first.json()["payment_id"]


requests = st.tuples(st.sampled_from(KEYS), st.booleans())


async def _fire(
    db: Path, batches: list[list[tuple[str, bool]]], fail: float, drop: float, seed: int
) -> tuple[list[tuple[str, httpx.Response]], list[dict[str, str]]]:
    app = create_app(db, fail_rate=fail, drop_rate=drop, seed=seed)
    responses = []
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://bank"
    ) as c:
        for batch in batches:
            sent = [
                c.post(
                    "/payments",
                    headers={"Idempotency-Key": key},
                    json={**BODY[key], "amount": "9.99"} if tampered else BODY[key],
                )
                for key, tampered in batch
            ]
            results = await asyncio.gather(*sent)
            responses += [(key, r) for (key, _), r in zip(batch, results, strict=True)]
        payments = (await c.get("/statement")).json()["payments"]
    return responses, payments


@settings(max_examples=60, deadline=None)
@given(
    batches=st.lists(st.lists(requests, min_size=1, max_size=10), min_size=1, max_size=4),
    fail=st.sampled_from([0.0, 0.3]),
    drop=st.sampled_from([0.0, 0.3, 1.0]),
    seed=st.integers(0, 2**16),
)
def test_no_key_is_ever_paid_twice(
    batches: list[list[tuple[str, bool]]], fail: float, drop: float, seed: int
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        responses, payments = asyncio.run(_fire(Path(tmp) / "bank.db", batches, fail, drop, seed))

    per_key = Counter(p["idempotency_key"] for p in payments)
    assert all(count == 1 for count in per_key.values())
    paid = {p["idempotency_key"]: p for p in payments}
    for key, r in responses:
        if r.status_code in (200, 201):
            assert r.json() == paid[key]
        if r.status_code in (200, 201, 503):
            assert key in paid
