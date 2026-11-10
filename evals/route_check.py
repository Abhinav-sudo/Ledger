"""Run a fixed 20-invoice slice of the dev world through the graph and score routing."""

import argparse
import json
import logging
from collections import Counter, defaultdict
from pathlib import Path

import psycopg

from ledger.api.main import email_text
from ledger.graph import build
from ledger.llm.router import AllModelsFailed, ModelRouter
from ledger.settings import database_url

WORLD_TABLES = (
    "audit_log, payment_intents, invoice_lines, invoices, goods_receipts, po_lines, "
    "purchase_orders, vendor_bank_accounts, vendors"
)


class Calls(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[dict] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record.llm)


def pick(labels: list[dict]) -> list[str]:
    """Every exception type, every hard sub-case, and the originals the duplicates copy."""

    def first(kind: str, note: str = "", n: int = 1) -> list[dict]:
        return [lb for lb in labels if lb["exception_type"] == kind and note in lb["notes"]][:n]

    dups = first("duplicate", n=2)
    chosen = [
        *dups,
        *first("clean", "legitimate repeat"),
        *first("clean", "changed account"),
        *first("price_variance", "exactly at"),
        *first("price_variance", "within"),
        *first("price_variance", "beyond"),
        *first("qty_variance", n=2),
        *first("missing_po", "no PO reference"),
        *first("missing_po", "does not exist"),
        *first("missing_po", "is closed"),
        *first("bank_change", "remit-to"),
        *first("bank_change", "prompt-injection"),
        *first("credit_note", n=2),
    ]
    ids = {lb["invoice_id"] for lb in chosen} | {d["related_invoice"] for d in dups}
    for lb in first("clean", n=len(labels)):
        if len(ids) == 20:
            break
        if not lb["related_invoice"] and "changed account" not in lb["notes"]:
            ids.add(lb["invoice_id"])
    return sorted(ids)


def reset(world: Path, ids: list[str], saver: build.OutboxSaver) -> None:
    with psycopg.connect(database_url()) as con:
        con.execute(f"TRUNCATE {WORLD_TABLES} CASCADE")
        con.execute((world / "erp.sql").read_text())
    for invoice_id in ids:
        saver.delete_thread(invoice_id)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--world", type=Path, default=Path("out/seed-42"))
    args = parser.parse_args()
    labels = [json.loads(line) for line in (args.world / "labels.jsonl").open()]
    by_id = {lb["invoice_id"]: lb for lb in labels}
    ids = pick(labels)

    calls = Calls()
    logging.getLogger("ledger.llm").addHandler(calls)
    logging.getLogger("ledger.llm").setLevel(logging.INFO)

    pool = build.pool(database_url())
    saver = build.postgres_saver(pool)
    reset(args.world, ids, saver)
    graph = build.build(saver, ModelRouter.from_config())

    score: dict[str, Counter[str]] = defaultdict(Counter)
    for invoice_id in ids:
        lb = by_id[invoice_id]
        eml = args.world / "emails" / f"{invoice_id}.eml"
        state = {
            "invoice_id": invoice_id,
            "pdf_path": str(args.world / "pdfs" / f"{invoice_id}.pdf"),
            "email": email_text(eml.read_bytes()) if eml.exists() else None,
        }
        try:
            out = graph.invoke(state, build.thread(invoice_id))
        except AllModelsFailed as e:
            out = {"status": f"failed: {e}"}
        routed = out["match"].exceptions if out.get("match") else None
        expected = [] if lb["exception_type"] == "clean" else [lb["exception_type"]]
        ok = routed == expected
        score[lb["exception_type"]]["ok" if ok else "wrong"] += 1
        waiting = graph.get_state(build.thread(invoice_id)).next
        print(
            f"{invoice_id}  {lb['exception_type']:<15} routed={routed}  status={out['status']}"
            f"{'  WAITING ' + str(waiting) if waiting else ''}{'' if ok else '  <-- MISROUTED'}"
        )
        if not ok and out.get("match"):
            print(f"    reasons: {out['match'].reasons}")
    pool.close()

    print("\nrouting accuracy by labelled exception type")
    for kind, c in sorted(score.items()):
        print(f"  {kind:<15} {c['ok']}/{c['ok'] + c['wrong']}")
    total = sum(c["ok"] for c in score.values())
    print(f"  {'all':<15} {total}/{len(ids)}")

    print("\nmodel calls (provider/model: served, failures)")
    usage: dict[str, Counter[str]] = defaultdict(Counter)
    for r in calls.records:
        key = f"{r['provider']}/{r['model']}"
        usage[key][r["fallback_reason"] or "served"] += 1
        usage[key]["tokens"] += (r["prompt_tokens"] or 0) + (r["completion_tokens"] or 0)
    for key, c in sorted(usage.items()):
        print(f"  {key}: {dict(c)}")


if __name__ == "__main__":
    main()
