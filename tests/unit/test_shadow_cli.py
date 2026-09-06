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


def _write_shadow_config(tmp_path, **overrides) -> str:
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
    config.update(overrides)
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


# --- Telegram / notification CLI commands -----------------------------------

TELEGRAM_BOT_TOKEN = "777777:AACliTestTokenNeverReal"
TELEGRAM_CHAT_ID = "88"
TELEGRAM_SEND_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"


def test_telegram_config_check_requires_shadow_mode():
    result = CliRunner().invoke(cli, ["--mode", "backtest", "telegram-config-check"])
    assert result.exit_code != 0
    assert "requires --mode shadow" in result.output


def test_telegram_config_check_reports_disabled_by_default_and_never_leaks_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID)
    config_path = _write_shadow_config(tmp_path)
    result = CliRunner().invoke(cli, ["--config", config_path, "telegram-config-check"])
    assert result.exit_code == 0, result.output
    assert "telegram.enabled (config/shadow.yaml): False" in result.output
    assert "status: INACTIVE" in result.output
    assert "set (redacted)" in result.output
    assert TELEGRAM_BOT_TOKEN not in result.output
    assert TELEGRAM_CHAT_ID not in result.output


def test_telegram_config_check_reports_missing_secrets(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    config_path = _write_shadow_config(tmp_path, telegram={"enabled": True})
    result = CliRunner().invoke(cli, ["--config", config_path, "telegram-config-check"])
    assert result.exit_code == 0, result.output
    assert "TELEGRAM_BOT_TOKEN: NOT SET" in result.output
    assert "TELEGRAM_CHAT_ID: NOT SET" in result.output
    assert "status: INACTIVE" in result.output


def test_telegram_config_check_reports_active_when_fully_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID)
    config_path = _write_shadow_config(tmp_path, telegram={"enabled": True})
    result = CliRunner().invoke(cli, ["--config", config_path, "telegram-config-check"])
    assert result.exit_code == 0, result.output
    assert "status: ACTIVE" in result.output


def test_telegram_test_requires_confirm(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID)
    config_path = _write_shadow_config(tmp_path, telegram={"enabled": True})
    result = CliRunner().invoke(cli, ["--config", config_path, "telegram-test"])
    assert result.exit_code != 0
    assert "--confirm" in result.output


def test_telegram_test_refuses_when_inactive_with_zero_network_calls(tmp_path):
    config_path = _write_shadow_config(tmp_path)  # telegram.enabled defaults to False
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        result = CliRunner().invoke(cli, ["--config", config_path, "telegram-test", "--confirm"])
        assert len(mocked.calls) == 0
    assert result.exit_code != 0
    assert "not active" in result.output


@responses.activate
def test_telegram_test_confirm_sends_a_clearly_labelled_test_message(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID)
    responses.add(responses.POST, TELEGRAM_SEND_URL, json={"ok": True}, status=200)
    config_path = _write_shadow_config(tmp_path, telegram={"enabled": True})
    result = CliRunner().invoke(cli, ["--config", config_path, "telegram-test", "--confirm"])
    assert result.exit_code == 0, result.output
    assert "outcome=SENT" in result.output
    sent_body = responses.calls[0].request.body
    assert b"TEST NOTIFICATION" in sent_body
    assert b"does not describe any real trading event" in sent_body


def test_notification_status_on_empty_outbox(tmp_path):
    config_path = _write_shadow_config(tmp_path)
    result = CliRunner().invoke(cli, ["--config", config_path, "notification-status"])
    assert result.exit_code == 0, result.output
    assert "no notifications in the outbox" in result.output


def test_notification_disable_then_enable_round_trip(tmp_path):
    config_path = _write_shadow_config(tmp_path, telegram={"enabled": True})
    runner = CliRunner()

    disable = runner.invoke(cli, ["--config", config_path, "notification-disable", "--reason", "maintenance"])
    assert disable.exit_code == 0, disable.output
    assert "DISABLED" in disable.output

    check = runner.invoke(cli, ["--config", config_path, "telegram-config-check"])
    assert "ENGAGED" in check.output
    assert "maintenance" in check.output

    enable = runner.invoke(cli, ["--config", config_path, "notification-enable"])
    assert enable.exit_code == 0, enable.output
    assert "ENABLED" in enable.output

    check_again = runner.invoke(cli, ["--config", config_path, "telegram-config-check"])
    assert "disengaged" in check_again.output


def test_notification_retry_requires_confirm(tmp_path):
    config_path = _write_shadow_config(tmp_path, telegram={"enabled": True})
    result = CliRunner().invoke(
        cli, ["--config", config_path, "notification-retry", "--event-id", "entry:1"]
    )
    assert result.exit_code != 0
    assert "--confirm" in result.output
    assert "DUPLICATE" in result.output


def test_notification_retry_unknown_event_id_fails_cleanly(tmp_path):
    config_path = _write_shadow_config(tmp_path, telegram={"enabled": True})
    result = CliRunner().invoke(
        cli, ["--config", config_path, "notification-retry", "--event-id", "entry:999", "--confirm"]
    )
    assert result.exit_code != 0
    assert "no notification found" in result.output


@responses.activate
def test_notification_status_and_retry_round_trip_via_a_real_shadow_cycle(tmp_path, monkeypatch):
    """Drive a real shadow-run cycle to produce an entry notification via
    an AMBIGUOUS Telegram read-timeout, then manually retry it via the
    CLI - proving `notification-status` and `notification-retry` operate
    on the exact same on-disk outbox `shadow-run` writes to."""
    import requests

    from tests.fixtures.exchange_info import make_exchange_info
    from tests.unit.test_shadow_engine import (
        BOUNDARY_IDX,
        OFFICIAL_EVAL_IDX,
        WARMUP_CANDLE_COUNT,
        _direct_bootstrap,
        _full_candles,
    )
    from trading_agent.config.loader import load_config
    from trading_agent.data.storage import CandleStore

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID)
    config_path = _write_shadow_config(tmp_path, telegram={"enabled": True})
    config = load_config(config_path)

    kick_at = BOUNDARY_IDX + 3
    n_total = OFFICIAL_EVAL_IDX + 5
    all_candles = _full_candles(n_total, kick_at=kick_at)
    _direct_bootstrap(config, all_candles[:WARMUP_CANDLE_COUNT])
    with CandleStore(config.paths.db_path) as candle_store:
        candle_store.upsert_candles(all_candles[WARMUP_CANDLE_COUNT:])

    responses.add(
        responses.GET, f"{PROD_HOST}/api/v3/time", json={"serverTime": all_candles[-1].close_time_ms + 1}, status=200
    )
    responses.add(responses.GET, f"{PROD_HOST}/api/v3/klines", json=[], status=200)
    responses.add(responses.GET, f"{PROD_HOST}/api/v3/exchangeInfo", json=make_exchange_info(min_notional="0.01"), status=200)
    responses.add(responses.POST, TELEGRAM_SEND_URL, body=requests.exceptions.ReadTimeout("timed out"))

    runner = CliRunner()
    run_result = runner.invoke(cli, ["--config", config_path, "shadow-run"])
    assert run_result.exit_code == 0, run_result.output

    status = runner.invoke(cli, ["--config", config_path, "notification-status"])
    assert status.exit_code == 0, status.output
    assert "AMBIGUOUS" in status.output
    assert TELEGRAM_BOT_TOKEN not in status.output

    import re

    event_id_match = re.search(r"(entry:\d+)", status.output)
    assert event_id_match is not None
    event_id = event_id_match.group(1)

    responses.replace(responses.POST, TELEGRAM_SEND_URL, json={"ok": True}, status=200)
    retry = runner.invoke(
        cli, ["--config", config_path, "notification-retry", "--event-id", event_id, "--confirm"]
    )
    assert retry.exit_code == 0, retry.output
    assert "outcome: SENT" in retry.output

    # Assert on the SPECIFIC retried event's own line, not the whole
    # output - a daily-summary notification may ALSO have fired this
    # cycle (depending on the real wall-clock Melbourne time the test
    # happens to run at) and is irrelevant to what this test proves.
    final_status = runner.invoke(cli, ["--config", config_path, "notification-status"])
    event_lines = [line for line in final_status.output.splitlines() if line.startswith(event_id)]
    assert len(event_lines) == 1
    assert "status=SENT" in event_lines[0]
