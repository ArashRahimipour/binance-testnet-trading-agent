from decimal import Decimal

from trading_agent.metrics.performance import EquityPoint, Trade, compute_performance_report

INTERVAL = "4h"
STEP_MS = 4 * 60 * 60 * 1000
START = 1_700_000_000_000


def _equity_curve(values: list[float], in_position_flags: list[bool] | None = None) -> list[EquityPoint]:
    flags = in_position_flags or [False] * len(values)
    return [
        EquityPoint(timestamp_ms=START + i * STEP_MS, equity=Decimal(str(v)), in_position=flags[i])
        for i, v in enumerate(values)
    ]


def test_empty_equity_curve_returns_zeroed_report_with_warning():
    report = compute_performance_report([], [], INTERVAL, min_trades_for_significance=5)
    assert report.trade_count == 0
    assert report.low_trade_count_warning is True


def test_total_return_pct_matches_simple_growth():
    curve = _equity_curve([100, 110, 121])
    report = compute_performance_report([], curve, INTERVAL, min_trades_for_significance=1)
    assert abs(report.total_return_pct - 21.0) < 1e-6


def test_max_drawdown_pct_detects_peak_to_trough():
    curve = _equity_curve([100, 120, 90, 130])
    report = compute_performance_report([], curve, INTERVAL, min_trades_for_significance=1)
    # peak 120 -> trough 90 = 25% drawdown
    assert abs(report.max_drawdown_pct - 25.0) < 1e-6


def test_exposure_pct_reflects_in_position_fraction():
    curve = _equity_curve([100, 100, 100, 100], in_position_flags=[True, True, False, False])
    report = compute_performance_report([], curve, INTERVAL, min_trades_for_significance=1)
    assert abs(report.exposure_pct - 50.0) < 1e-6


def test_win_rate_and_profit_factor():
    trades = [
        Trade(START, START + STEP_MS, Decimal(100), Decimal(110), Decimal(1), Decimal(0), Decimal(10)),
        Trade(START, START + STEP_MS, Decimal(100), Decimal(90), Decimal(1), Decimal(0), Decimal(-10)),
        Trade(START, START + STEP_MS, Decimal(100), Decimal(120), Decimal(1), Decimal(0), Decimal(20)),
    ]
    curve = _equity_curve([100, 110, 100, 120])
    report = compute_performance_report(trades, curve, INTERVAL, min_trades_for_significance=1)
    assert abs(report.win_rate - (2 / 3 * 100)) < 1e-6
    assert abs(report.profit_factor - 3.0) < 1e-6  # gross profit 30 / gross loss 10
    assert report.avg_win_quote == 15.0
    assert report.avg_loss_quote == -10.0


def test_low_trade_count_warning_threshold():
    trades = [
        Trade(START, START + STEP_MS, Decimal(100), Decimal(110), Decimal(1), Decimal(0), Decimal(10))
    ]
    curve = _equity_curve([100, 110])
    below = compute_performance_report(trades, curve, INTERVAL, min_trades_for_significance=5)
    at_threshold = compute_performance_report(trades, curve, INTERVAL, min_trades_for_significance=1)
    assert below.low_trade_count_warning is True
    assert at_threshold.low_trade_count_warning is False


def test_buy_and_hold_return_passed_through():
    curve = _equity_curve([100, 105])
    report = compute_performance_report([], curve, INTERVAL, min_trades_for_significance=1, buy_and_hold_return_pct=42.0)
    assert report.buy_and_hold_return_pct == 42.0


def test_assumptions_documented_in_report():
    curve = _equity_curve([100, 105])
    report = compute_performance_report([], curve, INTERVAL, min_trades_for_significance=1)
    assert report.assumptions["risk_free_rate"] == 0.0
    assert report.assumptions["periods_per_year"] > 0
