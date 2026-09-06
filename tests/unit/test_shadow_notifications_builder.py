"""Proofs for `shadow/notifications/builder.py`'s pure message builders -
no I/O, no network, no database: every test here just calls a function and
inspects the returned string / tuple.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.fixtures.exchange_info import make_exchange_info
from trading_agent.config.models import AppConfig
from trading_agent.metrics.performance import (
    EXIT_REASON_STOP_LOSS,
    EXIT_REASON_STRATEGY,
    EXIT_REASON_TAKE_PROFIT,
    PerformanceReport,
    Trade,
)
from trading_agent.shadow.notifications.builder import (
    NO_REAL_ORDER_NOTE,
    build_daily_summary_message,
    build_entry_message,
    build_exit_message,
    build_test_message,
    compute_realized_plan_for_display,
    compute_trade_stats_to_date,
)
from trading_agent.shadow.report import (
    ShadowDataGapSummary,
    ShadowReport,
)
from trading_agent.shadow.store import ShadowRunState, ShadowTradeRecord
from trading_agent.sizing.exchange_filters import SymbolFilters


def _config(**overrides) -> AppConfig:
    stop_loss_overrides = overrides.pop("stop_loss", {"stop_distance_pct": 0.05})
    fees_overrides = overrides.pop("fees", {"taker_fee_pct": 0.001, "slippage_pct": 0.0005})
    return AppConfig(mode="backtest", stop_loss=stop_loss_overrides, fees=fees_overrides, **overrides)


def _filters() -> SymbolFilters:
    return SymbolFilters.from_exchange_info(make_exchange_info(min_notional="0.01"))


def _trade(**overrides) -> Trade:
    defaults = {
        "entry_time_ms": 1_000,
        "exit_time_ms": 2_000,
        "entry_price": Decimal(100),
        "exit_price": Decimal(110),
        "quantity": Decimal(1),
        "fees_paid": Decimal("0.2"),
        "pnl_quote": Decimal("9.8"),
        "exit_reason": EXIT_REASON_STRATEGY,
        "entry_fee_quote": Decimal("0.1"),
        "exit_fee_quote": Decimal("0.1"),
        "entry_reference_price": Decimal("99.9"),
        "exit_reference_price": Decimal("110.1"),
    }
    defaults.update(overrides)
    return Trade(**defaults)


# --- compute_realized_plan_for_display -------------------------------------


def test_compute_realized_plan_for_display_reproduces_stop_and_target():
    config = _config()
    filters = _filters()
    entry_price = Decimal(100)
    quantity = Decimal(1)
    equity = Decimal(1000)
    plan = compute_realized_plan_for_display(entry_price, quantity, equity, config, filters)
    assert plan.approved is True
    # Stop is a straight percentage off the entry price - deterministic.
    expected_stop = entry_price * (1 - Decimal(str(config.stop_loss.stop_distance_pct)))
    assert plan.stop_price == expected_stop
    assert plan.target_price is not None
    assert plan.target_price > entry_price
    # The fixed R/R policy targets a >= 2.0 net reward/risk.
    assert plan.net_reward_to_risk is not None
    assert plan.net_reward_to_risk >= 1.99


def test_compute_realized_plan_for_display_is_deterministic():
    config = _config()
    filters = _filters()
    a = compute_realized_plan_for_display(Decimal(250), Decimal(2), Decimal(500), config, filters)
    b = compute_realized_plan_for_display(Decimal(250), Decimal(2), Decimal(500), config, filters)
    assert a == b


# --- build_entry_message ----------------------------------------------------


def test_build_entry_message_contains_all_required_fields():
    config = _config()
    filters = _filters()
    equity_before = Decimal(50)
    entry_price = Decimal(260)
    quantity = Decimal("0.03")
    plan = compute_realized_plan_for_display(entry_price, quantity, equity_before, config, filters)
    message = build_entry_message(
        event_id="entry:123456",
        symbol="BTCUSDT",
        signal_time_ms=123455,
        entry_time_ms=123456,
        entry_price=entry_price,
        entry_reference_price=Decimal("259.9"),
        quantity=quantity,
        entry_fee_quote=Decimal("0.01"),
        equity_before_entry=equity_before,
        realized_plan=plan,
        signal_reason_code="MULTITIMEFRAME_1H_CONFIRMED_ENTRY",
        signal_inputs={"governing_breakout_level": 252.5},
        config=config,
    )
    assert "SHADOW ENTRY" in message
    assert "Event ID: entry:123456" in message
    assert "Symbol: BTCUSDT" in message
    assert "Side: BUY" in message
    assert "Signal timestamp (ms): 123455" in message
    assert "Hypothetical fill timestamp (ms): 123456" in message
    assert "Stop-loss price" in message
    assert "Take-profit price" in message
    assert "Maximum planned loss" in message
    assert "Planned net profit" in message
    assert "Net reward/risk after costs" in message
    assert "Entry fee (actual)" in message
    assert "Exit fee (estimated at take-profit)" in message
    assert "Entry slippage (actual)" in message
    assert "Exit slippage (estimated at take-profit)" in message
    assert "Weekly regime" in message
    assert "4h setup" in message
    assert "252.5" in message
    assert "1h confirmation: MULTITIMEFRAME_1H_CONFIRMED_ENTRY" in message
    assert "Simulated equity before this entry" in message
    assert NO_REAL_ORDER_NOTE in message
    assert "NO REAL OR TESTNET ORDER WAS PLACED" in message


def test_build_entry_message_falls_back_when_no_breakout_level_given():
    config = _config()
    filters = _filters()
    plan = compute_realized_plan_for_display(Decimal(100), Decimal(1), Decimal(500), config, filters)
    message = build_entry_message(
        event_id="entry:1", symbol="BTCUSDT", signal_time_ms=0, entry_time_ms=1,
        entry_price=Decimal(100), entry_reference_price=Decimal(100), quantity=Decimal(1),
        entry_fee_quote=Decimal(0), equity_before_entry=Decimal(500), realized_plan=plan,
        signal_reason_code="X", signal_inputs={}, config=config,
    )
    assert "4h Donchian breakout" in message


# --- build_exit_message ------------------------------------------------------


def test_build_exit_message_contains_all_required_fields():
    trade = _trade(exit_reason=EXIT_REASON_TAKE_PROFIT)
    message = build_exit_message(
        event_id="exit:2000",
        symbol="BTCUSDT",
        trade=trade,
        planned_risk_quote=Decimal("2.0"),
        updated_equity=Decimal("59.8"),
        closed_trade_count=3,
        win_rate_pct=66.6,
        expectancy_quote=1.5,
        expectancy_r=0.8,
    )
    assert "SHADOW EXIT" in message
    assert "Event ID: exit:2000" in message
    assert "Exit reason: TAKE-PROFIT" in message
    assert "Entry timestamp (ms): 1000" in message
    assert "Exit timestamp (ms): 2000" in message
    assert "Quantity: 1" in message
    assert "Holding duration" in message
    assert "Gross PnL" in message
    assert "Fees paid" in message
    assert "Slippage cost" in message
    assert "Net PnL" in message
    assert "Realized R-multiple: 4.90R" in message  # 9.8 / 2.0
    assert "Updated simulated equity" in message
    assert "Closed trade count (to date): 3" in message
    assert "Win rate (to date): 66.60%" in message
    assert "Expectancy (to date)" in message
    assert NO_REAL_ORDER_NOTE in message


def test_build_exit_message_labels_stop_loss_and_strategy_exit_correctly():
    stop_msg = build_exit_message(
        event_id="e1", symbol="BTCUSDT", trade=_trade(exit_reason=EXIT_REASON_STOP_LOSS),
        planned_risk_quote=None, updated_equity=Decimal(50), closed_trade_count=1,
        win_rate_pct=None, expectancy_quote=None, expectancy_r=None,
    )
    assert "Exit reason: STOP-LOSS" in stop_msg
    assert "Realized R-multiple: n/a" in stop_msg
    assert "Win rate (to date): n/a" in stop_msg

    strategy_msg = build_exit_message(
        event_id="e2", symbol="BTCUSDT", trade=_trade(exit_reason=EXIT_REASON_STRATEGY),
        planned_risk_quote=None, updated_equity=Decimal(50), closed_trade_count=1,
        win_rate_pct=100.0, expectancy_quote=9.8, expectancy_r=None,
    )
    assert "Exit reason: STRATEGY EXIT" in strategy_msg


def test_gross_pnl_recovers_correctly_from_net_pnl_plus_fees():
    # Trade.pnl_quote is NET of both entry and exit fees - gross must be
    # pnl_quote + fees_paid, never approximated a different way.
    trade = _trade(pnl_quote=Decimal("9.8"), fees_paid=Decimal("0.2"))
    message = build_exit_message(
        event_id="e", symbol="BTCUSDT", trade=trade, planned_risk_quote=None,
        updated_equity=Decimal(50), closed_trade_count=1, win_rate_pct=None,
        expectancy_quote=None, expectancy_r=None,
    )
    assert "Gross PnL: $10.0000" in message


# --- compute_trade_stats_to_date --------------------------------------------


def test_compute_trade_stats_to_date_empty():
    assert compute_trade_stats_to_date([]) == (0, None, None, None)


def test_compute_trade_stats_to_date_matches_manual_calculation():
    records = [
        ShadowTradeRecord(trade=_trade(pnl_quote=Decimal(10)), planned_risk_quote=Decimal(5), net_reward_to_risk=2.0),
        ShadowTradeRecord(trade=_trade(pnl_quote=Decimal(-5)), planned_risk_quote=Decimal(5), net_reward_to_risk=2.0),
        ShadowTradeRecord(trade=_trade(pnl_quote=Decimal(10)), planned_risk_quote=None, net_reward_to_risk=None),
    ]
    count, win_rate, expectancy_quote, expectancy_r = compute_trade_stats_to_date(records)
    assert count == 3
    assert win_rate == pytest.approx((2 / 3) * 100)
    assert expectancy_quote == pytest.approx(float((Decimal(10) - Decimal(5) + Decimal(10)) / 3))
    # Only the first two trades have a positive planned_risk_quote.
    assert expectancy_r == pytest.approx((2.0 + (-1.0)) / 2)


# --- build_daily_summary_message --------------------------------------------


def _shadow_report(**overrides) -> ShadowReport:
    performance = PerformanceReport(
        trade_count=5, total_return_pct=10.0, annualized_return_pct=None, max_drawdown_pct=3.0,
        volatility_pct=None, sharpe_ratio=None, sortino_ratio=None, win_rate=60.0, profit_factor=1.5,
        avg_win_quote=5.0, avg_loss_quote=-2.0, exposure_pct=50.0, turnover=1.0,
        buy_and_hold_return_pct=None, low_trade_count_warning=True, starting_equity=Decimal(50),
        ending_equity=Decimal(55), buy_and_hold=None, assumptions={},
    )
    data_gaps = ShadowDataGapSummary(
        stored_candle_count=1000, gap_count=0, total_missing_intervals=0,
        latest_segment_length=1000, min_required_candles=500, effective_min_required_candles=600,
    )
    run_state = ShadowRunState(
        last_processed_close_time_ms=123456789, last_run_at_ms=123456999, total_cycles=42,
        last_segment_length=1000, last_cycle_status="OK", last_cycle_detail="ok", open_position=None,
    )
    defaults = {
        "run_state": run_state, "bootstrap": None, "performance": performance, "expectancy_r": 0.5,
        "expectancy_quote": 1.1, "total_fees_paid_quote": Decimal("2.0"), "total_slippage_cost_quote": Decimal("0.5"),
        "longest_losing_streak": 2, "open_position": None, "data_gaps": data_gaps,
        "promotion_review_eligible": False, "promotion_review_note": "NOT yet eligible: 5 of 30",
    }
    defaults.update(overrides)
    return ShadowReport(**defaults)


def test_build_daily_summary_message_contains_required_fields():
    report = _shadow_report()
    message = build_daily_summary_message("2026-03-01", report)
    assert "SHADOW DAILY SUMMARY" in message
    assert "2026-03-01" in message
    assert "Australia/Melbourne" in message
    assert "Last cycle status: OK" in message
    assert "Simulated equity: $55.0000" in message
    assert "Open position: none" in message
    assert "Closed trades: 5" in message
    assert "Win rate: 60.00%" in message
    assert "Expectancy" in message
    assert "Max drawdown: 3.00%" in message
    assert "Data gaps: 0 confirmed" in message
    assert "Last processed candle close (ms): 123456789" in message
    assert "Promotion progress" in message
    assert "NOT yet eligible" in message


# --- build_test_message ------------------------------------------------------


def test_build_test_message_is_clearly_labelled_and_never_a_real_event():
    message = build_test_message()
    assert "TEST NOTIFICATION" in message
    assert "does not describe any real trading event" in message
    assert NO_REAL_ORDER_NOTE in message
