import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from email import message_from_bytes, policy
from typing import Annotated, Any

import psycopg
from fastapi import FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from pydantic_core import to_jsonable_python

from ledger import chaos
from ledger.graph import build
from ledger.llm.router import ModelRouter
from ledger.settings import database_url, inbox
from ledger.workers import graph_worker


class Decision(BaseModel):
    by: str
    note: str | None = None


def email_text(raw: bytes) -> str:
    msg = message_from_bytes(raw, policy=policy.default)
    body = msg.get_body(preferencelist=("plain",))
    return f"Subject: {msg['subject']}\n\n{body.get_content() if body else ''}"


def _view(snapshot: Any) -> JSONResponse:
    values = dict(snapshot.values)
    audit = values.pop("audit", [])
    return JSONResponse(
        to_jsonable_python(
            {
                "thread_id": values.get("invoice_id"),
                "status": values.get("status"),
                "next": list(snapshot.next),
                "interrupt": snapshot.interrupts[0].value if snapshot.interrupts else None,
                "state": values,
                "audit": audit,
            }
        )
    )


def create_app(router: ModelRouter | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        pool = build.pool(database_url())
        app.state.graph = build.build(
            build.postgres_saver(pool), router or ModelRouter.from_config()
        )
        yield
        pool.close()

    app = FastAPI(title="ledger", lifespan=lifespan)

    @app.post("/invoices", status_code=202)
    def ingest(
        pdf: UploadFile,
        email: UploadFile | None = None,
        invoice_id: Annotated[str | None, Form(pattern=r"^[A-Za-z0-9_-]{1,64}$")] = None,
    ) -> dict[str, str]:
        data = pdf.file.read()
        invoice_id = invoice_id or f"INV-{hashlib.sha256(data).hexdigest()[:16]}"
        path = inbox() / f"{invoice_id}.pdf"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        payload = {
            "pdf_path": str(path),
            "email": email_text(email.file.read()) if email else None,
        }
        with psycopg.connect(database_url(), autocommit=True) as con:
            graph_worker.enqueue(con, invoice_id, "run", payload)
        chaos.point("api.after_enqueue")
        return {"thread_id": invoice_id}

    @app.get("/invoices/{invoice_id}")
    def show(invoice_id: str) -> JSONResponse:
        snapshot = app.state.graph.get_state(build.thread(invoice_id))
        if not snapshot.values:
            raise HTTPException(404, "no such invoice")
        return _view(snapshot)

    def resume(invoice_id: str, decision: str, body: Decision) -> dict[str, str]:
        if "approve" not in app.state.graph.get_state(build.thread(invoice_id)).next:
            raise HTTPException(409, "invoice is not waiting for approval")
        with psycopg.connect(database_url(), autocommit=True) as con:
            payload = {"decision": decision, **body.model_dump()}
            if not graph_worker.enqueue(con, invoice_id, "resume", payload):
                raise HTTPException(409, "a decision is already recorded for this invoice")
        return {"thread_id": invoice_id}

    @app.post("/invoices/{invoice_id}/approve", status_code=202)
    def approve(invoice_id: str, body: Decision) -> dict[str, str]:
        return resume(invoice_id, "approve", body)

    @app.post("/invoices/{invoice_id}/reject", status_code=202)
    def reject(invoice_id: str, body: Decision) -> dict[str, str]:
        return resume(invoice_id, "reject", body)

    return app


app = create_app()
