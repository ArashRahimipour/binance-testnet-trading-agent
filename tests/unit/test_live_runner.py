from decimal import Decimal

import pytest
import requests
import responses

from tests.fixtures.exchange_info import make_exchange_info
from trading_agent.config.models import AppConfig, Secrets
from trading_agent.data.models import interval_to_ms
from trading_agent.execution.live_runner import ColdStartReconciliationError, run_testnet_cycle
from trading_agent.journal.journal import Journal
from trading_agent.persistence.portfolio_store import PortfolioStore
from trading_agent.persistence.risk_state_store import RiskStateStore
from trading_agent.portfolio.state import PortfolioState
from trading_agent.risk.kill_switch import KillSwitch

HOST = "https://testnet.binance.vision"
INTERVAL = "4h"
STEP = interval_to_ms(INTERVAL)
START = 1_700_000_000_000


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


def _kline_rows(closes: list[float]) -> list[list]:
    return [_kline_row(START + i * STEP, c) for i, c in enumerate(closes)]


def _config(tmp_path, **risk_overrides) -> AppConfig:
    return AppConfig(
        mode="testnet",
        strategy={"ema_fast": 3, "ema_slow": 6},
        risk=risk_overrides,
        paths={"data_dir": str(tmp_path), "logs_dir": str(tmp_path), "db_path": str(tmp_path / "agent.db")},
    )


def _secrets() -> Secrets:
    return Secrets(testnet_api_key="k", testnet_api_secret="s")


def _mock_common(closes: list[float], server_time_ms: int | None = None):
    rows = _kline_rows(closes)
    server_time = server_time_ms if server_time_ms is not None else rows[-1][6] + 1
    responses.add(responses.GET, f"{HOST}/api/v3/time", json={"serverTime": server_time}, status=200)
    responses.add(responses.GET, f"{HOST}/api/v3/klines", json=rows, status=200)
    responses.add(
        responses.GET,
        f"{HOST}/api/v3/exchangeInfo",
        json=make_exchange_info(min_notional="1"),
        status=200,
    )
    return rows


def _seed_flat_portfolio(tmp_path, config, quote_balance="50"):
    with PortfolioStore(config.paths.db_path) as store:
        store.save("BTCUSDT", PortfolioState.initial(Decimal(quote_balance)), updated_at_ms=0)


@responses.activate
def test_hold_signal_produces_no_order(tmp_path):
    config = _config(tmp_path)
    _mock_common([100.0] * 8)
    _seed_flat_portfolio(tmp_path, config)
    with Journal(tmp_path / "journal.db") as journal, PortfolioStore(config.paths.db_path) as pstore, RiskStateStore(
        tmp_path / "risk.db"
    ) as rstore:
        result = run_testnet_cycle(config, _secrets(), journal, pstore, rstore)
    assert result.action == "HOLD"


@responses.activate
def test_buy_signal_places_order_and_updates_portfolio(tmp_path):
    config = _config(tmp_path)
    closes = [100, 99, 98, 97, 96, 95, 100]
    _mock_common(closes)
    _seed_flat_portfolio(tmp_path, config)
    responses.add(
        responses.POST,
        f"{HOST}/api/v3/order",
        json={
            "orderId": 1,
            "clientOrderId": "ta-x",
            "status": "FILLED",
            "executedQty": "0.0004",
            "cummulativeQuoteQty": "40.0",
            "transactTime": 1,
        },
        status=200,
    )
    with Journal(tmp_path / "journal.db") as journal, PortfolioStore(config.paths.db_path) as pstore, RiskStateStore(
        tmp_path / "risk.db"
    ) as rstore:
        result = run_testnet_cycle(config, _secrets(), journal, pstore, rstore)
        portfolio = pstore.load("BTCUSDT")
    assert result.action == "BUY"
    assert portfolio.base_balance == Decimal("0.0004")


@responses.activate
def test_kill_switch_blocks_trade(tmp_path):
    config = _config(tmp_path)
    closes = [100, 99, 98, 97, 96, 95, 100]
    _mock_common(closes)
    _seed_flat_portfolio(tmp_path, config)
    KillSwitch(config.paths.data_dir / "KILL_SWITCH").engage("manual test stop")
    with Journal(tmp_path / "journal.db") as journal, PortfolioStore(config.paths.db_path) as pstore, RiskStateStore(
        tmp_path / "risk.db"
    ) as rstore:
        result = run_testnet_cycle(config, _secrets(), journal, pstore, rstore)
    assert result.action == "NO_TRADE"
    assert result.reason_code == "KILL_SWITCH_ENGAGED"


@responses.activate
def test_stale_data_blocks_trade(tmp_path):
    config = _config(tmp_path, stale_data_max_age_seconds=60)
    closes = [100, 99, 98, 97, 96, 95, 100]
    rows = _kline_rows(closes)
    far_future_server_time = rows[-1][6] + 10_000_000
    _mock_common(closes, server_time_ms=far_future_server_time)
    _seed_flat_portfolio(tmp_path, config)
    with Journal(tmp_path / "journal.db") as journal, PortfolioStore(config.paths.db_path) as pstore, RiskStateStore(
        tmp_path / "risk.db"
    ) as rstore:
        result = run_testnet_cycle(config, _secrets(), journal, pstore, rstore)
    assert result.action == "NO_TRADE"
    assert result.reason_code == "DATA_UNAVAILABLE_OR_INVALID"


@responses.activate
def test_cold_start_with_nonzero_base_balance_refuses_to_guess(tmp_path):
    config = _config(tmp_path)
    closes = [100.0] * 8
    _mock_common(closes)
    responses.add(
        responses.GET,
        f"{HOST}/api/v3/account",
        json={"balances": [{"asset": "USDT", "free": "10"}, {"asset": "BTC", "free": "1"}]},
        status=200,
    )
    with (
        Journal(tmp_path / "journal.db") as journal,
        PortfolioStore(config.paths.db_path) as pstore,
        RiskStateStore(tmp_path / "risk.db") as rstore,
        pytest.raises(ColdStartReconciliationError),
    ):
        run_testnet_cycle(config, _secrets(), journal, pstore, rstore)


@responses.activate
def test_timeout_then_not_found_retries_and_succeeds(tmp_path):
    config = _config(tmp_path)
    closes = [100, 99, 98, 97, 96, 95, 100]
    _mock_common(closes)
    _seed_flat_portfolio(tmp_path, config)

    responses.add(responses.POST, f"{HOST}/api/v3/order", body=requests.exceptions.Timeout())
    responses.add(
        responses.GET,
        f"{HOST}/api/v3/order",
        json={"code": -2013, "msg": "Order does not exist."},
        status=400,
    )
    responses.add(
        responses.POST,
        f"{HOST}/api/v3/order",
        json={
            "orderId": 2,
            "clientOrderId": "ta-x",
            "status": "FILLED",
            "executedQty": "0.0004",
            "cummulativeQuoteQty": "40.0",
            "transactTime": 1,
        },
        status=200,
    )
    with Journal(tmp_path / "journal.db") as journal, PortfolioStore(config.paths.db_path) as pstore, RiskStateStore(
        tmp_path / "risk.db"
    ) as rstore:
        result = run_testnet_cycle(config, _secrets(), journal, pstore, rstore)
    assert result.action == "BUY"
    assert result.reason_code == "FILLED"


@responses.activate
def test_duplicate_order_already_journaled_blocks_resubmission(tmp_path):
    config = _config(tmp_path)
    closes = [100, 99, 98, 97, 96, 95, 100]
    _mock_common(closes)
    _seed_flat_portfolio(tmp_path, config)
    last_close_time_ms = _kline_rows(closes)[-1][6]

    with Journal(tmp_path / "journal.db") as journal:
        from trading_agent.execution.client_order_id import generate_client_order_id

        existing_id = generate_client_order_id("BTCUSDT", "BUY", last_close_time_ms)
        journal.record(
            "ORDER_SUBMITTED",
            {"client_order_id": existing_id, "side": "BUY", "quantity": "0.0004"},
            last_close_time_ms,
        )

    with Journal(tmp_path / "journal.db") as journal, PortfolioStore(config.paths.db_path) as pstore, RiskStateStore(
        tmp_path / "risk.db"
    ) as rstore:
        result = run_testnet_cycle(config, _secrets(), journal, pstore, rstore)
    assert result.action == "NO_TRADE"
    assert result.reason_code == "DUPLICATE_ORDER_BLOCKED"
