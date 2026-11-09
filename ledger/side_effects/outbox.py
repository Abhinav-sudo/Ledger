"""The outbox: payment intents are written in the same transaction as the graph state."""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import psycopg
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import ChannelVersions, Checkpoint, CheckpointMetadata
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


def record(cur: psycopg.Cursor[Any], intent: PaymentIntent) -> bool:
    """Insert the intent, or accept the one already there if it is the same payment.

    Returns whether a row was inserted. Raises ConflictingIntent for a different payment.
    """
    inserted = cur.execute(
        INSERT_INTENT,
        (
            intent.id,
            intent.invoice_id,
            intent.amount,
            intent.vendor_account,
            intent.idempotency_key,
        ),
    ).rowcount
    existing = PaymentIntent(**cur.execute(SELECT_INTENT, (intent.invoice_id,)).fetchone())
    if existing.idempotency_key != intent.idempotency_key:
        raise ConflictingIntent(f"{intent.invoice_id} already has intent {existing.id}")
    return inserted == 1


def find(invoice_id: str) -> PaymentIntent | None:
    with psycopg.connect(database_url(), row_factory=dict_row) as con:
        row = con.execute(SELECT_INTENT, (invoice_id,)).fetchone()
    return PaymentIntent(**row) if row else None


_ambient: ContextVar[psycopg.Cursor[Any] | None] = ContextVar("outbox_cursor", default=None)


class OutboxSaver(PostgresSaver):
    """PostgresSaver that commits a payment intent in the same transaction as the state.

    LangGraph persists a node's return value after the node finishes, through put_writes and
    then put, each on its own autocommit connection, so a node cannot join that transaction.
    Both are wrapped here: whichever persists the intent channel first inserts the row in the
    same transaction; the other finds it already there. Overrides PostgresSaver._cursor, so
    the version is pinned and tests/test_outbox.py checks atomicity.
    """

    @contextmanager
    def _cursor(self, *, pipeline: bool = False) -> Iterator[psycopg.Cursor[Any]]:
        cur = _ambient.get()
        if cur is not None:
            yield cur
            return
        with super()._cursor(pipeline=pipeline) as cur:
            yield cur

    @contextmanager
    def _with_intent(self, intent: PaymentIntent) -> Iterator[None]:
        with (
            self.lock,
            _internal.get_connection(self.conn) as conn,
            conn.transaction(),
            conn.cursor(binary=True, row_factory=dict_row) as cur,
        ):
            token = _ambient.set(cur)
            try:
                if record(cur, intent):
                    cur.execute(
                        INSERT_AUDIT,
                        (
                            intent.invoice_id,
                            "payment_intent",
                            "intent.recorded",
                            Jsonb({"key": intent.idempotency_key, "amount": str(intent.amount)}),
                        ),
                    )
                yield
                chaos.point("outbox.before_commit")
            finally:
                _ambient.reset(token)
        chaos.point("outbox.after_commit")

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
        with self._with_intent(intent):
            return super().put_writes(config, writes, task_id, task_path)

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        intent = checkpoint["channel_values"].get(CHANNEL) if CHANNEL in new_versions else None
        if intent is None:
            return super().put(config, checkpoint, metadata, new_versions)
        with self._with_intent(intent):
            return super().put(config, checkpoint, metadata, new_versions)
