from decimal import Decimal

import responses

from trading_agent.execution.startup_reconciliation import (
    reconcile_balances,
    reconcile_pending_orders,
)
from trading_agent.execution.testnet_adapter import TESTNET_HOST, TestnetBrokerAdapter
from trading_agent.journal.journal import Journal
from trading_agent.persistence.pending_orders_store import PendingOrdersStore
from trading_agent.portfolio.state import PortfolioState
from trading_agent.strategy.base import PositionSide

HOST = TESTNET_HOST


def _adapter() -> TestnetBrokerAdapter:
    return TestnetBrokerAdapter(api_key="k", api_secret="s")


@responses.activate
def test_not_found_pending_order_is_resolved_with_no_portfolio_change(tmp_path):
    responses.add(
        responses.GET, f"{HOST}/api/v3/order",
        json={"code": -2013, "msg": "Order does not exist."}, status=400,
    )
    portfolio = PortfolioState.initial(Decimal(50))
    with PendingOrdersStore(tmp_path / "pending.db") as store, Journal(tmp_path / "journal.db") as journal:
        store.create("ta-1", "BTCUSDT", "SELL", Decimal("0.001"), 1000, 2000)
        result = reconcile_pending_orders(_adapter(), "BTCUSDT", "USDT", store, portfolio, Decimal("0.001"), journal, 3000)
        assert result.blocked is False
        assert result.portfolio == portfolio
        assert store.load_open("BTCUSDT") == []


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
    portfolio = PortfolioState(
        quote_balance=Decimal(50), base_balance=Decimal("0.001"),
        position_side=PositionSide.LONG,
        avg_entry_price=Decimal(50000), realized_pnl_quote=Decimal(0),
    )
    with PendingOrdersStore(tmp_path / "pending.db") as store, Journal(tmp_path / "journal.db") as journal:
        store.create("ta-1", "BTCUSDT", "SELL", Decimal("0.001"), 1000, 2000)
        result = reconcile_pending_orders(_adapter(), "BTCUSDT", "USDT", store, portfolio, Decimal("0.001"), journal, 3000)
        assert result.blocked is False
        assert result.portfolio.base_balance == Decimal(0)
        assert store.get("ta-1").status == "RESOLVED"


@responses.activate
def test_still_open_order_blocks_new_entries(tmp_path):
    responses.add(
        responses.GET, f"{HOST}/api/v3/order",
        json={"orderId": 1, "clientOrderId": "ta-1", "status": "NEW", "executedQty": "0", "cummulativeQuoteQty": "0", "transactTime": 1},
        status=200,
    )
    portfolio = PortfolioState.initial(Decimal(50))
    with PendingOrdersStore(tmp_path / "pending.db") as store, Journal(tmp_path / "journal.db") as journal:
        store.create("ta-1", "BTCUSDT", "SELL", Decimal("0.001"), 1000, 2000)
        result = reconcile_pending_orders(_adapter(), "BTCUSDT", "USDT", store, portfolio, Decimal("0.001"), journal, 3000)
        assert result.blocked is True
        assert "still open" in result.blocked_reason
        assert store.load_open("BTCUSDT")[0].status == "SUBMITTED"  # remains open, not resolved


@responses.activate
def test_unknown_reconciliation_outcome_blocks_new_entries(tmp_path):
    responses.add(
        responses.GET, f"{HOST}/api/v3/order",
        json={"code": -1021, "msg": "Timestamp for this request is outside of the recvWindow."}, status=400,
    )
    portfolio = PortfolioState.initial(Decimal(50))
    with PendingOrdersStore(tmp_path / "pending.db") as store, Journal(tmp_path / "journal.db") as journal:
        store.create("ta-1", "BTCUSDT", "SELL", Decimal("0.001"), 1000, 2000)
        result = reconcile_pending_orders(_adapter(), "BTCUSDT", "USDT", store, portfolio, Decimal("0.001"), journal, 3000)
        assert result.blocked is True
        assert result.portfolio == portfolio  # never guessed at a change


def test_no_open_orders_is_a_no_op(tmp_path):
    portfolio = PortfolioState.initial(Decimal(50))
    with PendingOrdersStore(tmp_path / "pending.db") as store, Journal(tmp_path / "journal.db") as journal:
        result = reconcile_pending_orders(_adapter(), "BTCUSDT", "USDT", store, portfolio, Decimal("0.001"), journal, 3000)
    assert result.blocked is False
    assert result.portfolio == portfolio


@responses.activate
def test_reconcile_balances_ok_when_matching():
    responses.add(
        responses.GET, f"{HOST}/api/v3/account",
        json={"balances": [
            {"asset": "USDT", "free": "50", "locked": "0"},
            {"asset": "BTC", "free": "0", "locked": "0"},
        ]},
        status=200,
    )
    portfolio = PortfolioState.initial(Decimal(50))
    ok, reason = reconcile_balances(_adapter(), "USDT", "BTC", portfolio)
    assert ok is True
    assert reason is None


@responses.activate
def test_reconcile_balances_detects_quote_mismatch():
    responses.add(
        responses.GET, f"{HOST}/api/v3/account",
        json={"balances": [
            {"asset": "USDT", "free": "40", "locked": "0"},
            {"asset": "BTC", "free": "0", "locked": "0"},
        ]},
        status=200,
    )
    portfolio = PortfolioState.initial(Decimal(50))
    ok, reason = reconcile_balances(_adapter(), "USDT", "BTC", portfolio)
    assert ok is False
    assert "mismatch" in reason


@responses.activate
def test_reconcile_balances_includes_locked_amounts():
    # Local base_balance=0.001 should match free+locked on the exchange, not just free.
    responses.add(
        responses.GET, f"{HOST}/api/v3/account",
        json={"balances": [
            {"asset": "USDT", "free": "50", "locked": "0"},
            {"asset": "BTC", "free": "0.0005", "locked": "0.0005"},
        ]},
        status=200,
    )
    portfolio = PortfolioState(
        quote_balance=Decimal(50), base_balance=Decimal("0.001"),
        position_side=PositionSide.LONG,
        avg_entry_price=Decimal(50000), realized_pnl_quote=Decimal(0),
    )
    ok, reason = reconcile_balances(_adapter(), "USDT", "BTC", portfolio)
    assert ok is True
    assert reason is None
