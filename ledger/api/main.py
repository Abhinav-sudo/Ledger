import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from email import message_from_bytes, policy
from typing import Annotated, Any

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from langgraph.types import Command
from pydantic import BaseModel
from pydantic_core import to_jsonable_python

from ledger.graph import build
from ledger.llm.router import ModelRouter
from ledger.settings import database_url, inbox


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
        background: BackgroundTasks,
        email: UploadFile | None = None,
        invoice_id: Annotated[str | None, Form(pattern=r"^[A-Za-z0-9_-]{1,64}$")] = None,
    ) -> dict[str, str]:
        data = pdf.file.read()
        invoice_id = invoice_id or f"INV-{hashlib.sha256(data).hexdigest()[:16]}"
        config = build.thread(invoice_id)
        # ponytail: check-then-run, two simultaneous uploads of one invoice can both start a run;
        # the payment outbox key, not this check, is what stops a double payment.
        if app.state.graph.get_state(config).values:
            return {"thread_id": invoice_id}
        path = inbox() / f"{invoice_id}.pdf"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        state = {
            "invoice_id": invoice_id,
            "pdf_path": str(path),
            "email": email_text(email.file.read()) if email else None,
        }
        background.add_task(app.state.graph.invoke, state, config)
        return {"thread_id": invoice_id}

    @app.get("/invoices/{invoice_id}")
    def show(invoice_id: str) -> JSONResponse:
        snapshot = app.state.graph.get_state(build.thread(invoice_id))
        if not snapshot.values:
            raise HTTPException(404, "no such invoice")
        return _view(snapshot)

    def resume(invoice_id: str, decision: str, body: Decision) -> JSONResponse:
        config = build.thread(invoice_id)
        if "approve" not in app.state.graph.get_state(config).next:
            raise HTTPException(409, "invoice is not waiting for approval")
        app.state.graph.invoke(Command(resume={"decision": decision, **body.model_dump()}), config)
        return _view(app.state.graph.get_state(config))

    @app.post("/invoices/{invoice_id}/approve")
    def approve(invoice_id: str, body: Decision) -> JSONResponse:
        return resume(invoice_id, "approve", body)

    @app.post("/invoices/{invoice_id}/reject")
    def reject(invoice_id: str, body: Decision) -> JSONResponse:
        return resume(invoice_id, "reject", body)

    return app


app = create_app()
