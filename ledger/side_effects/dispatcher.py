"""The only code that calls the bank. Run as: python -m ledger.side_effects.dispatcher"""

import logging
import random
import time
from typing import Any, NamedTuple

import httpx
import psycopg
from psycopg.rows import dict_row

from ledger import chaos
from ledger.settings import bank_url, database_url

log = logging.getLogger("ledger.dispatcher")

MAX_ATTEMPTS = 8
BASE_DELAY_S = 0.5
MAX_DELAY_S = 30.0

CLAIM = """
SELECT id, invoice_id, amount, vendor_account, idempotency_key, attempts
FROM payment_intents
WHERE status = 'pending' AND next_attempt_at <= now()
ORDER BY next_attempt_at
FOR UPDATE SKIP LOCKED
LIMIT 1
"""
MARK_SENT = """
UPDATE payment_intents
SET status = 'sent', sent_at = now(), bank_payment_id = %s, last_error = NULL
WHERE id = %s AND status = 'pending'
"""
RETRY = """
UPDATE payment_intents
SET attempts = attempts + 1, next_attempt_at = now() + make_interval(secs => %s),
    last_error = %s
WHERE id = %s
"""
DEAD_LETTER = """
UPDATE payment_intents SET status = 'failed', attempts = attempts + 1, last_error = %s
WHERE id = %s
"""


class Outcome(NamedTuple):
    payment_id: str | None
    error: str | None
    retryable: bool


def backoff(attempts: int) -> float:
    return min(MAX_DELAY_S, BASE_DELAY_S * 2**attempts) * random.uniform(0.5, 1.0)


def claim(con: psycopg.Connection[Any]) -> dict[str, Any] | None:
    return con.execute(CLAIM).fetchone()


def send(client: httpx.Client, row: dict[str, Any]) -> Outcome:
    try:
        resp = client.post(
            "/payments",
            json={
                "invoice_id": row["invoice_id"],
                "amount": f"{row['amount']:.2f}",
                "account": row["vendor_account"],
            },
            headers={"Idempotency-Key": row["idempotency_key"]},
        )
    except httpx.TransportError as e:
        return Outcome(None, type(e).__name__, retryable=True)
    if resp.is_success:
        return Outcome(resp.json()["payment_id"], None, retryable=False)
    return Outcome(None, f"http {resp.status_code}: {resp.text[:200]}", resp.status_code >= 500)


def settle(con: psycopg.Connection[Any], row: dict[str, Any], outcome: Outcome) -> None:
    if outcome.payment_id:
        con.execute(MARK_SENT, (outcome.payment_id, row["id"]))
    elif outcome.retryable and row["attempts"] + 1 < MAX_ATTEMPTS:
        con.execute(RETRY, (backoff(row["attempts"]), outcome.error, row["id"]))
    else:
        con.execute(DEAD_LETTER, (outcome.error, row["id"]))
        log.warning("intent %s dead-lettered: %s", row["id"], outcome.error)


def run_once(con: psycopg.Connection[Any], client: httpx.Client) -> bool:
    """Claim, send and settle one due intent in one transaction. False when none is due.

    The row lock is held across the bank call, so a crash anywhere before commit leaves the
    intent pending and unlocked, and the retry reuses the same Idempotency-Key.
    """
    with con.transaction():
        row = claim(con)
        if row is None:
            return False
        chaos.point("dispatcher.after_claim")
        outcome = send(client, row)
        chaos.point("dispatcher.after_send")
        settle(con, row, outcome)
        chaos.point("dispatcher.before_commit")
    return True


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    with (
        psycopg.connect(database_url(), autocommit=True, row_factory=dict_row) as con,
        httpx.Client(base_url=bank_url(), timeout=10) as client,
    ):
        log.info("dispatching to %s", bank_url())
        while True:
            if not run_once(con, client):
                time.sleep(0.2)


if __name__ == "__main__":
    main()
