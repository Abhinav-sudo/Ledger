import os
from decimal import Decimal
from pathlib import Path

PRICE_TOLERANCE = Decimal("0.02")
QTY_TOLERANCE = Decimal("0")
CONFIDENCE_THRESHOLD = 0.85


def database_url() -> str:
    return os.environ.get("LEDGER_DATABASE_URL", "postgresql://ledger:ledger@localhost:5432/ledger")


def inbox() -> Path:
    return Path(os.environ.get("LEDGER_INBOX", "out/inbox"))


def bank_url() -> str:
    return os.environ.get("BANK_URL", "http://localhost:8081")


def outbox_mode() -> str:
    """on (default), or the crash-suite ablations: off and off-keyed pay the bank inline."""
    mode = os.environ.get("LEDGER_OUTBOX", "on")
    if mode not in ("on", "off", "off-keyed"):
        raise ValueError(f"LEDGER_OUTBOX must be on, off or off-keyed, not {mode!r}")
    return mode
