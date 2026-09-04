from decimal import Decimal

from trading_agent.persistence.pending_orders_store import (
    STATUS_RESOLVED,
    STATUS_SUBMITTED,
    PendingOrdersStore,
)


def test_create_and_load_open(tmp_path):
    with PendingOrdersStore(tmp_path / "pending.db") as store:
        store.create("ta-1", "BTCUSDT", "SELL", Decimal("0.001"), 1000, 2000)
        open_orders = store.load_open("BTCUSDT")
    assert len(open_orders) == 1
    assert open_orders[0].client_order_id == "ta-1"
    assert open_orders[0].status == STATUS_SUBMITTED
    assert open_orders[0].applied_executed_qty == Decimal(0)


def test_create_is_idempotent_on_conflict(tmp_path):
    with PendingOrdersStore(tmp_path / "pending.db") as store:
        store.create("ta-1", "BTCUSDT", "SELL", Decimal("0.001"), 1000, 2000)
        store.create("ta-1", "BTCUSDT", "SELL", Decimal("0.001"), 1000, 2000)  # no-op, same id
        assert len(store.load_open("BTCUSDT")) == 1


def test_update_applied_qty(tmp_path):
    with PendingOrdersStore(tmp_path / "pending.db") as store:
        store.create("ta-1", "BTCUSDT", "SELL", Decimal("0.001"), 1000, 2000)
        store.update_applied_qty("ta-1", Decimal("0.0005"))
        pending = store.get("ta-1")
    assert pending.applied_executed_qty == Decimal("0.0005")
    assert pending.status == STATUS_SUBMITTED


def test_mark_resolved_removes_from_open_list(tmp_path):
    with PendingOrdersStore(tmp_path / "pending.db") as store:
        store.create("ta-1", "BTCUSDT", "SELL", Decimal("0.001"), 1000, 2000)
        store.mark_resolved("ta-1", "FILLED", 3000)
        open_orders = store.load_open("BTCUSDT")
        resolved = store.get("ta-1")
    assert open_orders == []
    assert resolved.status == STATUS_RESOLVED
    assert resolved.resolved_order_status == "FILLED"
    assert resolved.resolved_at_ms == 3000


def test_get_returns_none_for_unknown_id(tmp_path):
    with PendingOrdersStore(tmp_path / "pending.db") as store:
        assert store.get("nonexistent") is None


def test_load_open_only_returns_matching_symbol(tmp_path):
    with PendingOrdersStore(tmp_path / "pending.db") as store:
        store.create("ta-1", "BTCUSDT", "SELL", Decimal("0.001"), 1000, 2000)
        store.create("ta-2", "ETHUSDT", "SELL", Decimal("0.01"), 1000, 2000)
        assert len(store.load_open("BTCUSDT")) == 1
        assert len(store.load_open("ETHUSDT")) == 1
