from decimal import Decimal

import psycopg
import pytest

from ledger import chaos
from ledger.contracts.payment import PaymentIntent
from ledger.graph import build as b
from ledger.side_effects import idempotency
from ledger.side_effects.outbox import CHANNEL, ConflictingIntent
from world.generator import World

ACCOUNT = "GB80LDGR07117181324261"


def _invoice(con: psycopg.Connection, world: World, invoice_id: str) -> None:
    con.execute(
        "INSERT INTO invoices (id, vendor_id, invoice_no, invoice_date, currency, "
        "remit_to_account, total) VALUES (%s, %s, %s, '2026-01-05', 'USD', %s, 100)",
        (invoice_id, world.vendors[0].id, invoice_id, ACCOUNT),
    )


def _intent(invoice_id: str, amount: str = "100.00") -> PaymentIntent:
    key = idempotency.key(invoice_id, Decimal(amount), ACCOUNT)
    return PaymentIntent(
        id=idempotency.intent_id(key),
        invoice_id=invoice_id,
        amount=Decimal(amount),
        vendor_account=ACCOUNT,
        idempotency_key=key,
    )


def _config(thread: str) -> dict:
    return {"configurable": {"thread_id": thread, "checkpoint_ns": "", "checkpoint_id": "c1"}}


def _counts(db: str, invoice_id: str) -> tuple[int, int, int]:
    with psycopg.connect(db) as con:
        return (
            con.execute(
                "SELECT count(*) FROM payment_intents WHERE invoice_id = %s", (invoice_id,)
            ).fetchone()[0],
            con.execute(
                "SELECT count(*) FROM checkpoint_writes WHERE thread_id = %s", (invoice_id,)
            ).fetchone()[0],
            con.execute(
                "SELECT count(*) FROM audit_log WHERE invoice_id = %s", (invoice_id,)
            ).fetchone()[0],
        )


@pytest.fixture
def saver(db: str, world: World):
    with psycopg.connect(db) as con:
        for i in ("OB-1", "OB-2", "OB-3"):
            _invoice(con, world, i)
    pool = b.pool(db)
    yield b.postgres_saver(pool)
    pool.close()


def test_intent_and_state_commit_together(saver, db: str) -> None:
    saver.put_writes(_config("OB-1"), [(CHANNEL, _intent("OB-1")), ("status", "p")], "t1")
    assert _counts(db, "OB-1") == (1, 2, 1)


def test_failure_before_commit_leaves_neither(
    saver, db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(name: str) -> None:
        if name == "outbox.before_commit":
            raise RuntimeError("killed")

    monkeypatch.setattr(chaos, "point", boom)
    with pytest.raises(RuntimeError):
        saver.put_writes(_config("OB-2"), [(CHANNEL, _intent("OB-2")), ("status", "p")], "t1")
    assert _counts(db, "OB-2") == (0, 0, 0)


def test_rerun_is_success_but_a_different_payment_is_refused(saver, db: str) -> None:
    writes = [(CHANNEL, _intent("OB-3")), ("status", "p")]
    saver.put_writes(_config("OB-3"), writes, "t1")
    saver.put_writes(_config("OB-3"), writes, "t2")
    with pytest.raises(ConflictingIntent):
        saver.put_writes(_config("OB-3"), [(CHANNEL, _intent("OB-3", "99.00"))], "t3")
    intents, writes_saved, _ = _counts(db, "OB-3")
    assert (intents, writes_saved) == (1, 4)
