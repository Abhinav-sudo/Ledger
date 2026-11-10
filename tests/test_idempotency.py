from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ledger.side_effects.idempotency import intent_id, key

ACCOUNT = "GB80LDGR07117181324261"


def test_trailing_zeros_hash_the_same() -> None:
    keys = {key("INV-1", Decimal(v), ACCOUNT) for v in ("100", "100.0", "100.00", "100.000")}
    assert len(keys) == 1


def test_sub_cent_amount_is_refused() -> None:
    with pytest.raises(ValueError, match="sub-cent"):
        key("INV-1", Decimal("100.001"), ACCOUNT)


def test_account_formatting_does_not_change_the_key() -> None:
    assert key("INV-1", Decimal(5), ACCOUNT) == key(
        "INV-1", Decimal(5), " gb80 ldgr0711 7181324261"
    )


def test_intent_id_is_deterministic() -> None:
    k = key("INV-1", Decimal(5), ACCOUNT)
    assert intent_id(k) == intent_id(k)


cents = st.integers(min_value=1, max_value=10**12).map(lambda c: Decimal(c) / 100)


@given(cents, cents, st.sampled_from(["INV-1", "INV-2"]), st.sampled_from(["INV-1", "INV-2"]))
def test_keys_collide_exactly_when_inputs_are_equal(
    a: Decimal, b: Decimal, inv_a: str, inv_b: str
) -> None:
    same = (inv_a, a) == (inv_b, b)
    assert (key(inv_a, a, ACCOUNT) == key(inv_b, b, ACCOUNT)) == same
