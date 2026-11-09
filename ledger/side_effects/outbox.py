"""The outbox: payment intents are written in the same transaction as the graph state."""

from collections.abc import Sequence
from typing import Any

import psycopg
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres import PostgresSaver, _internal
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ledger import chaos
from ledger.contracts.payment import PaymentIntent
from ledger.settings import database_url

CHANNEL = "intent"

INSERT_INTENT = """
INSERT INTO payment_intents (id, invoice_id, amount, vendor_account, idempotency_key, status)
VALUES (%s, %s, %s, %s, %s, 'pending')
ON CONFLICT DO NOTHING
"""
SELECT_INTENT = """
SELECT id, invoice_id, amount, vendor_account, idempotency_key
FROM payment_intents WHERE invoice_id = %s
"""
INSERT_AUDIT = "INSERT INTO audit_log (invoice_id, actor, event, detail) VALUES (%s, %s, %s, %s)"


class ConflictingIntent(Exception):
    pass


def record(cur: psycopg.Cursor[Any], intent: PaymentIntent) -> PaymentIntent:
    """Insert the intent, or accept the one already there if it is the same payment."""
    cur.execute(
        INSERT_INTENT,
        (
            intent.id,
            intent.invoice_id,
            intent.amount,
            intent.vendor_account,
            intent.idempotency_key,
        ),
    )
    existing = PaymentIntent(**cur.execute(SELECT_INTENT, (intent.invoice_id,)).fetchone())
    if existing.idempotency_key != intent.idempotency_key:
        raise ConflictingIntent(f"{intent.invoice_id} already has intent {existing.id}")
    return existing


def find(invoice_id: str) -> PaymentIntent | None:
    with psycopg.connect(database_url(), row_factory=dict_row) as con:
        row = con.execute(SELECT_INTENT, (invoice_id,)).fetchone()
    return PaymentIntent(**row) if row else None


class OutboxSaver(PostgresSaver):
    """PostgresSaver whose node writes and payment intent commit in one transaction.

    LangGraph persists a node's return value through put_writes after the node finishes, on
    an autocommit connection, so a node cannot join that transaction itself. Relies on
    PostgresSaver internals; the version is pinned and tests/test_outbox.py checks atomicity.
    """

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        intent = next((v for c, v in writes if c == CHANNEL and v is not None), None)
        if intent is None:
            return super().put_writes(config, writes, task_id, task_path)
        conf = config["configurable"]
        rows = self._dump_writes(
            conf["thread_id"],
            conf["checkpoint_ns"],
            conf["checkpoint_id"],
            task_id,
            task_path,
            writes,
        )
        with (
            self.lock,
            _internal.get_connection(self.conn) as conn,
            conn.transaction(),
            conn.cursor(binary=True, row_factory=dict_row) as cur,
        ):
            record(cur, intent)
            cur.execute(
                INSERT_AUDIT,
                (
                    intent.invoice_id,
                    "payment_intent",
                    "intent.recorded",
                    Jsonb({"key": intent.idempotency_key, "amount": str(intent.amount)}),
                ),
            )
            cur.executemany(self.INSERT_CHECKPOINT_WRITES_SQL, rows)
            chaos.point("outbox.before_commit")
        chaos.point("outbox.after_commit")
        return None
