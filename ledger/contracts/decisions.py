from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from ledger.contracts.money import Money

Action = Literal["pay", "pay_partial", "hold", "reject", "request_credit_note"]


class Analysis(BaseModel):
    agent: str
    finding: str
    evidence: list[str]
    confidence: float = Field(ge=0, le=1)


class Proposal(BaseModel):
    action: Action
    amount: Money = Field(ge=0)
    rationale: str
    requires_approval: bool
    approver_role: str | None = None


class Approval(BaseModel):
    decision: Literal["approve", "reject"]
    by: str
    at: datetime
    note: str | None = None


class AuditEvent(BaseModel):
    actor: str
    event: str
    detail: dict[str, Any] = {}
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))
