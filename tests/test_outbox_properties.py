import tempfile
from decimal import Decimal
from pathlib import Path

import psycopg
from helpers import ACCOUNT, bank
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from psycopg.rows import dict_row

from ledger.contracts.payment import PaymentIntent
from ledger.side_effects import dispatcher, idempotency, outbox
from ledger.workers import reconciler
from world.generator import World

INVOICES = ("P-0", "P-1")
AMOUNTS = {"P-0": "100.00", "P-1": "200.00"}

node = st.tuples(st.just("node"), st.sampled_from(INVOICES), st.booleans())
conflict = st.tuples(st.just("conflict"), st.sampled_from(INVOICES))
step = st.tuples(st.sampled_from(["claim", "settle", "crash"]), st.integers(0, 1))
send = st.tuples(st.just("send"), st.integers(0, 1), st.booleans())
ops = st.lists(st.one_of(node, conflict, step, send), max_size=40)


def _intent(invoice_id: str, amount: str) -> PaymentIntent:
    key = idempotency.key(invoice_id, Decimal(amount), ACCOUNT)
    return PaymentIntent(
        id=idempotency.intent_id(key),
        invoice_id=invoice_id,
        amount=Decimal(amount),
        vendor_account=ACCOUNT,
        idempotency_key=key,
    )


def _reset(db: str, vendor_id: str) -> None:
    with psycopg.connect(db) as con:
        con.execute("TRUNCATE payment_intents, invoice_lines, invoices CASCADE")
        for invoice_id in INVOICES:
            con.execute(
                "INSERT INTO invoices (id, vendor_id, invoice_no, invoice_date, currency, "
                "remit_to_account, total) VALUES (%s, %s, %s, '2026-01-05', 'USD', %s, 1)",
                (invoice_id, vendor_id, invoice_id, ACCOUNT),
            )


def _rerun(db: str, intent: PaymentIntent, commit: bool) -> None:
    """A payment node re-execution: commits, or dies before its transaction commits."""
    with psycopg.connect(db, row_factory=dict_row) as con:
        try:
            outbox.record(con.cursor(), intent)
        except outbox.ConflictingIntent:
            con.rollback()
            return
        if commit:
            con.commit()
        else:
            con.rollback()


@settings(
    max_examples=75,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(ops)
def test_at_most_one_payment_per_key_under_any_interleaving(
    db: str, world: World, ops: list[tuple]
) -> None:
    _reset(db, world.vendors[0].id)
    with tempfile.TemporaryDirectory() as tmp:
        client = bank(Path(tmp) / "bank.db")
        dispatchers = [psycopg.connect(db, row_factory=dict_row) for _ in range(2)]
        held: list[dict | None] = [None, None]
        outcomes: list[dispatcher.Outcome | None] = [None, None]
        try:
            for op in ops:
                kind, arg = op[0], op[1]
                if kind == "node":
                    _rerun(db, _intent(arg, AMOUNTS[arg]), commit=op[2])
                elif kind == "conflict":
                    _rerun(db, _intent(arg, "150.00"), commit=True)
                elif kind == "claim" and held[arg] is None:
                    held[arg] = dispatcher.claim(dispatchers[arg])
                    if held[arg] is None:
                        dispatchers[arg].rollback()
                elif kind == "send" and held[arg] and outcomes[arg] is None:
                    outcome = dispatcher.send(client, held[arg])
                    lost = dispatcher.Outcome(None, "response lost", retryable=True)
                    outcomes[arg] = lost if op[2] else outcome
                elif kind == "settle" and held[arg] and outcomes[arg]:
                    dispatcher.settle(dispatchers[arg], held[arg], outcomes[arg])
                    dispatchers[arg].commit()
                    held[arg], outcomes[arg] = None, None
                elif kind == "crash":
                    dispatchers[arg].rollback()
                    held[arg], outcomes[arg] = None, None
        finally:
            for con in dispatchers:
                con.rollback()
                con.close()

        with psycopg.connect(db, autocommit=True, row_factory=dict_row) as con:
            for _ in range(dispatcher.MAX_ATTEMPTS * 2):
                con.execute("UPDATE payment_intents SET next_attempt_at = now()")
                if not dispatcher.run_once(con, client):
                    break
            report = reconciler.reconcile_once(con, client)
            intents = con.execute("SELECT invoice_id, status FROM payment_intents").fetchall()

        payments = client.get("/statement").json()["payments"]
        keys = [p["idempotency_key"] for p in payments]
        assert len(keys) == len(set(keys))
        assert report.paid_twice == {}
        assert report.unknown_payments == []
        assert all(status == "failed" for status in report.unpaid.values())
        assert len({i["invoice_id"] for i in intents}) == len(intents)
