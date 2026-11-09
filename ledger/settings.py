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
