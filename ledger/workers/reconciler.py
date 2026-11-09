"""Matches the bank statement to payment intents. Run as: python -m ledger.workers.reconciler"""

import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import httpx
import psycopg
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command
from psycopg.rows import dict_row

from ledger import chaos
from ledger.graph import build
from ledger.llm.router import ModelRouter
from ledger.settings import bank_url, database_url

log = logging.getLogger("ledger.reconciler")

INTENTS = "SELECT invoice_id, idempotency_key, status FROM payment_intents"
CONFIRM = """
UPDATE payment_intents
SET status = 'confirmed', confirmed_at = now(), bank_payment_id = %s
WHERE idempotency_key = %s AND status <> 'confirmed'
"""
CONFIRMED = "SELECT invoice_id, bank_payment_id FROM payment_intents WHERE status = 'confirmed'"


@dataclass
class Report:
    confirmed: int = 0
    unpaid: dict[str, str] = field(default_factory=dict)
    unknown_payments: list[str] = field(default_factory=list)
    paid_twice: dict[str, int] = field(default_factory=dict)


def reconcile_once(con: psycopg.Connection[Any], client: httpx.Client) -> Report:
    """Confirm intents the bank has paid, and flag everything that does not line up.

    unpaid holds intents with no bank payment yet, keyed by invoice, with their status.
    """
    payments = client.get("/statement").raise_for_status().json()["payments"]
    by_key = {p["idempotency_key"]: p for p in payments}
    intents = con.execute(INTENTS).fetchall()
    report = Report()
    for intent in intents:
        payment = by_key.get(intent["idempotency_key"])
        if payment is None:
            report.unpaid[intent["invoice_id"]] = intent["status"]
        elif intent["status"] != "confirmed":
            report.confirmed += con.execute(
                CONFIRM, (payment["payment_id"], intent["idempotency_key"])
            ).rowcount
    known = {i["idempotency_key"] for i in intents}
    report.unknown_payments = sorted(
        p["payment_id"] for p in payments if p["idempotency_key"] not in known
    )
    per_invoice = Counter(p["invoice_id"] for p in payments)
    report.paid_twice = {inv: n for inv, n in per_invoice.items() if n > 1}
    return report


def resume_confirmed(con: psycopg.Connection[Any], graph: CompiledStateGraph) -> list[str]:
    """Resume every thread still waiting at reconcile whose intent is confirmed."""
    # ponytail: rescans every confirmed intent each pass; track a resumed flag once this is slow.
    resumed = []
    for row in con.execute(CONFIRMED).fetchall():
        config = build.thread(row["invoice_id"])
        if graph.get_state(config).next == ("reconcile",):
            graph.invoke(
                Command(resume={"bank_payment_id": row["bank_payment_id"]}),
                config,
                durability="sync",
            )
            resumed.append(row["invoice_id"])
    return resumed


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    pool = build.pool(database_url())
    graph = build.build(build.postgres_saver(pool), ModelRouter.from_config())
    with (
        psycopg.connect(database_url(), autocommit=True, row_factory=dict_row) as con,
        httpx.Client(base_url=bank_url(), timeout=10) as client,
    ):
        while True:
            report = reconcile_once(con, client)
            chaos.point("reconciler.after_confirm")
            resumed = resume_confirmed(con, graph)
            if report.unknown_payments or report.paid_twice:
                log.error(
                    "payments with no intent %s, paid twice %s",
                    report.unknown_payments,
                    report.paid_twice,
                )
            if report.confirmed or resumed:
                log.info("confirmed %d, resumed %d", report.confirmed, len(resumed))
            time.sleep(0.5)


if __name__ == "__main__":
    main()
