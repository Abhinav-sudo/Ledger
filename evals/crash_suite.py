"""SIGKILL the payment pipeline at random points, then count what reached the bank.

Each run gets a fresh database and bank. The API, graph worker, two dispatchers and the
reconciler run as subprocesses with chaos points armed (weighted toward the payment window),
and the supervisor also kills a random one every few seconds. For the middle third of the
run the bank pays and then drops its response. Then chaos stops, everything drains, and the
bank statement is reconciled against the intents and thread states.

    uv run python -m evals.crash_suite --outbox on
"""

import argparse
import json
import os
import random
import shutil
import signal
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import httpx
import psycopg
from langgraph.graph.state import CompiledStateGraph
from psycopg.rows import dict_row

from evals.truth import TruthRouter
from ledger.graph import build
from ledger.settings import database_url
from ledger.workers import reconciler

ROOT = Path(__file__).parents[1]
BANK_PORT, API_PORT = 18081, 18000
TERMINAL = {"paid", "held", "rejected", "credit_note_requested", "needs_human"}
KILLABLE = ("api", "worker", "dispatcher-1", "dispatcher-2", "reconciler")
CHAOS = {
    "api": "api.after_enqueue=0.02",
    "worker": "outbox.before_commit=0.08,outbox.after_commit=0.08,inline.after_bank_call=0.15,"
    "worker.after_claim=0.01,worker.before_done=0.02",
    "dispatcher": "dispatcher.after_claim=0.02,dispatcher.after_send=0.10,"
    "dispatcher.before_commit=0.05",
    "reconciler": "reconciler.after_confirm=0.05",
}
DROP_RATE = 0.3


@dataclass
class Result:
    kills: int
    invoices: int
    payments: int
    duplicates: int
    orphaned: int
    no_intent: int
    stuck: int
    duplicate_invoices_paid: int


class Supervisor:
    def __init__(self, run_dir: Path, env: dict[str, str], rng: random.Random) -> None:
        self.run_dir = run_dir
        self.env = env
        self.rng = rng
        self.procs: dict[str, subprocess.Popen] = {}
        self.kills = 0
        self.chaos = True
        self.drop = 0.0

    def command(self, name: str) -> list[str]:
        py = sys.executable
        if name == "bank":
            return [
                py,
                "-m",
                "uvicorn",
                "world.mock_bank.app:from_env",
                "--factory",
                "--port",
                str(BANK_PORT),
                "--log-level",
                "warning",
            ]
        if name == "api":
            return [
                py,
                "-m",
                "uvicorn",
                "ledger.api.main:app",
                "--port",
                str(API_PORT),
                "--log-level",
                "warning",
            ]
        if name == "worker":
            return [py, "-m", "evals.crash_worker"]
        if name.startswith("dispatcher"):
            return [py, "-m", "ledger.side_effects.dispatcher"]
        return [py, "-m", "ledger.workers.reconciler"]

    def start(self, name: str) -> None:
        env = {**self.env, "BANK_DROP_RATE": str(self.drop)}
        role = name.split("-")[0]
        if self.chaos and role in CHAOS:
            env["LEDGER_CHAOS"] = CHAOS[role]
        log = (self.run_dir / f"{name}.log").open("a")
        self.procs[name] = subprocess.Popen(
            self.command(name), env=env, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT
        )

    def start_all(self) -> None:
        for name in ("bank", *KILLABLE):
            self.start(name)

    def reap(self) -> None:
        for name, proc in list(self.procs.items()):
            code = proc.poll()
            if code is None:
                continue
            if code == -signal.SIGKILL:
                self.kills += 1
            self.start(name)

    def kill_random(self) -> None:
        self.procs[self.rng.choice(KILLABLE)].send_signal(signal.SIGKILL)

    def restart(self, names: tuple[str, ...]) -> None:
        for name in names:
            self.procs[name].terminate()
        for name in names:
            self.procs[name].wait()
            self.start(name)

    def stop(self) -> None:
        for proc in self.procs.values():
            proc.terminate()
        for proc in self.procs.values():
            proc.wait()


def _pick(labels: dict[str, dict], run: int, n: int) -> list[str]:
    """A slice of the world, plus the originals its duplicates copy."""
    ids = list(labels)[run * n : (run + 1) * n]
    originals = {
        labels[i]["related_invoice"] for i in ids if labels[i]["exception_type"] == "duplicate"
    }
    return sorted(set(ids) | originals)


def _create_db(name: str, world: Path) -> str:
    admin = database_url()
    with psycopg.connect(admin, autocommit=True) as con:
        con.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        con.execute(f"CREATE DATABASE {name}")
    url = psycopg.conninfo.make_conninfo(admin, dbname=name)
    with psycopg.connect(url) as con:
        con.execute((ROOT / "world" / "schema.sql").read_text())
        con.execute((world / "erp.sql").read_text())
    pool = build.pool(url)
    build.postgres_saver(pool)
    pool.close()
    return url


def _wait(client: httpx.Client, path: str) -> None:
    for _ in range(150):
        try:
            if client.get(path).status_code < 500:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.2)
    raise TimeoutError(f"{client.base_url}{path} never came up")


def _post(client: httpx.Client, world: Path, invoice_id: str) -> bool:
    files = {"pdf": (world / "pdfs" / f"{invoice_id}.pdf").read_bytes()}
    eml = world / "emails" / f"{invoice_id}.eml"
    if eml.exists():
        files["email"] = eml.read_bytes()
    try:
        resp = client.post("/invoices", files=files, data={"invoice_id": invoice_id})
    except httpx.TransportError:
        return False
    return resp.status_code == 202


def _approve_waiting(client: httpx.Client, ids: list[str]) -> int:
    waiting = 0
    for invoice_id in ids:
        try:
            state = client.get(f"/invoices/{invoice_id}")
            if state.status_code == 200 and state.json()["next"] == ["approve"]:
                waiting += 1
                client.post(f"/invoices/{invoice_id}/approve", json={"by": "treasury@suite"})
        except httpx.TransportError:
            waiting += 1
    return waiting


def _statuses(graph: CompiledStateGraph, ids: list[str]) -> dict[str, str | None]:
    return {i: graph.get_state(build.thread(i)).values.get("status") for i in ids}


def run_once(args: argparse.Namespace, run: int, labels: dict[str, dict]) -> Result:
    rng = random.Random(args.seed * 1000 + run)
    ids = _pick(labels, run, args.invoices)
    bank_change = [i for i in ids if labels[i]["exception_type"] == "bank_change"]
    run_dir = ROOT / "out" / "crash" / f"{args.outbox}-{run}"
    shutil.rmtree(run_dir, ignore_errors=True)
    run_dir.mkdir(parents=True)
    url = _create_db(f"ledger_crash_{args.outbox.replace('-', '_')}_{run}", args.world)
    env = {
        "PATH": os.environ["PATH"],
        "PYTHONUNBUFFERED": "1",
        "LEDGER_DATABASE_URL": url,
        "LEDGER_OUTBOX": args.outbox,
        "LEDGER_INBOX": str(run_dir / "inbox"),
        "LEDGER_JOB_LEASE_S": "5",
        "BANK_URL": f"http://127.0.0.1:{BANK_PORT}",
        "BANK_DB": str(run_dir / "bank.db"),
    }
    sup = Supervisor(run_dir, env, rng)
    sup.start_all()
    api = httpx.Client(base_url=f"http://127.0.0.1:{API_PORT}", timeout=10)
    bank = httpx.Client(base_url=f"http://127.0.0.1:{BANK_PORT}", timeout=10)
    pool = build.pool(url)
    graph = build.build(build.postgres_saver(pool), TruthRouter(None))
    db = psycopg.connect(url, autocommit=True, row_factory=dict_row)
    try:
        next_kill = time.monotonic() + rng.uniform(2, 6)
        queue = list(ids)
        deadline = time.monotonic() + args.chaos_seconds
        while time.monotonic() < deadline:
            sup.reap()
            if time.monotonic() >= next_kill:
                sup.kill_random()
                next_kill = time.monotonic() + rng.uniform(2, 6)
            while queue and _post(api, args.world, queue[0]):
                queue.pop(0)
            done = db.execute(
                "SELECT count(*) AS n FROM graph_jobs WHERE kind = 'run' AND status = 'done'"
            ).fetchone()["n"]
            drop = DROP_RATE if len(ids) / 3 <= done < 2 * len(ids) / 3 else 0.0
            if drop != sup.drop:
                sup.drop = drop
                sup.restart(("bank",))
            waiting = _approve_waiting(api, bank_change)
            if not queue and done == len(ids) and not waiting:
                break
            time.sleep(0.2)

        sup.chaos, sup.drop = False, 0.0
        sup.reap()
        sup.restart(("bank", *KILLABLE))
        _wait(bank, "/statement")
        _wait(api, "/docs")
        drain_until = time.monotonic() + args.drain_seconds
        while time.monotonic() < drain_until:
            sup.reap()
            while queue and _post(api, args.world, queue[0]):
                queue.pop(0)
            _approve_waiting(api, bank_change)
            open_intents = db.execute(
                "SELECT count(*) AS n FROM payment_intents WHERE status IN ('pending', 'sent')"
            ).fetchone()["n"]
            db.execute(
                "UPDATE payment_intents SET next_attempt_at = now() WHERE status = 'pending'"
            )
            statuses = _statuses(graph, ids)
            if not queue and not open_intents and set(statuses.values()) <= TERMINAL:
                break
            time.sleep(1)

        _wait(bank, "/statement")
        payments = bank.get("/statement").json()["payments"]
        report = reconciler.reconcile_once(db, bank)
        statuses = _statuses(graph, ids)
    finally:
        sup.stop()
        db.close()
        pool.close()

    per_invoice = Counter(p["invoice_id"] for p in payments)
    stuck = {i: s for i, s in statuses.items() if s not in TERMINAL}
    paid_duplicates = [i for i in per_invoice if labels[i]["exception_type"] == "duplicate"]
    (run_dir / "result.json").write_text(
        json.dumps({"stuck": stuck, "unpaid": report.unpaid, "paid_twice": report.paid_twice})
    )
    return Result(
        kills=sup.kills,
        invoices=len(ids),
        payments=len(payments),
        duplicates=sum(n - 1 for n in per_invoice.values()),
        orphaned=len(report.unpaid),
        no_intent=len(report.unknown_payments),
        stuck=len(stuck),
        duplicate_invoices_paid=len(paid_duplicates),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--outbox", choices=("on", "off", "off-keyed"), default="on")
    parser.add_argument("--runs", type=int, default=4)
    parser.add_argument("--invoices", type=int, default=60, help="per run, before originals")
    parser.add_argument("--world", type=Path, default=ROOT / "out" / "seed-42")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--chaos-seconds", type=float, default=240)
    parser.add_argument("--drain-seconds", type=float, default=180)
    args = parser.parse_args()
    rows = (json.loads(line) for line in (args.world / "labels.jsonl").open())
    labels = {lb["invoice_id"]: lb for lb in rows}

    results = []
    for run in range(args.runs):
        r = run_once(args, run, labels)
        results.append(r)
        print(f"run {run + 1}: {r}", flush=True)

    cols = (
        "run",
        "kills",
        "invoices",
        "bank payments",
        "duplicate payments",
        "orphaned intents",
        "payments with no intent",
        "stuck threads",
        "duplicate-labelled invoices paid",
    )
    print(f"\nLEDGER_OUTBOX={args.outbox}\n")
    print("| " + " | ".join(cols) + " |")
    print("|" + "---|" * len(cols))
    for i, r in enumerate(results, start=1):
        print(
            f"| {i} | {r.kills} | {r.invoices} | {r.payments} | {r.duplicates} | {r.orphaned} "
            f"| {r.no_intent} | {r.stuck} | {r.duplicate_invoices_paid} |"
        )
    total = Result(*(sum(getattr(r, f) for r in results) for f in Result.__dataclass_fields__))
    print(
        f"| **all {len(results)}** | {total.kills} | {total.invoices} | {total.payments} "
        f"| {total.duplicates} | {total.orphaned} | {total.no_intent} | {total.stuck} "
        f"| {total.duplicate_invoices_paid} |"
    )


if __name__ == "__main__":
    main()
