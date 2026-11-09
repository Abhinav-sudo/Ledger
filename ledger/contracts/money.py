from decimal import Decimal
from typing import Annotated

from pydantic import BeforeValidator

CENT = Decimal("0.01")


def _no_float(value: object) -> object:
    if isinstance(value, float):
        raise ValueError("money must be a Decimal, int, or string, never a float")
    return value


Money = Annotated[Decimal, BeforeValidator(_no_float)]
