import time
from decimal import Decimal

import pytest
import requests
import responses

from tests.fixtures.exchange_info import make_exchange_info
from trading_agent.config.models import AppConfig, Secrets
from trading_agent.data.models import interval_to_ms
from trading_agent.execution.live_runner import (
    TESTNET_ENTRY_DISABLED_REASON,
    ColdStartReconciliationError,
    run_testnet_cycle,
)
from trading_agent.journal.journal import Journal
from trading_agent.persistence.pending_orders_store import PendingOrdersStore
from trading_agent.persistence.portfolio_store import PortfolioStore
from trading_agent.persistence.risk_state_store import RiskStateStore
from trading_agent.portfolio.state import PortfolioState
from trading_agent.risk.kill_switch import KillSwitch
from trading_agent.strategy.base import PositionSide

HOST = "https://testnet.binance.vision"
INTERVAL = "4h"
STEP = interval_to_ms(INTERVAL)


def _kline_row(open_time_ms: int, close: float) -> list:
    return [
        open_time_ms,
        f"{close:.2f}",
        f"{close + 1:.2f}",
        f"{close - 1:.2f}",
        f"{close:.2f}",
        "10.0",
        open_time_ms + STEP - 1,
        "1000.0",
        100,
        "5.0",
        "500.0",
        "0",
    ]


def _kline_rows_ending_near_now(closes: list[float]) -> list[list]:
    """Anchor the LAST candle's close time to ~1ms before real wall-clock
    time, so the server-time mock used for both completed-candle filtering
    AND clock-drift sync (Finding 8) is realistic - these tests exercise
    `sync_time` against the real clock, not a synthetic one."""
    now_ms = int(time.time() * 1000)
    n = len(closes)
    start = now_ms - n * STEP
    return [_kline_row(start + i * STEP, c) for i, c in enumerate(closes)]


def _config(tmp_path, **risk_overrides) -> AppConfig:
    return AppConfig(
        mode="testnet",
        strategy={"ema_fast": 3, "ema_slow": 6},
        risk=risk_overrides,
        paths={"data_dir": str(tmp_path), "logs_dir": str(tmp_path), "db_path": str(tmp_path / "agent.db")},
    )


def _secrets() -> Secrets:
    return Secrets(testnet_api_key="k", testnet_api_secret="s")


def _mock_account(quote_free="50", quote_locked="0", base_free="0", base_locked="0"):
    responses.add(
        responses.GET,
        f"{HOST}/api/v3/account",
        json={
            "balances": [
                {"asset": "USDT", "free": quote_free, "locked": quote_locked},
                {"asset": "BTC", "free": base_free, "locked": base_locked},
            ]
        },
        status=200,
    )


def _mock_common(closes: list[float], server_time_ms: int | None = None, min_notional: str = "0.01"):
    rows = _kline_rows_ending_near_now(closes)
    server_time = server_time_ms if server_time_ms is not None else rows[-1][6] + 1
    responses.add(responses.GET, f"{HOST}/api/v3/time", json={"serverTime": server_time}, status=200)
    responses.add(responses.GET, f"{HOST}/api/v3/klines", json=rows, status=200)
    responses.add(
        responses.GET,
        f"{HOST}/api/v3/exchangeInfo",
        json=make_exchange_info(min_notional=min_notional),
        status=200,
    )
    return rows


def _seed_flat_portfolio(config, quote_balance="50"):
    with PortfolioStore(config.paths.db_path) as store:
        store.save("BTCUSDT", PortfolioState.initial(Decimal(quote_balance)), updated_at_ms=0)


def _seed_long_portfolio(config, quote_balance="10", base_balance="0.0004", avg_price="50000"):
    with PortfolioStore(config.paths.db_path) as store:
        store.save(
            "BTCUSDT",
            PortfolioState(
                quote_balance=Decimal(quote_balance),
                base_balance=Decimal(base_balance),
                position_side=PositionSide.LONG,
                avg_entry_price=Decimal(avg_price),
                realized_pnl_quote=Decimal(0),
            ),
            updated_at_ms=0,
        )


def _run(config, tmp_path):
    with (
        Journal(tmp_path / "journal.db") as journal,
        PortfolioStore(config.paths.db_path) as pstore,
        RiskStateStore(tmp_path / "risk.db") as rstore,
        PendingOrdersStore(tmp_path / "pending.db") as pendingstore,
    ):
        return run_testnet_cycle(config, _secrets(), journal, pstore, rstore, pendingstore)


@responses.activate
def test_hold_signal_produces_no_order(tmp_path):
    config = _config(tmp_path)
    _mock_common([100.0] * 8)
    _seed_flat_portfolio(config)
    _mock_account(quote_free="50", base_free="0")
    result = _run(config, tmp_path)
    assert result.action == "HOLD"


@responses.activate
def test_buy_signal_is_suppressed_on_testnet(tmp_path):
    # Bullish crossover series (same shape used in strategy tests) - would
    # be a BUY, but Finding 4: automatic entry is disabled on Testnet.
    config = _config(tmp_path)
    closes = [100, 99, 98, 97, 96, 95, 100]
    _mock_common(closes)
    _seed_flat_portfolio(config)
    _mock_account(quote_free="50", base_free="0")
    result = _run(config, tmp_path)
    assert result.action == "NO_TRADE"
    assert result.reason_code == TESTNET_ENTRY_DISABLED_REASON
    with PortfolioStore(config.paths.db_path) as store:
        portfolio = store.load("BTCUSDT")
    assert portfolio.position_side == PositionSide.FLAT  # untouched


@responses.activate
def test_exit_signal_places_sell_order_and_updates_portfolio(tmp_path):
    config = _config(tmp_path)
    closes = [70, 75, 80, 85, 90, 95, 100, 50]  # bearish crossover -> EXIT
    _mock_common(closes)
    _seed_long_portfolio(config, quote_balance="10", base_balance="0.0004", avg_price="50000")
    _mock_account(quote_free="10", base_free="0.0004")
    responses.add(
        responses.POST,
        f"{HOST}/api/v3/order",
        json={
            "orderId": 1,
            "clientOrderId": "ta-x",
            "status": "FILLED",
            "executedQty": "0.0004",
            "cummulativeQuoteQty": "20.0",
            "transactTime": 1,
            "fills": [{"price": "50000", "qty": "0.0004", "commission": "0.02", "commissionAsset": "USDT"}],
        },
        status=200,
    )
    result = _run(config, tmp_path)
    assert result.action == "SELL"
    with PortfolioStore(config.paths.db_path) as store:
        portfolio = store.load("BTCUSDT")
    assert portfolio.position_side == PositionSide.FLAT
    assert portfolio.base_balance == Decimal(0)


@responses.activate
def test_kill_switch_blocks_exit(tmp_path):
    config = _config(tmp_path)
    closes = [70, 75, 80, 85, 90, 95, 100, 50]  # bearish crossover -> EXIT
    _mock_common(closes)
    _seed_long_portfolio(config)
    _mock_account(quote_free="10", base_free="0.0004")
    KillSwitch(config.paths.data_dir / "KILL_SWITCH").engage("manual test stop")
    result = _run(config, tmp_path)
    assert result.action == "NO_TRADE"
    assert result.reason_code == "KILL_SWITCH_ENGAGED"


@responses.activate
def test_stale_data_blocks_trade_without_clock_drift(tmp_path):
    # Candles anchored far in the past, but server time matches the real
    # wall clock (so sync_time succeeds) - isolates staleness from drift.
    config = _config(tmp_path, stale_data_max_age_seconds=60)
    closes = [70, 75, 80, 85, 90, 95, 100, 50]
    now_ms = int(time.time() * 1000)
    old_start = now_ms - 100_000_000 - len(closes) * STEP
    rows = [_kline_row(old_start + i * STEP, c) for i, c in enumerate(closes)]
    responses.add(responses.GET, f"{HOST}/api/v3/time", json={"serverTime": now_ms}, status=200)
    responses.add(responses.GET, f"{HOST}/api/v3/klines", json=rows, status=200)
    responses.add(responses.GET, f"{HOST}/api/v3/exchangeInfo", json=make_exchange_info(min_notional="1"), status=200)
    _seed_long_portfolio(config)
    result = _run(config, tmp_path)
    assert result.action == "NO_TRADE"
    assert result.reason_code == "DATA_UNAVAILABLE_OR_INVALID"


@responses.activate
def test_excessive_clock_drift_fails_closed(tmp_path):
    config = _config(tmp_path)
    closes = [70, 75, 80, 85, 90, 95, 100, 50]
    rows = _kline_rows_ending_near_now(closes)
    far_future_server_time = rows[-1][6] + 10_000_000  # ~2.7 hours off the real clock
    responses.add(responses.GET, f"{HOST}/api/v3/time", json={"serverTime": far_future_server_time}, status=200)
    _seed_long_portfolio(config)
    result = _run(config, tmp_path)
    assert result.action == "ERROR"
    assert result.reason_code == "CLOCK_DRIFT_EXCEEDS_TOLERANCE"


@responses.activate
def test_cold_start_with_nonzero_base_balance_refuses_to_guess(tmp_path):
    config = _config(tmp_path)
    closes = [100.0] * 8
    _mock_common(closes)
    _mock_account(quote_free="10", base_free="1")  # nonzero, well above min_qty
    with (
        Journal(tmp_path / "journal.db") as journal,
        PortfolioStore(config.paths.db_path) as pstore,
        RiskStateStore(tmp_path / "risk.db") as rstore,
        PendingOrdersStore(tmp_path / "pending.db") as pendingstore,
        pytest.raises(ColdStartReconciliationError),
    ):
        run_testnet_cycle(config, _secrets(), journal, pstore, rstore, pendingstore)


@responses.activate
def test_timeout_then_not_found_retries_and_succeeds(tmp_path):
    config = _config(tmp_path)
    closes = [70, 75, 80, 85, 90, 95, 100, 50]
    _mock_common(closes)
    _seed_long_portfolio(config)
    _mock_account(quote_free="10", base_free="0.0004")

    responses.add(responses.POST, f"{HOST}/api/v3/order", body=requests.exceptions.Timeout())
    responses.add(
        responses.GET, f"{HOST}/api/v3/order",
        json={"code": -2013, "msg": "Order does not exist."}, status=400,
    )
    responses.add(
        responses.POST,
        f"{HOST}/api/v3/order",
        json={
            "orderId": 2, "clientOrderId": "ta-x", "status": "FILLED",
            "executedQty": "0.0004", "cummulativeQuoteQty": "20.0", "transactTime": 1,
        },
        status=200,
    )
    result = _run(config, tmp_path)
    assert result.action == "SELL"


@responses.activate
def test_connection_error_is_treated_as_ambiguous_and_reconciled_before_retry(tmp_path):
    # Finding 9: not just Timeout - any ambiguous network failure must be
    # reconciled before a retry is attempted.
    config = _config(tmp_path)
    closes = [70, 75, 80, 85, 90, 95, 100, 50]
    _mock_common(closes)
    _seed_long_portfolio(config)
    _mock_account(quote_free="10", base_free="0.0004")

    responses.add(responses.POST, f"{HOST}/api/v3/order", body=requests.exceptions.ConnectionError())
    responses.add(
        responses.GET, f"{HOST}/api/v3/order",
        json={"code": -2013, "msg": "Order does not exist."}, status=400,
    )
    responses.add(
        responses.POST,
        f"{HOST}/api/v3/order",
        json={
            "orderId": 2, "clientOrderId": "ta-x", "status": "FILLED",
            "executedQty": "0.0004", "cummulativeQuoteQty": "20.0", "transactTime": 1,
        },
        status=200,
    )
    result = _run(config, tmp_path)
    assert result.action == "SELL"


@responses.activate
def test_duplicate_order_already_journaled_blocks_resubmission(tmp_path):
    config = _config(tmp_path)
    closes = [70, 75, 80, 85, 90, 95, 100, 50]
    rows = _mock_common(closes)
    _seed_long_portfolio(config)
    _mock_account(quote_free="10", base_free="0.0004")
    last_close_time_ms = rows[-1][6]

    with Journal(tmp_path / "journal.db") as journal:
        from trading_agent.execution.client_order_id import generate_client_order_id

        existing_id = generate_client_order_id("BTCUSDT", "SELL", last_close_time_ms)
        journal.record(
            "ORDER_SUBMITTED",
            {"client_order_id": existing_id, "side": "SELL", "quantity": "0.0004"},
            last_close_time_ms,
        )

    result = _run(config, tmp_path)
    assert result.action == "NO_TRADE"
    assert result.reason_code == "DUPLICATE_ORDER_BLOCKED"


@responses.activate
def test_balance_discrepancy_blocks_new_entries_but_exit_still_allowed(tmp_path):
    # A BUY would already be suppressed regardless, but this proves the
    # reconciliation-blocked gate is wired through RiskContext and that an
    # EXIT is unaffected by a balance mismatch (Findings 2 & 5).
    config = _config(tmp_path)
    closes = [70, 75, 80, 85, 90, 95, 100, 50]
    _mock_common(closes)
    _seed_long_portfolio(config, quote_balance="10", base_balance="0.0004")
    _mock_account(quote_free="999", base_free="0.0004")  # quote mismatch
    responses.add(
        responses.POST,
        f"{HOST}/api/v3/order",
        json={
            "orderId": 1, "clientOrderId": "ta-x", "status": "FILLED",
            "executedQty": "0.0004", "cummulativeQuoteQty": "20.0", "transactTime": 1,
        },
        status=200,
    )
    result = _run(config, tmp_path)
    assert result.action == "SELL"  # still allowed despite the discrepancy


@responses.activate
def test_pending_order_from_previous_crashed_run_is_resolved_before_new_signal(tmp_path):
    # Simulates a crash: an order was durably recorded as SUBMITTED but the
    # process died before applying its outcome. The next cycle must resolve
    # it (Finding 2) BEFORE generating a new signal - here, resolving the
    # crashed sell brings the position fully flat, so the resulting bearish
    # crossover signal correctly becomes a HOLD (nothing left to sell)
    # rather than blindly acting on stale in-memory state.
    config = _config(tmp_path)
    closes = [70, 75, 80, 85, 90, 95, 100, 50]
    _mock_common(closes)
    _seed_long_portfolio(config, quote_balance="10", base_balance="0.0004")
    _mock_account(quote_free="10", base_free="0.0004")

    with PendingOrdersStore(tmp_path / "pending.db") as store:
        store.create("ta-crashed", "BTCUSDT", "SELL", Decimal("0.0004"), 1, 2)

    responses.add(
        responses.GET, f"{HOST}/api/v3/order",
        json={
            "orderId": 99, "clientOrderId": "ta-crashed", "status": "FILLED",
            "executedQty": "0.0004", "cummulativeQuoteQty": "20.0", "transactTime": 1,
            "fills": [{"price": "50000", "qty": "0.0004", "commission": "0.02", "commissionAsset": "USDT"}],
        },
        status=200,
    )
    result = _run(config, tmp_path)
    assert result.action == "HOLD"
    assert result.reason_code == "HOLD_ALREADY_FLAT"
    with PortfolioStore(config.paths.db_path) as pstore:
        portfolio = pstore.load("BTCUSDT")
    assert portfolio.base_balance == Decimal(0)  # crashed order's fill was applied, position now flat
    with PendingOrdersStore(tmp_path / "pending.db") as store:
        assert store.get("ta-crashed").status == "RESOLVED"


@responses.activate
def test_still_open_pending_order_blocks_new_signal_this_cycle(tmp_path):
    config = _config(tmp_path)
    closes = [70, 75, 80, 85, 90, 95, 100, 50]
    _mock_common(closes)
    _seed_long_portfolio(config, quote_balance="10", base_balance="0.0008")
    _mock_account(quote_free="10", base_free="0.0008")

    with PendingOrdersStore(tmp_path / "pending.db") as store:
        store.create("ta-crashed", "BTCUSDT", "SELL", Decimal("0.0004"), 1, 2)

    responses.add(
        responses.GET, f"{HOST}/api/v3/order",
        json={
            "orderId": 99, "clientOrderId": "ta-crashed", "status": "NEW",
            "executedQty": "0", "cummulativeQuoteQty": "0", "transactTime": 1,
        },
        status=200,
    )
    result = _run(config, tmp_path)
    assert result.action == "NO_TRADE"
    assert result.reason_code == "UNRESOLVED_ORDER_BLOCKS_NEW_SIGNAL"
    with PendingOrdersStore(tmp_path / "pending.db") as store:
        assert store.get("ta-crashed").status == "SUBMITTED"  # remains open
