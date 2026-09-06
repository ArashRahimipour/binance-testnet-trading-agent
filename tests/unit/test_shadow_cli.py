from __future__ import annotations

import responses
import yaml
from click.testing import CliRunner

from tests.fixtures.klines import make_kline_series, make_stateful_klines_callback
from trading_agent.cli.main import cli
from trading_agent.data.models import interval_to_ms
from trading_agent.research.candidates.multitimeframe_breakout import MultiTimeframeBreakoutStrategy
from trading_agent.shadow.bootstrap import compute_warmup_candle_count
from trading_agent.shadow.boundary import SHADOW_START_BOUNDARY_MS

PROD_HOST = "https://api.binance.com"
INTERVAL = "1h"
STEP = interval_to_ms(INTERVAL)
WARMUP_CANDLE_COUNT = compute_warmup_candle_count(MultiTimeframeBreakoutStrategy().min_required_candles)
WARMUP_START_MS = SHADOW_START_BOUNDARY_MS - WARMUP_CANDLE_COUNT * STEP


def _write_shadow_config(tmp_path) -> str:
    config = {
        "mode": "shadow",
        "market": {"symbol": "BTCUSDT", "interval": INTERVAL},
        "risk": {"max_risk_per_trade_pct": 0.01},
        "backtest": {"starting_equity": 50.0},
        "paths": {
            "data_dir": str(tmp_path),
            "logs_dir": str(tmp_path),
            "db_path": str(tmp_path / "shadow_agent.db"),
        },
    }
    path = tmp_path / "shadow.yaml"
    path.write_text(yaml.safe_dump(config))
    return str(path)


def test_mode_choice_now_accepts_shadow():
    result = CliRunner().invoke(cli, ["--mode", "shadow", "config-check"])
    assert result.exit_code == 0, result.output
    assert "mode: shadow" in result.output


def test_mode_live_still_rejected_after_adding_shadow():
    result = CliRunner().invoke(cli, ["--mode", "live", "config-check"])
    assert result.exit_code != 0
    assert "Invalid value" in result.output or "invalid choice" in result.output.lower()


def test_shadow_run_requires_shadow_mode():
    result = CliRunner().invoke(cli, ["--mode", "backtest", "shadow-run"])
    assert result.exit_code != 0
    assert "requires --mode shadow" in result.output


def test_shadow_status_requires_shadow_mode():
    result = CliRunner().invoke(cli, ["--mode", "backtest", "shadow-status"])
    assert result.exit_code != 0
    assert "requires --mode shadow" in result.output


def test_shadow_report_requires_shadow_mode():
    result = CliRunner().invoke(cli, ["--mode", "backtest", "shadow-report"])
    assert result.exit_code != 0
    assert "requires --mode shadow" in result.output


@responses.activate
def test_shadow_run_short_circuits_on_kill_switch_with_no_network(tmp_path):
    config_path = _write_shadow_config(tmp_path)
    engage = CliRunner().invoke(cli, ["--config", config_path, "shadow-kill-switch", "engage", "--reason", "manual pause"])
    assert engage.exit_code == 0, engage.output
    assert "ENGAGED" in engage.output

    result = CliRunner().invoke(cli, ["--config", config_path, "shadow-run"])
    assert result.exit_code == 0, result.output
    assert "status=KILL_SWITCH_ENGAGED" in result.output
    assert "no order was placed" in result.output.lower()


def test_shadow_kill_switch_group_engage_status_disengage(tmp_path):
    config_path = _write_shadow_config(tmp_path)
    runner = CliRunner()

    status_before = runner.invoke(cli, ["--config", config_path, "shadow-kill-switch", "status"])
    assert status_before.exit_code == 0
    assert "disengaged" in status_before.output

    engage = runner.invoke(cli, ["--config", config_path, "shadow-kill-switch", "engage", "--reason", "audit"])
    assert engage.exit_code == 0
    assert "audit" in engage.output

    status_after = runner.invoke(cli, ["--config", config_path, "shadow-kill-switch", "status"])
    assert "ENGAGED" in status_after.output
    assert "audit" in status_after.output

    disengage = runner.invoke(cli, ["--config", config_path, "shadow-kill-switch", "disengage"])
    assert disengage.exit_code == 0
    assert "DISENGAGED" in disengage.output

    status_final = runner.invoke(cli, ["--config", config_path, "shadow-kill-switch", "status"])
    assert "disengaged" in status_final.output


def test_shadow_kill_switch_is_independent_of_testnet_kill_switch(tmp_path):
    config_path = _write_shadow_config(tmp_path)
    runner = CliRunner()
    runner.invoke(cli, ["--config", config_path, "shadow-kill-switch", "engage", "--reason", "x"])

    testnet_status = runner.invoke(cli, ["--config", config_path, "kill-switch", "status"])
    assert testnet_status.exit_code == 0
    assert "disengaged" in testnet_status.output


def test_shadow_status_and_report_work_on_a_fresh_empty_store(tmp_path):
    config_path = _write_shadow_config(tmp_path)
    runner = CliRunner()

    status = runner.invoke(cli, ["--config", config_path, "shadow-status"])
    assert status.exit_code == 0, status.output
    assert "shadow_start_boundary: 2026-09-06T00:00:00Z" in status.output
    assert "bootstrapped: NO" in status.output
    assert "closed_trades: 0" in status.output
    assert "open_position: none" in status.output

    report = runner.invoke(cli, ["--config", config_path, "shadow-report"])
    assert report.exit_code == 0, report.output
    assert "bootstrapped: NO" in report.output
    assert "SHADOW SIMULATION" in report.output
    assert "NOT yet eligible: 0 of 30" in report.output


def test_shadow_bootstrap_requires_shadow_mode():
    result = CliRunner().invoke(cli, ["--mode", "backtest", "shadow-bootstrap"])
    assert result.exit_code != 0
    assert "requires --mode shadow" in result.output


@responses.activate
def test_shadow_run_refuses_before_bootstrap_with_no_network_call(tmp_path):
    config_path = _write_shadow_config(tmp_path)
    result = CliRunner().invoke(cli, ["--config", config_path, "shadow-run"])
    assert result.exit_code == 0, result.output
    assert "status=NOT_BOOTSTRAPPED" in result.output
    assert "shadow-bootstrap" in result.output


@responses.activate
def test_shadow_bootstrap_cli_happy_path_and_status_report_reflect_it(tmp_path):
    config_path = _write_shadow_config(tmp_path)
    rows = make_kline_series(WARMUP_START_MS, INTERVAL, WARMUP_CANDLE_COUNT)
    responses.add_callback(responses.GET, f"{PROD_HOST}/api/v3/klines", callback=make_stateful_klines_callback(rows))
    runner = CliRunner()

    first = runner.invoke(cli, ["--config", config_path, "shadow-bootstrap"])
    assert first.exit_code == 0, first.output
    assert "status=OK" in first.output
    assert f"warmup_candle_count={WARMUP_CANDLE_COUNT}" in first.output

    second = runner.invoke(cli, ["--config", config_path, "shadow-bootstrap"])
    assert second.exit_code == 0, second.output
    assert "status=ALREADY_BOOTSTRAPPED" in second.output

    status = runner.invoke(cli, ["--config", config_path, "shadow-status"])
    assert status.exit_code == 0, status.output
    assert "bootstrapped: yes" in status.output
    assert f"{WARMUP_CANDLE_COUNT} warm-up candle(s)" in status.output

    report = runner.invoke(cli, ["--config", config_path, "shadow-report"])
    assert report.exit_code == 0, report.output
    assert "bootstrapped: yes" in report.output
