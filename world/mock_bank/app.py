import hashlib
import os
import random
import sqlite3
import time
import uuid
from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

SCHEMA = """
CREATE TABLE IF NOT EXISTS payments (
    idempotency_key TEXT PRIMARY KEY,
    request_hash    TEXT NOT NULL,
    payment_id      TEXT NOT NULL,
    invoice_id      TEXT NOT NULL,
    amount          TEXT NOT NULL,
    account         TEXT NOT NULL,
    paid_at         TEXT NOT NULL
)
"""
COLUMNS = "idempotency_key, payment_id, invoice_id, amount, account, paid_at"


class PaymentRequest(BaseModel):
    invoice_id: str
    amount: Decimal = Field(gt=0, decimal_places=2)
    account: str


def create_app(
    db_path: Path,
    fail_rate: float = 0.0,
    timeout_rate: float = 0.0,
    timeout_s: float = 30.0,
    drop_rate: float = 0.0,
    seed: int | None = None,
) -> FastAPI:
    """Mock bank. Exactly-once rests on the PRIMARY KEY, not on application checks."""
    rng = random.Random(seed)
    with closing(sqlite3.connect(db_path)) as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute(SCHEMA)

    def connect() -> sqlite3.Connection:
        con = sqlite3.connect(db_path, timeout=30, isolation_level=None)
        con.row_factory = sqlite3.Row
        return con

    app = FastAPI(title="mock bank")

    @app.post("/payments")
    def pay(
        body: PaymentRequest, idempotency_key: str | None = Header(default=None)
    ) -> JSONResponse:
        if not idempotency_key:
            raise HTTPException(400, "Idempotency-Key header is required")
        if rng.random() < fail_rate:
            raise HTTPException(500, "injected failure before payment")
        if rng.random() < timeout_rate:
            time.sleep(timeout_s)

        request_hash = hashlib.sha256(
            f"{body.invoice_id}|{body.amount:.2f}|{body.account}".encode()
        ).hexdigest()
        with closing(connect()) as con:
            inserted = con.execute(
                f"INSERT INTO payments (request_hash, {COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (idempotency_key) DO NOTHING",
                (
                    request_hash,
                    idempotency_key,
                    str(uuid.uuid4()),
                    body.invoice_id,
                    f"{body.amount:.2f}",
                    body.account,
                    datetime.now(UTC).isoformat(),
                ),
            ).rowcount
            row = con.execute(
                f"SELECT request_hash, {COLUMNS} FROM payments WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()

        if row["request_hash"] != request_hash:
            raise HTTPException(422, "Idempotency-Key was already used for a different request")
        if rng.random() < drop_rate:
            # The money has moved but the caller never learns it: the window the outbox exists for.
            raise HTTPException(503, "injected failure after payment")
        payment = dict(row)
        del payment["request_hash"]
        return JSONResponse(payment, status_code=201 if inserted else 200)

    @app.get("/statement")
    def statement() -> dict[str, list[dict[str, str]]]:
        with closing(connect()) as con:
            rows = con.execute(f"SELECT {COLUMNS} FROM payments ORDER BY rowid").fetchall()
        return {"payments": [dict(r) for r in rows]}

    return app


def from_env() -> FastAPI:
    env = os.environ
    return create_app(
        Path(env.get("BANK_DB", "bank.db")),
        fail_rate=float(env.get("BANK_FAIL_RATE", "0")),
        timeout_rate=float(env.get("BANK_TIMEOUT_RATE", "0")),
        timeout_s=float(env.get("BANK_TIMEOUT_S", "30")),
        drop_rate=float(env.get("BANK_DROP_RATE", "0")),
        seed=int(env["BANK_SEED"]) if "BANK_SEED" in env else None,
    )
