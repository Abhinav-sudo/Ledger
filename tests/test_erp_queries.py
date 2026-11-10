from ledger.tools import erp_queries
from world.generator import World


def test_vendor_found_by_full_or_abbreviated_name(db: str, world: World) -> None:
    for v in world.vendors[:10]:
        assert erp_queries.find_vendor(v.name).id == v.id
        assert erp_queries.find_vendor(v.short_name.upper()).id == v.id
    assert erp_queries.find_vendor("Nobody Trading Plc") is None


def test_purchase_order_and_receipts(db: str, world: World) -> None:
    po = world.purchase_orders[0]
    got = erp_queries.get_purchase_order(po.po_number)
    assert [ln.unit_price for ln in got.lines] == [ln.unit_price for ln in po.lines]
    assert set(erp_queries.get_receipts(po.po_number)) == {ln.line_no for ln in po.lines}
    assert erp_queries.get_purchase_order("PO-00000") is None
