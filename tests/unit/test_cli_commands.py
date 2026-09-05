import json
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import responses
import yaml
from click.testing import CliRunner

from tests.fixtures.exchange_info import make_exchange_info
from tests.fixtures.klines import make_kline_row, make_kline_series
from trading_agent.cli.main import cli
from trading_agent.data.gap_detection import GapRecord
from trading_agent.data.models import Candle, interval_to_ms
from trading_agent.data.storage import CandleStore
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
    assert "--- overall (continuous run) ---" in result.output
    assert "INDEPENDENT FIXED-PARAMETER HOLDOUT EVALUATION" in result.output
    assert "not a claim of live profitability" in result.output


@responses.activate
def test_research_backtest_command_end_to_end(tmp_path):
    config_path = _write_config(tmp_path)
    # 60 candles: enough for the frozen baseline's ema_slow=50 warm-up
    # (51) and for most declared candidates' own warm-up requirements.
    rows = make_kline_series(START, INTERVAL, 60)
    responses.add(responses.GET, f"{PROD_HOST}/api/v3/time", json={"serverTime": rows[-1][6] + 1}, status=200)
    responses.add(responses.GET, f"{PROD_HOST}/api/v3/klines", json=rows, status=200)
    fetch_result = CliRunner().invoke(cli, ["--config", config_path, "fetch-data", "--limit", "60"])
    assert fetch_result.exit_code == 0, fetch_result.output

    responses.add(
        responses.GET, f"{PROD_HOST}/api/v3/exchangeInfo", json=make_exchange_info(min_notional="1"), status=200,
    )
    result = CliRunner().invoke(cli, ["--config", config_path, "research-backtest"])
    assert result.exit_code == 0, result.output
    assert "research cutoff: 2025-05-16T00:00:00Z" in result.output
    assert "FROZEN BASELINE (ema_crossover_v0_1_rejected)" in result.output
    assert "9 declared configurations" in result.output
    assert "SCORECARD" in result.output
    assert "candidates were evaluated together" in result.output
    assert "REJECTED, RESEARCH_SURVIVOR, and INSUFFICIENT_EVIDENCE" in result.output
    # every candidate id from the registry must appear (nothing hidden)
    for spec_id in ("trend_regime_A1", "breakout_B1", "mean_reversion_C1"):
        assert spec_id in result.output


def test_research_backtest_command_requires_backtest_mode(tmp_path):
    config_path = _write_config(tmp_path)
    result = CliRunner().invoke(cli, ["--config", config_path, "--mode", "testnet", "research-backtest"])
    assert result.exit_code != 0
    assert "requires --mode backtest" in result.output


@responses.activate
def test_research_postmortem_command_end_to_end(tmp_path):
    config_path = _write_config(tmp_path)
    rows = make_kline_series(START, INTERVAL, 60)
    responses.add(responses.GET, f"{PROD_HOST}/api/v3/time", json={"serverTime": rows[-1][6] + 1}, status=200)
    responses.add(responses.GET, f"{PROD_HOST}/api/v3/klines", json=rows, status=200)
    fetch_result = CliRunner().invoke(cli, ["--config", config_path, "fetch-data", "--limit", "60"])
    assert fetch_result.exit_code == 0, fetch_result.output

    responses.add(
        responses.GET, f"{PROD_HOST}/api/v3/exchangeInfo", json=make_exchange_info(min_notional="1"), status=200,
    )
    result = CliRunner().invoke(cli, ["--config", config_path, "research-postmortem"])
    assert result.exit_code == 0, result.output
    assert "research cutoff: 2025-05-16T00:00:00Z" in result.output
    assert "never ranks, sorts by performance" in result.output
    assert "SUM of PnL across INDEPENDENTLY RESTARTED $50 blocks" in result.output
    for spec_id in ("trend_regime_A1", "breakout_B1", "mean_reversion_C1"):
        assert spec_id in result.output
    assert "DIAGNOSIS:" in result.output
    assert "not a claim of profitability and not approval for live or Testnet trading" in result.output


def test_research_postmortem_command_requires_backtest_mode(tmp_path):
    config_path = _write_config(tmp_path)
    result = CliRunner().invoke(cli, ["--config", config_path, "--mode", "testnet", "research-postmortem"])
    assert result.exit_code != 0
    assert "requires --mode backtest" in result.output


@responses.activate
def test_research_sensitivity_command_end_to_end(tmp_path):
    config_path = _write_config(tmp_path)
    rows = make_kline_series(START, INTERVAL, 60)
    responses.add(responses.GET, f"{PROD_HOST}/api/v3/time", json={"serverTime": rows[-1][6] + 1}, status=200)
    responses.add(responses.GET, f"{PROD_HOST}/api/v3/klines", json=rows, status=200)
    fetch_result = CliRunner().invoke(cli, ["--config", config_path, "fetch-data", "--limit", "60"])
    assert fetch_result.exit_code == 0, fetch_result.output

    responses.add(
        responses.GET, f"{PROD_HOST}/api/v3/exchangeInfo", json=make_exchange_info(min_notional="1"), status=200,
    )
    result = CliRunner().invoke(cli, ["--config", config_path, "research-sensitivity"])
    assert result.exit_code == 0, result.output
    assert "research cutoff: 2025-05-16T00:00:00Z" in result.output
    assert "round_1_original_evaluation" in result.output
    assert "duration_normalized_sensitivity" in result.output
    assert "NON-BINDING" in result.output
    for spec_id in ("trend_regime_A1", "breakout_B1", "mean_reversion_C1"):
        assert spec_id in result.output
    assert "never changed by this command" in result.output


def test_research_sensitivity_command_requires_backtest_mode(tmp_path):
    config_path = _write_config(tmp_path)
    result = CliRunner().invoke(cli, ["--config", config_path, "--mode", "testnet", "research-sensitivity"])
    assert result.exit_code != 0
    assert "requires --mode backtest" in result.output


@responses.activate
def test_research_round2_command_end_to_end(tmp_path):
    config_path = _write_config(tmp_path)
    # 250 candles clears D1's own EMA200+slope warm-up requirement so the
    # command exercises real signal generation, though still short of a
    # full 365-day duration block (reported as a fragment, not silently
    # dropped).
    rows = make_kline_series(START, INTERVAL, 250)
    responses.add(responses.GET, f"{PROD_HOST}/api/v3/time", json={"serverTime": rows[-1][6] + 1}, status=200)
    responses.add(responses.GET, f"{PROD_HOST}/api/v3/klines", json=rows, status=200)
    fetch_result = CliRunner().invoke(cli, ["--config", config_path, "fetch-data", "--limit", "250"])
    assert fetch_result.exit_code == 0, fetch_result.output

    responses.add(
        responses.GET, f"{PROD_HOST}/api/v3/exchangeInfo", json=make_exchange_info(min_notional="1"), status=200,
    )
    result = CliRunner().invoke(cli, ["--config", config_path, "research-round2"])
    assert result.exit_code == 0, result.output
    assert "research cutoff: 2025-05-16T00:00:00Z" in result.output
    assert "breakout_regime_D1_round2" in result.output
    assert "ROUND 2 HYPOTHESIS" in result.output
    assert "cumulative_candidate_configurations_examined=10" in result.output
    assert "NOT AN UNTOUCHED TEST" in result.output
    assert "breakout_B1 on IDENTICAL dates" in result.output
    assert "not a claim of profitability and not approval for live or Testnet trading" in result.output


def test_research_round2_command_requires_backtest_mode(tmp_path):
    config_path = _write_config(tmp_path)
    result = CliRunner().invoke(cli, ["--config", config_path, "--mode", "testnet", "research-round2"])
    assert result.exit_code != 0
    assert "requires --mode backtest" in result.output


def _write_config_1h(tmp_path) -> str:
    config = {
        "mode": "backtest",
        "market": {"symbol": "BTCUSDT", "interval": "1h"},
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
def test_research_round3_command_end_to_end(tmp_path):
    config_path = _write_config_1h(tmp_path)
    # 500 1h candles - far short of E1's own ~7564-hour weekly-EMA warm-up
    # (reported as a fragment, not silently dropped), but enough to prove
    # the command reads interval="1h" rows and reports E1's structure.
    rows = make_kline_series(START, "1h", 500)
    responses.add(responses.GET, f"{PROD_HOST}/api/v3/time", json={"serverTime": rows[-1][6] + 1}, status=200)
    responses.add(responses.GET, f"{PROD_HOST}/api/v3/klines", json=rows, status=200)
    fetch_result = CliRunner().invoke(cli, ["--config", config_path, "fetch-data", "--limit", "500"])
    assert fetch_result.exit_code == 0, fetch_result.output

    responses.add(
        responses.GET, f"{PROD_HOST}/api/v3/exchangeInfo", json=make_exchange_info(min_notional="1"), status=200,
    )
    result = CliRunner().invoke(cli, ["--config", config_path, "research-round3"])
    assert result.exit_code == 0, result.output
    assert "research cutoff: 2025-05-16T00:00:00Z" in result.output
    assert "multitimeframe_breakout_E1_round3" in result.output
    assert "ROUND 3 HYPOTHESIS" in result.output
    assert "cumulative_candidate_configurations_examined=11" in result.output
    assert "NOT AN UNTOUCHED TEST" in result.output
    assert "OFFICIAL REJECTED" in result.output
    assert "INSUFFICIENT-DURATION FRAGMENT" in result.output
    assert "not a claim of profitability and not approval for live or Testnet trading" in result.output


def test_research_round3_command_requires_backtest_mode(tmp_path):
    config_path = _write_config_1h(tmp_path)
    result = CliRunner().invoke(cli, ["--config", config_path, "--mode", "testnet", "research-round3"])
    assert result.exit_code != 0
    assert "requires --mode backtest" in result.output


def test_research_round3_command_errors_without_1h_data(tmp_path):
    # A config/database that only ever had 4h candles fetched - research-
    # round3 must fail closed (not silently fall back to 4h data).
    config_path = _write_config(tmp_path)  # 4h config
    result = CliRunner().invoke(cli, ["--config", config_path, "research-round3"])
    assert result.exit_code != 0
    assert "1h" in result.output


def _seed_1h_gap(db_path, existing_open_times: list[int], gap: GapRecord) -> None:
    step = interval_to_ms("1h")
    candles = [
        Candle(
            symbol="BTCUSDT", interval="1h", open_time_ms=t, close_time_ms=t + step - 1,
            open=Decimal(100), high=Decimal(101), low=Decimal(99), close=Decimal(100), volume=Decimal(1),
        )
        for t in existing_open_times
    ]
    with CandleStore(db_path) as store:
        store.upsert_candles(candles)
        store.store_candles_and_gaps([], [gap], "BTCUSDT", "1h", detected_at_ms=1)


def _gap_recovery_klines_callback(minute_rows: dict[int, list]):
    """Serves 1m rows for the reconstruction fetch; anything else
    (including the native-1h cross-check) returns empty, matching the
    real expected shape of a genuinely confirmed gap."""

    def _callback(request):
        query = parse_qs(urlparse(request.url).query)
        interval = query.get("interval", [None])[0]
        if interval != "1m":
            return (200, {"Content-Type": "application/json"}, json.dumps([]))
        start = int(query["startTime"][0]) if "startTime" in query else min(minute_rows)
        end = int(query["endTime"][0]) if "endTime" in query else max(minute_rows)
        rows = [row for t, row in sorted(minute_rows.items()) if start <= t <= end]
        return (200, {"Content-Type": "application/json"}, json.dumps(rows))

    return _callback


@responses.activate
def test_research_gap_audit_command_is_read_only_and_reports_a_recoverable_hour(tmp_path):
    config_path = _write_config_1h(tmp_path)
    db_path = tmp_path / "agent.db"
    step = interval_to_ms("1h")
    minute_step = interval_to_ms("1m")
    gap_hour = START + step
    gap = GapRecord(expected_open_time_ms=gap_hour, previous_open_time_ms=START, next_open_time_ms=START + 2 * step, missing_intervals=1)
    _seed_1h_gap(db_path, [START, START + 2 * step], gap)

    minute_rows = {gap_hour + i * minute_step: make_kline_row(gap_hour + i * minute_step, "1m") for i in range(60)}
    responses.add_callback(responses.GET, f"{PROD_HOST}/api/v3/klines", callback=_gap_recovery_klines_callback(minute_rows))

    result = CliRunner().invoke(cli, ["--config", config_path, "research-gap-audit"])
    assert result.exit_code == 0, result.output
    assert "total_confirmed_gaps=1" in result.output
    assert "fully_recoverable_hours=1" in result.output
    assert "FULLY_RECOVERABLE" in result.output
    assert "READ-ONLY audit - nothing was stored" in result.output
    assert "Round-3" in result.output

    with CandleStore(db_path) as store:
        assert len(store.get_candles("BTCUSDT", "1h")) == 2  # untouched
        assert len(store.get_gaps("BTCUSDT", "1h")) == 1  # untouched


def test_research_gap_audit_command_requires_backtest_mode(tmp_path):
    config_path = _write_config_1h(tmp_path)
    result = CliRunner().invoke(cli, ["--config", config_path, "--mode", "testnet", "research-gap-audit"])
    assert result.exit_code != 0
    assert "requires --mode backtest" in result.output


def test_research_gap_audit_command_requires_1h_interval(tmp_path):
    config_path = _write_config(tmp_path)  # 4h config
    result = CliRunner().invoke(cli, ["--config", config_path, "research-gap-audit"])
    assert result.exit_code != 0
    assert "1h" in result.output


def test_research_gap_audit_command_with_no_confirmed_gaps(tmp_path):
    config_path = _write_config_1h(tmp_path)
    db_path = tmp_path / "agent.db"
    step = interval_to_ms("1h")
    with CandleStore(db_path) as store:
        store.upsert_candles(
            [
                Candle(
                    symbol="BTCUSDT", interval="1h", open_time_ms=START, close_time_ms=START + step - 1,
                    open=Decimal(100), high=Decimal(101), low=Decimal(99), close=Decimal(100), volume=Decimal(1),
                )
            ]
        )
    result = CliRunner().invoke(cli, ["--config", config_path, "research-gap-audit"])
    assert result.exit_code == 0, result.output
    assert "nothing to audit" in result.output


@responses.activate
def test_research_gap_recover_without_confirm_stores_nothing(tmp_path):
    config_path = _write_config_1h(tmp_path)
    db_path = tmp_path / "agent.db"
    step = interval_to_ms("1h")
    minute_step = interval_to_ms("1m")
    gap_hour = START + step
    gap = GapRecord(expected_open_time_ms=gap_hour, previous_open_time_ms=START, next_open_time_ms=START + 2 * step, missing_intervals=1)
    _seed_1h_gap(db_path, [START, START + 2 * step], gap)

    minute_rows = {gap_hour + i * minute_step: make_kline_row(gap_hour + i * minute_step, "1m") for i in range(60)}
    responses.add_callback(responses.GET, f"{PROD_HOST}/api/v3/klines", callback=_gap_recovery_klines_callback(minute_rows))

    result = CliRunner().invoke(cli, ["--config", config_path, "research-gap-recover"])
    assert result.exit_code == 0, result.output
    assert "No --confirm flag given - NOTHING was stored" in result.output

    with CandleStore(db_path) as store:
        assert len(store.get_candles("BTCUSDT", "1h")) == 2
        assert len(store.get_gaps("BTCUSDT", "1h")) == 1


@responses.activate
def test_research_gap_recover_confirm_stores_the_recovered_candle_atomically(tmp_path):
    config_path = _write_config_1h(tmp_path)
    db_path = tmp_path / "agent.db"
    step = interval_to_ms("1h")
    minute_step = interval_to_ms("1m")
    gap_hour = START + step
    gap = GapRecord(expected_open_time_ms=gap_hour, previous_open_time_ms=START, next_open_time_ms=START + 2 * step, missing_intervals=1)
    _seed_1h_gap(db_path, [START, START + 2 * step], gap)

    minute_rows = {gap_hour + i * minute_step: make_kline_row(gap_hour + i * minute_step, "1m") for i in range(60)}
    responses.add_callback(responses.GET, f"{PROD_HOST}/api/v3/klines", callback=_gap_recovery_klines_callback(minute_rows))

    result = CliRunner().invoke(cli, ["--config", config_path, "research-gap-recover", "--confirm"])
    assert result.exit_code == 0, result.output
    assert "STORED 1 recovered candle(s)" in result.output
    assert "0 confirmed 1h gap(s) remain" in result.output

    with CandleStore(db_path) as store:
        candles = store.get_candles("BTCUSDT", "1h")
        assert [c.open_time_ms for c in candles] == [START, gap_hour, START + 2 * step]
        assert store.get_gaps("BTCUSDT", "1h") == []


def test_research_gap_recover_command_requires_backtest_mode(tmp_path):
    config_path = _write_config_1h(tmp_path)
    result = CliRunner().invoke(cli, ["--config", config_path, "--mode", "testnet", "research-gap-recover", "--confirm"])
    assert result.exit_code != 0
    assert "requires --mode backtest" in result.output


def test_research_gap_recover_command_requires_1h_interval(tmp_path):
    config_path = _write_config(tmp_path)  # 4h config
    result = CliRunner().invoke(cli, ["--config", config_path, "research-gap-recover", "--confirm"])
    assert result.exit_code != 0
    assert "1h" in result.output


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
