from decimal import Decimal
from pathlib import Path

import pytest
from helpers import ACCOUNT, bank, extracted

from ledger.contracts.decisions import Proposal
from ledger.graph.nodes import payment_intent
from world.generator import World


def _state(world: World) -> dict:
    inv = world.invoices[0]
    vendor = next(v for v in world.vendors if v.id == inv.vendor_id)
    ext = extracted(inv, vendor).model_copy(update={"remit_to_account": ACCOUNT})
    proposal = Proposal(
        action="pay", amount=Decimal("42.00"), rationale="", requires_approval=False
    )
    return {"invoice_id": inv.id, "extracted": ext, "proposal": proposal}


@pytest.mark.parametrize(("mode", "payments"), [("off", 2), ("off-keyed", 1)])
def test_inline_reruns(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, payments: int
) -> None:
    client = bank(tmp_path / "bank.db")
    monkeypatch.setattr(payment_intent, "_bank", lambda: client)
    monkeypatch.setenv("LEDGER_OUTBOX", mode)
    for _ in range(2):
        assert payment_intent.run(_state(world))["status"] == "paid"
    assert len(client.get("/statement").json()["payments"]) == payments


def test_outbox_is_the_default_and_calls_no_bank(
    db: str, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_bank() -> None:
        raise AssertionError("the outbox path must not call the bank")

    monkeypatch.setattr(payment_intent, "_bank", no_bank)
    monkeypatch.delenv("LEDGER_OUTBOX", raising=False)
    out = payment_intent.run(_state(world))
    assert out["status"] == "payment_pending"
    assert out["intent"].amount == Decimal("42.00")


def test_unknown_mode_is_refused(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEDGER_OUTBOX", "maybe")
    with pytest.raises(ValueError, match="LEDGER_OUTBOX"):
        payment_intent.run(_state(world))
