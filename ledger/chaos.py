"""Fault injection for the crash suite. Inert unless LEDGER_CHAOS is set."""

import os
import random
import signal
from functools import cache


@cache
def _odds() -> dict[str, float]:
    spec = os.environ.get("LEDGER_CHAOS", "")
    return {k: float(v) for k, v in (p.split("=") for p in spec.split(",") if p)}


def point(name: str) -> None:
    odds = _odds().get(name)
    if odds and random.random() < odds:
        os.kill(os.getpid(), signal.SIGKILL)
