from decimal import Decimal

import responses
import yaml
from click.testing import CliRunner

from tests.fixtures.exchange_info import make_exchange_info
from tests.fixtures.klines import make_kline_series
from trading_agent.cli.main import cli
from trading_agent.data.models import interval_to_ms
from trading_agent.persistence.execution_store import ExecutionStateStore
from trading_agent.portfolio.state import PortfolioState

PROD_HOST = "https://api.binance.com"
TESTNET_HOST = "https://testnet.binance.vision"
INTERVAL = "4h"
STEP = interval_to_ms(INTERVAL)
START = 1_700_000_000_000


def _write_config(tmp_path) -> str:
    config = {
        "mode": "backtest",
        "market": {"symbol": "BTCUSDT", "interval": INTERVAL},
        "strategy": {"ema_fast": 3, "ema_slow": 6},
        "paths": {
            "data_dir": str(tmp_path),
            "logs_dir": str(tmp_path),
            "db_path": str(tmp_path / "agent.db"),
        },
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config))
    return str(path)


@responses.activate
def test_fetch_data_stores_candles(tmp_path):
    config_path = _write_config(tmp_path)
    rows = make_kline_series(START, INTERVAL, 20)
    responses.add(responses.GET, f"{PROD_HOST}/api/v3/time", json={"serverTime": rows[-1][6] + 1}, status=200)
    responses.add(responses.GET, f"{PROD_HOST}/api/v3/klines", json=rows, status=200)

    result = CliRunner().invoke(cli, ["--config", config_path, "fetch-data"])
    assert result.exit_code == 0, result.output
    assert "Stored 20 completed candles" in result.output


@responses.activate
def test_fetch_data_with_date_range_uses_paginated_path(tmp_path):
    # A page shorter than the (large, default) page limit ends pagination
    # after one request - full multi-page continuation is covered at the
    # unit level in test_historical_fetch.py with a configurable page size.
    # This proves --start/--end is correctly wired to the paginated fetch.
    config_path = _write_config(tmp_path)
    rows = make_kline_series(START, INTERVAL, 5)
    responses.add(responses.GET, f"{PROD_HOST}/api/v3/time", json={"serverTime": rows[-1][6] + 1}, status=200)
    responses.add(responses.GET, f"{PROD_HOST}/api/v3/klines", json=rows, status=200)

    result = CliRunner().invoke(
        cli,
        [
            "--config", config_path, "fetch-data",
            "--start", "2023-11-14", "--end", "2023-11-16",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Stored 5 completed candles" in result.output


@responses.activate
def test_backtest_command_end_to_end(tmp_path):
    config_path = _write_config(tmp_path)
    rows = make_kline_series(START, INTERVAL, 20)
    responses.add(responses.GET, f"{PROD_HOST}/api/v3/time", json={"serverTime": rows[-1][6] + 1}, status=200)
    responses.add(responses.GET, f"{PROD_HOST}/api/v3/klines", json=rows, status=200)
    fetch_result = CliRunner().invoke(cli, ["--config", config_path, "fetch-data"])
    assert fetch_result.exit_code == 0, fetch_result.output

    responses.add(
        responses.GET,
        f"{PROD_HOST}/api/v3/exchangeInfo",
        json=make_exchange_info(min_notional="1"),
        status=200,
    )
    result = CliRunner().invoke(cli, ["--config", config_path, "backtest"])
    assert result.exit_code == 0, result.output
    assert "--- overall ---" in result.output
    assert "not a claim of live profitability" in result.output


def test_backtest_command_requires_backtest_mode(tmp_path):
    config_path = _write_config(tmp_path)
    result = CliRunner().invoke(cli, ["--config", config_path, "--mode", "testnet", "backtest"])
    assert result.exit_code != 0
    assert "requires --mode backtest" in result.output


def test_backtest_command_without_data_errors(tmp_path):
    config_path = _write_config(tmp_path)
    result = CliRunner().invoke(cli, ["--config", config_path, "backtest"])
    assert result.exit_code != 0
    assert "fetch-data" in result.output


def test_kill_switch_engage_disengage_status(tmp_path):
    config_path = _write_config(tmp_path)
    runner = CliRunner()

    status = runner.invoke(cli, ["--config", config_path, "kill-switch", "status"])
    assert "disengaged" in status.output

    engage = runner.invoke(cli, ["--config", config_path, "kill-switch", "engage", "--reason", "testing"])
    assert engage.exit_code == 0
    assert "ENGAGED" in engage.output

    status_after = runner.invoke(cli, ["--config", config_path, "kill-switch", "status"])
    assert "ENGAGED" in status_after.output
    assert "testing" in status_after.output

    disengage = runner.invoke(cli, ["--config", config_path, "kill-switch", "disengage"])
    assert disengage.exit_code == 0

    status_final = runner.invoke(cli, ["--config", config_path, "kill-switch", "status"])
    assert "disengaged" in status_final.output


def test_status_command_shows_uninitialized_portfolio(tmp_path):
    config_path = _write_config(tmp_path)
    result = CliRunner().invoke(cli, ["--config", config_path, "status"])
    assert result.exit_code == 0
    assert "not initialized" in result.output


def test_status_command_shows_seeded_portfolio(tmp_path):
    config_path = _write_config(tmp_path)
    db_path = tmp_path / "agent.db"
    with ExecutionStateStore(db_path) as store:
        store.save_portfolio("BTCUSDT", PortfolioState.initial(Decimal(50)), updated_at_ms=0)
    result = CliRunner().invoke(cli, ["--config", config_path, "status"])
    assert result.exit_code == 0
    assert "quote_balance=50" in result.output


def test_run_command_requires_testnet_mode(tmp_path):
    config_path = _write_config(tmp_path)
    result = CliRunner().invoke(cli, ["--config", config_path, "run"])
    assert result.exit_code != 0
    assert "requires --mode testnet" in result.output


def test_run_command_fails_without_credentials(tmp_path, monkeypatch):
    monkeypatch.delenv("BINANCE_TESTNET_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_TESTNET_API_SECRET", raising=False)
    config_path = _write_config(tmp_path)
    result = CliRunner().invoke(cli, ["--config", config_path, "--mode", "testnet", "run"])
    assert result.exit_code != 0
    assert "Failed to load testnet credentials" in result.output
