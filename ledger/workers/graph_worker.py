"""Runs invoice threads from graph_jobs. Run as: python -m ledger.workers.graph_worker"""

import logging
import os
import time
from collections.abc import Callable
from typing import Any

import psycopg
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ledger import chaos
from ledger.graph import build
from ledger.llm.router import ModelRouter
from ledger.settings import database_url

log = logging.getLogger("ledger.worker")

# Must outlast the slowest job, or a live worker's invoice gets claimed a second time.
LEASE_S = int(os.environ.get("LEDGER_JOB_LEASE_S", "300"))
MAX_ATTEMPTS = 5

ENQUEUE = """
INSERT INTO graph_jobs (invoice_id, kind, payload) VALUES (%s, %s, %s)
ON CONFLICT (invoice_id, kind) DO NOTHING
RETURNING id
"""
# A job whose worker died is claimed again once its lease runs out.
CLAIM = """
UPDATE graph_jobs
SET lease_until = now() + make_interval(secs => %s), attempts = attempts + 1
WHERE id = (
    SELECT id FROM graph_jobs
    WHERE status = 'queued' AND attempts < %s AND (lease_until IS NULL OR lease_until < now())
    ORDER BY id
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
RETURNING id, invoice_id, kind, payload
"""
DONE = "UPDATE graph_jobs SET status = 'done' WHERE id = %s"


def enqueue(con: psycopg.Connection[Any], invoice_id: str, kind: str, payload: dict) -> bool:
    return con.execute(ENQUEUE, (invoice_id, kind, Jsonb(payload))).fetchone() is not None


def _input(graph: CompiledStateGraph, job: dict[str, Any]) -> tuple[bool, Any]:
    """What to invoke the thread with, if anything, given where its checkpoint stands."""
    snapshot = graph.get_state(build.thread(job["invoice_id"]))
    if not snapshot.values:
        return True, {"invoice_id": job["invoice_id"], **job["payload"]}
    if job["kind"] == "resume" and snapshot.next == ("approve",):
        return True, Command(resume=job["payload"])
    # A task can be done (its writes committed) while the checkpoint has not moved past it,
    # so next is empty; invoking with None applies those writes and carries on.
    if snapshot.tasks and not snapshot.interrupts:
        return True, None
    return False, None


def run_once(con: psycopg.Connection[Any], graph_for: Callable[[str], CompiledStateGraph]) -> bool:
    job = con.execute(CLAIM, (LEASE_S, MAX_ATTEMPTS)).fetchone()
    if job is None:
        return False
    chaos.point("worker.after_claim")
    graph = graph_for(job["invoice_id"])
    run, value = _input(graph, job)
    if run:
        graph.invoke(value, build.thread(job["invoice_id"]), durability="sync")
    chaos.point("worker.before_done")
    con.execute(DONE, (job["id"],))
    return True


def main(router_for: Callable[[str], Any] | None = None) -> None:
    """router_for builds a router per invoice; the crash suite passes a ground-truth stub."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    saver = build.postgres_saver(build.pool(database_url()))
    shared = build.build(saver, ModelRouter.from_config()) if router_for is None else None

    def graph_for(invoice_id: str) -> CompiledStateGraph:
        return shared or build.build(saver, router_for(invoice_id))

    with psycopg.connect(database_url(), autocommit=True, row_factory=dict_row) as con:
        while True:
            try:
                busy = run_once(con, graph_for)
            except Exception:
                # The job stays queued and is retried when its lease expires.
                log.exception("graph job failed")
                busy = False
            if not busy:
                time.sleep(0.2)


if __name__ == "__main__":
    main()
