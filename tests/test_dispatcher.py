from pathlib import Path

import psycopg
import pytest
from helpers import add_intent, bank
from psycopg.rows import dict_row

from ledger.side_effects import dispatcher
from world.generator import World


@pytest.fixture
def con(db: str):
    with psycopg.connect(db, autocommit=True, row_factory=dict_row) as c:
        yield c


def _intent(con: psycopg.Connection, world: World, invoice_id: str = "D-1") -> str:
    return add_intent(con, world.vendors[0].id, invoice_id, "250.00")


def _row(con: psycopg.Connection, key: str) -> dict:
    return con.execute(
        "SELECT * FROM payment_intents WHERE idempotency_key = %s", (key,)
    ).fetchone()


def _due_now(con: psycopg.Connection) -> None:
    con.execute("UPDATE payment_intents SET next_attempt_at = now()")


def test_sends_once_and_marks_sent(con, world: World, tmp_path: Path) -> None:
    key = _intent(con, world)
    client = bank(tmp_path / "bank.db")
    assert dispatcher.run_once(con, client)
    assert not dispatcher.run_once(con, client)
    row = _row(con, key)
    assert row["status"] == "sent"
    assert len(client.get("/statement").json()["payments"]) == 1


def test_dropped_response_is_retried_with_the_same_key(con, world: World, tmp_path: Path) -> None:
    key = _intent(con, world)
    dropping = bank(tmp_path / "bank.db", drop_rate=1.0)
    dispatcher.run_once(con, dropping)
    row = _row(con, key)
    assert (row["status"], row["attempts"]) == ("pending", 1)
    assert row["next_attempt_at"] > row["created_at"]

    _due_now(con)
    healthy = bank(tmp_path / "bank.db")
    dispatcher.run_once(con, healthy)
    assert _row(con, key)["status"] == "sent"
    assert len(healthy.get("/statement").json()["payments"]) == 1


def test_dead_letters_after_max_attempts(con, world: World, tmp_path: Path) -> None:
    key = _intent(con, world)
    failing = bank(tmp_path / "bank.db", fail_rate=1.0)
    for _ in range(dispatcher.MAX_ATTEMPTS):
        _due_now(con)
        dispatcher.run_once(con, failing)
    row = _row(con, key)
    assert (row["status"], row["attempts"]) == ("failed", dispatcher.MAX_ATTEMPTS)
    assert "http 500" in row["last_error"]


def test_a_claimed_row_is_skipped_by_another_dispatcher(db: str, con, world: World) -> None:
    _intent(con, world)
    with (
        psycopg.connect(db, row_factory=dict_row) as a,
        psycopg.connect(db, row_factory=dict_row) as b,
    ):
        assert dispatcher.claim(a) is not None
        assert dispatcher.claim(b) is None
        a.rollback()
        assert dispatcher.claim(b) is not None


def test_backoff_grows_and_is_capped() -> None:
    assert dispatcher.backoff(0) <= dispatcher.BASE_DELAY_S
    assert dispatcher.backoff(4) >= dispatcher.BASE_DELAY_S * 2**4 / 2
    assert dispatcher.backoff(40) <= dispatcher.MAX_DELAY_S
