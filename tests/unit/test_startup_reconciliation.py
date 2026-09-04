from decimal import Decimal

import responses

from trading_agent.execution.startup_reconciliation import (
    reconcile_balances,
    reconcile_pending_orders,
)
from trading_agent.execution.testnet_adapter import TESTNET_HOST, TestnetBrokerAdapter
from trading_agent.journal.journal import Journal
from trading_agent.persistence.execution_store import ExecutionStateStore
from trading_agent.portfolio.state import PortfolioState
from trading_agent.strategy.base import PositionSide

HOST = TESTNET_HOST
QUOTE_ASSET = "USDT"
BASE_ASSET = "BTC"


def _adapter() -> TestnetBrokerAdapter:
    return TestnetBrokerAdapter(api_key="k", api_secret="s")


@responses.activate
def test_not_found_pending_order_is_resolved_with_no_portfolio_change(tmp_path):
    responses.add(
        responses.GET, f"{HOST}/api/v3/order",
        json={"code": -2013, "msg": "Order does not exist."}, status=400,
    )
    with ExecutionStateStore(tmp_path / "state.db") as store, Journal(tmp_path / "journal.db") as journal:
        store.save_portfolio("BTCUSDT", PortfolioState.initial(Decimal(50)), updated_at_ms=0)
        store.create_pending("ta-1", "BTCUSDT", "SELL", Decimal("0.001"), 1000, 2000)
        result = reconcile_pending_orders(
            _adapter(), "BTCUSDT", QUOTE_ASSET, BASE_ASSET, store, Decimal("0.001"), journal, 3000
        )
        assert result.blocked is False
        assert store.load_portfolio("BTCUSDT").base_balance == Decimal(0)
        assert store.load_open_pending("BTCUSDT") == []
        assert store.get_pending("ta-1").status == "RESOLVED"


@responses.activate
def test_confirmed_filled_order_is_applied_exactly_once(tmp_path):
    responses.add(
        responses.GET, f"{HOST}/api/v3/order",
        json={
            "orderId": 1, "clientOrderId": "ta-1", "status": "FILLED",
            "executedQty": "0.001", "cummulativeQuoteQty": "55.0", "transactTime": 1,
            "fills": [{"price": "55000", "qty": "0.001", "commission": "0.055", "commissionAsset": "USDT"}],
        },
        status=200,
    )
    with ExecutionStateStore(tmp_path / "state.db") as store, Journal(tmp_path / "journal.db") as journal:
        store.save_portfolio(
            "BTCUSDT",
            PortfolioState(
                quote_balance=Decimal(50), base_balance=Decimal("0.001"),
                position_side=PositionSide.LONG,
                avg_entry_price=Decimal(50000), realized_pnl_quote=Decimal(0),
            ),
            updated_at_ms=0,
        )
        store.create_pending("ta-1", "BTCUSDT", "SELL", Decimal("0.001"), 1000, 2000)
        result = reconcile_pending_orders(
            _adapter(), "BTCUSDT", QUOTE_ASSET, BASE_ASSET, store, Decimal("0.001"), journal, 3000
        )
        assert result.blocked is False
        assert store.load_portfolio("BTCUSDT").base_balance == Decimal(0)
        assert store.get_pending("ta-1").status == "RESOLVED"


@responses.activate
def test_still_open_order_blocks_new_entries(tmp_path):
    responses.add(
        responses.GET, f"{HOST}/api/v3/order",
        json={"orderId": 1, "clientOrderId": "ta-1", "status": "NEW", "executedQty": "0", "cummulativeQuoteQty": "0", "transactTime": 1},
        status=200,
    )
    with ExecutionStateStore(tmp_path / "state.db") as store, Journal(tmp_path / "journal.db") as journal:
        store.save_portfolio("BTCUSDT", PortfolioState.initial(Decimal(50)), updated_at_ms=0)
        store.create_pending("ta-1", "BTCUSDT", "SELL", Decimal("0.001"), 1000, 2000)
        result = reconcile_pending_orders(
            _adapter(), "BTCUSDT", QUOTE_ASSET, BASE_ASSET, store, Decimal("0.001"), journal, 3000
        )
        assert result.blocked is True
        assert "still open" in result.blocked_reason
        assert store.load_open_pending("BTCUSDT")[0].status == "SUBMITTED"  # remains open, not resolved


@responses.activate
def test_unknown_reconciliation_outcome_blocks_new_entries(tmp_path):
    responses.add(
        responses.GET, f"{HOST}/api/v3/order",
        json={"code": -1021, "msg": "Timestamp for this request is outside of the recvWindow."}, status=400,
    )
    with ExecutionStateStore(tmp_path / "state.db") as store, Journal(tmp_path / "journal.db") as journal:
        store.save_portfolio("BTCUSDT", PortfolioState.initial(Decimal(50)), updated_at_ms=0)
        store.create_pending("ta-1", "BTCUSDT", "SELL", Decimal("0.001"), 1000, 2000)
        result = reconcile_pending_orders(
            _adapter(), "BTCUSDT", QUOTE_ASSET, BASE_ASSET, store, Decimal("0.001"), journal, 3000
        )
        assert result.blocked is True
        assert store.load_portfolio("BTCUSDT").base_balance == Decimal(0)  # never guessed at a change


def test_no_open_orders_is_a_no_op(tmp_path):
    with ExecutionStateStore(tmp_path / "state.db") as store, Journal(tmp_path / "journal.db") as journal:
        store.save_portfolio("BTCUSDT", PortfolioState.initial(Decimal(50)), updated_at_ms=0)
        result = reconcile_pending_orders(
            _adapter(), "BTCUSDT", QUOTE_ASSET, BASE_ASSET, store, Decimal("0.001"), journal, 3000
        )
    assert result.blocked is False


@responses.activate
def test_reconcile_balances_ok_when_matching(tmp_path):
    responses.add(
        responses.GET, f"{HOST}/api/v3/account",
        json={"balances": [
            {"asset": "USDT", "free": "50", "locked": "0"},
            {"asset": "BTC", "free": "0", "locked": "0"},
        ]},
        status=200,
    )
    with ExecutionStateStore(tmp_path / "state.db") as store:
        store.save_portfolio("BTCUSDT", PortfolioState.initial(Decimal(50)), updated_at_ms=0)
        ok, reason = reconcile_balances(_adapter(), "BTCUSDT", QUOTE_ASSET, BASE_ASSET, store)
    assert ok is True
    assert reason is None


@responses.activate
def test_reconcile_balances_detects_quote_mismatch(tmp_path):
    responses.add(
        responses.GET, f"{HOST}/api/v3/account",
        json={"balances": [
            {"asset": "USDT", "free": "40", "locked": "0"},
            {"asset": "BTC", "free": "0", "locked": "0"},
        ]},
        status=200,
    )
    with ExecutionStateStore(tmp_path / "state.db") as store:
        store.save_portfolio("BTCUSDT", PortfolioState.initial(Decimal(50)), updated_at_ms=0)
        ok, reason = reconcile_balances(_adapter(), "BTCUSDT", QUOTE_ASSET, BASE_ASSET, store)
    assert ok is False
    assert "mismatch" in reason


@responses.activate
def test_reconcile_balances_includes_locked_amounts(tmp_path):
    # Local base_balance=0.001 should match free+locked on the exchange, not just free.
    responses.add(
        responses.GET, f"{HOST}/api/v3/account",
        json={"balances": [
            {"asset": "USDT", "free": "50", "locked": "0"},
            {"asset": "BTC", "free": "0.0005", "locked": "0.0005"},
        ]},
        status=200,
    )
    with ExecutionStateStore(tmp_path / "state.db") as store:
        store.save_portfolio(
            "BTCUSDT",
            PortfolioState(
                quote_balance=Decimal(50), base_balance=Decimal("0.001"),
                position_side=PositionSide.LONG,
                avg_entry_price=Decimal(50000), realized_pnl_quote=Decimal(0),
            ),
            updated_at_ms=0,
        )
        ok, reason = reconcile_balances(_adapter(), "BTCUSDT", QUOTE_ASSET, BASE_ASSET, store)
    assert ok is True
    assert reason is None


@responses.activate
def test_balance_reconciliation_reads_portfolio_fresh_after_pending_order_resolution(tmp_path):
    # Round 2 regression: reconcile_balances must read the portfolio FRESH
    # from the store, not from a caller-held copy taken before pending-order
    # reconciliation ran (which may just have changed it via a real fill).
    responses.add(
        responses.GET, f"{HOST}/api/v3/order",
        json={
            "orderId": 1, "clientOrderId": "ta-1", "status": "FILLED",
            "executedQty": "0.001", "cummulativeQuoteQty": "50.0", "transactTime": 1,
            "fills": [{"price": "50000", "qty": "0.001", "commission": "0.05", "commissionAsset": "USDT"}],
        },
        status=200,
    )
    responses.add(
        responses.GET, f"{HOST}/api/v3/account",
        json={"balances": [
            {"asset": "USDT", "free": "0", "locked": "0"},
            {"asset": "BTC", "free": "0.001", "locked": "0"},
        ]},
        status=200,
    )
    with ExecutionStateStore(tmp_path / "state.db") as store, Journal(tmp_path / "journal.db") as journal:
        store.save_portfolio("BTCUSDT", PortfolioState.initial(Decimal("50.05")), updated_at_ms=0)
        store.create_pending("ta-1", "BTCUSDT", "BUY", Decimal("0.001"), 1000, 2000)
        adapter = _adapter()
        pending_result = reconcile_pending_orders(
            adapter, "BTCUSDT", QUOTE_ASSET, BASE_ASSET, store, Decimal("0.001"), journal, 3000
        )
        assert pending_result.blocked is False
        # If this read a stale caller-held portfolio (still flat), it would
        # wrongly report a mismatch against the exchange's post-fill balance.
        ok, reason = reconcile_balances(adapter, "BTCUSDT", QUOTE_ASSET, BASE_ASSET, store)
    assert ok is True
    assert reason is None
