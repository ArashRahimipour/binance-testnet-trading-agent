"""Proofs for the extended diagnostic/reporting round: accounting identity,
PnL breakdown, evidence-backed explanations, time-based performance,
trade-distribution diagnostics, deterministic bootstrap, rolling-window
diagnostics, the already-consumed test-window warning, and (via the
backtest engine) that gap segmentation, zero/one-trade windows, and an
ending open position are all handled without ever inventing a liquidation.
"""

from decimal import Decimal

from tests.fixtures.exchange_info import make_exchange_info
from trading_agent.backtest.engine import run_backtest, run_independent_holdout_evaluation
from trading_agent.config.models import AppConfig
from trading_agent.data.models import Candle, interval_to_ms
from trading_agent.metrics.diagnostics import OpenPositionInfo, ShutdownActivation
from trading_agent.metrics.extended_report import (
    compute_accounting_identity,
    compute_bootstrap_confidence_interval,
    compute_pnl_breakdown,
    compute_rolling_window_diagnostics,
    compute_time_based_performance,
    compute_trade_distribution,
    explain_window,
)
from trading_agent.metrics.performance import EquityPoint, Trade
from trading_agent.sizing.exchange_filters import SymbolFilters

INTERVAL = "4h"
STEP = interval_to_ms(INTERVAL)
START = 1_577_836_800_000  # 2020-01-01T00:00:00Z


def _trade(
    entry_time_ms: int,
    exit_time_ms: int,
    pnl_quote: Decimal,
    entry_price: Decimal = Decimal(100),
    exit_price: Decimal = Decimal(110),
    quantity: Decimal = Decimal(1),
    entry_fee_quote: Decimal = Decimal("0.1"),
    exit_fee_quote: Decimal = Decimal("0.1"),
    entry_reference_price: Decimal | None = None,
    exit_reference_price: Decimal | None = None,
) -> Trade:
    return Trade(
        entry_time_ms=entry_time_ms,
        exit_time_ms=exit_time_ms,
        entry_price=entry_price,
        exit_price=exit_price,
        quantity=quantity,
        fees_paid=entry_fee_quote + exit_fee_quote,
        pnl_quote=pnl_quote,
        entry_fee_quote=entry_fee_quote,
        exit_fee_quote=exit_fee_quote,
        entry_reference_price=entry_reference_price if entry_reference_price is not None else entry_price,
        exit_reference_price=exit_reference_price if exit_reference_price is not None else exit_price,
    )


# --- 1. Accounting identity. ---


def test_accounting_identity_holds_when_consistent():
    result = compute_accounting_identity(
        ending_cash_quote=Decimal(10), ending_base_quantity=Decimal(2), final_mark_price=Decimal(20),
        reported_ending_equity=Decimal(50),
    )
    assert result.computed_ending_equity == Decimal(50)
    assert result.identity_holds is True
    assert result.difference_quote == 0


def test_accounting_identity_flags_a_mismatch():
    result = compute_accounting_identity(
        ending_cash_quote=Decimal(10), ending_base_quantity=Decimal(2), final_mark_price=Decimal(20),
        reported_ending_equity=Decimal(999),
    )
    assert result.identity_holds is False
    assert result.difference_quote == Decimal(50) - Decimal(999)


# --- 2. PnL breakdown. ---


def test_pnl_breakdown_realized_only_no_open_position():
    trades = [_trade(START, START + STEP, Decimal(5)), _trade(START + STEP, START + 2 * STEP, Decimal(-2))]
    breakdown = compute_pnl_breakdown(trades, open_position=None, final_mark_price=Decimal(100))
    assert breakdown.realized_closed_trade_pnl_quote == Decimal(3)
    assert breakdown.unrealized_open_position_pnl_quote is None
    assert breakdown.total_marked_to_market_pnl_quote == Decimal(3)
    assert breakdown.entry_fees_total_quote == Decimal("0.2")
    assert breakdown.exit_fees_total_quote == Decimal("0.2")
    assert breakdown.open_position_note is None


def test_pnl_breakdown_with_open_position_never_invents_an_exit():
    open_position = OpenPositionInfo(
        entry_time_ms=START, entry_price=Decimal(100), entry_reference_price=Decimal(99),
        quantity=Decimal(2), entry_fee_quote=Decimal("0.5"),
    )
    breakdown = compute_pnl_breakdown([], open_position=open_position, final_mark_price=Decimal(120))
    # (120 - 100) * 2 - 0.5 entry fee = 39.5, no exit fee/slippage ever assumed.
    assert breakdown.unrealized_open_position_pnl_quote == Decimal("39.5")
    assert breakdown.total_marked_to_market_pnl_quote == Decimal("39.5")
    assert breakdown.entry_fees_total_quote == Decimal("0.5")
    assert breakdown.exit_fees_total_quote == Decimal(0)
    assert breakdown.open_position_note is not None
    assert "NOT liquidated or closed" in breakdown.open_position_note


def test_pnl_breakdown_slippage_cost_computed_from_reference_prices():
    trade = _trade(
        START, START + STEP, Decimal(1), entry_price=Decimal(101), entry_reference_price=Decimal(100),
        exit_price=Decimal(109), exit_reference_price=Decimal(110), quantity=Decimal(3),
    )
    breakdown = compute_pnl_breakdown([trade], open_position=None, final_mark_price=Decimal(100))
    # entry slippage: |101-100|*3 = 3; exit slippage: |109-110|*3 = 3 -> total 6
    assert breakdown.slippage_cost_total_quote == Decimal(6)


def test_pnl_breakdown_fees_are_always_estimated_in_a_backtest():
    breakdown = compute_pnl_breakdown([], open_position=None, final_mark_price=Decimal(1))
    assert breakdown.fees_are_estimated is True
    assert "SIMULATED ESTIMATES" in breakdown.fees_note


# --- 3. Explanations. ---


def test_explain_window_open_position_reason_present_when_open():
    open_position = OpenPositionInfo(START, Decimal(100), Decimal(100), Decimal(1), Decimal(0))
    explanation = explain_window([], executed_entries=1, open_position=open_position, shutdown_activations={}, rejected_entries_by_reason={}, window_end_time_ms=START + 10 * STEP, buy_signals_generated=1)
    assert explanation.open_position_reason is not None
    assert "still open" in explanation.open_position_reason


def test_explain_window_entries_exceed_closed_trades_when_position_still_open():
    open_position = OpenPositionInfo(START, Decimal(100), Decimal(100), Decimal(1), Decimal(0))
    trades = [_trade(START, START + STEP, Decimal(1))]
    explanation = explain_window(trades, executed_entries=2, open_position=open_position, shutdown_activations={}, rejected_entries_by_reason={}, window_end_time_ms=START + 10 * STEP, buy_signals_generated=2)
    assert "exceeds closed_trade_count" in explanation.entries_vs_closed_trades_note
    assert "not a counting defect" in explanation.entries_vs_closed_trades_note


def test_explain_window_names_the_latched_shutdown_as_why_trading_stopped():
    activation = ShutdownActivation(
        reason_code="MAX_DRAWDOWN_SHUTDOWN", first_activated_time_ms=START + 5 * STEP,
        equity_at_activation=Decimal(42), drawdown_pct_at_activation=0.163,
        last_active_time_ms=START + 20 * STEP, blocked_buy_count=59,
        remained_latched_to_end=True, duration_ms=15 * STEP,
    )
    explanation = explain_window(
        [_trade(START, START + STEP, Decimal(-1))], executed_entries=1, open_position=None,
        shutdown_activations={"MAX_DRAWDOWN_SHUTDOWN": activation}, rejected_entries_by_reason={"MAX_DRAWDOWN_SHUTDOWN": 59},
        window_end_time_ms=START + 100 * STEP, buy_signals_generated=60,
    )
    assert explanation.trading_stopped_reason is not None
    assert "MAX_DRAWDOWN_SHUTDOWN" in explanation.trading_stopped_reason
    assert "59" in explanation.trading_stopped_reason
    assert "remained continuously latched" in explanation.trading_stopped_reason


def test_explain_window_notes_strategy_silence_when_no_shutdown_latched():
    trades = [_trade(START, START + STEP, Decimal(1))]
    explanation = explain_window(
        trades, executed_entries=1, open_position=None, shutdown_activations={}, rejected_entries_by_reason={},
        window_end_time_ms=START + 100 * STEP, buy_signals_generated=1,
    )
    assert explanation.trading_stopped_reason is not None
    assert "no further BUY signal" in explanation.trading_stopped_reason
    assert "not a risk-engine block" in explanation.trading_stopped_reason


# --- 4. Time-based performance. ---


def _equity_curve_monthly() -> list[EquityPoint]:
    # Jan 1 -> 100, Jan 31 -> 110 (+10% in Jan), Feb 28 -> 121 (+10% in Feb).
    jan1 = int(__import__("datetime").datetime(2020, 1, 1, tzinfo=__import__("datetime").UTC).timestamp() * 1000)
    jan31 = int(__import__("datetime").datetime(2020, 1, 31, tzinfo=__import__("datetime").UTC).timestamp() * 1000)
    feb28 = int(__import__("datetime").datetime(2020, 2, 28, tzinfo=__import__("datetime").UTC).timestamp() * 1000)
    return [
        EquityPoint(timestamp_ms=jan1, equity=Decimal(100), in_position=False),
        EquityPoint(timestamp_ms=jan31, equity=Decimal(110), in_position=False),
        EquityPoint(timestamp_ms=feb28, equity=Decimal(121), in_position=False),
    ]


def test_monthly_returns_grouped_by_calendar_month():
    tb = compute_time_based_performance(_equity_curve_monthly(), total_return_pct=21.0, annualized_return_pct=None, max_drawdown_pct=0.0, exposure_pct=50.0)
    assert [(m.year, m.month) for m in tb.monthly_returns] == [(2020, 1), (2020, 2)]
    assert round(tb.monthly_returns[0].return_pct, 4) == 10.0
    assert round(tb.monthly_returns[1].return_pct, 4) == 10.0
    assert tb.positive_months_pct == 100.0


def test_longest_underwater_period_identified():
    curve = [
        EquityPoint(timestamp_ms=START, equity=Decimal(100), in_position=False),
        EquityPoint(timestamp_ms=START + STEP, equity=Decimal(90), in_position=False),  # underwater starts
        EquityPoint(timestamp_ms=START + 2 * STEP, equity=Decimal(80), in_position=False),
        EquityPoint(timestamp_ms=START + 3 * STEP, equity=Decimal(105), in_position=False),  # recovers -> ends underwater
        EquityPoint(timestamp_ms=START + 4 * STEP, equity=Decimal(95), in_position=False),  # briefly underwater again
        EquityPoint(timestamp_ms=START + 5 * STEP, equity=Decimal(106), in_position=False),  # recovers again, shorter
    ]
    tb = compute_time_based_performance(curve, total_return_pct=6.0, annualized_return_pct=None, max_drawdown_pct=20.0, exposure_pct=0.0)
    # Measured from the PEAK itself (START, equity=100) through to the
    # first point that makes a new high again (START + 3*STEP, equity=105)
    # - the standard definition of an underwater/drawdown period.
    assert tb.longest_underwater_start_ms == START
    assert tb.longest_underwater_end_ms == START + 3 * STEP
    assert tb.longest_underwater_days == (3 * STEP) / (24 * 60 * 60 * 1000)


def test_exposure_adjusted_return_and_calmar():
    tb = compute_time_based_performance([], total_return_pct=20.0, annualized_return_pct=40.0, max_drawdown_pct=10.0, exposure_pct=50.0)
    assert tb.exposure_adjusted_return_pct == 40.0  # 20 / (50/100)
    assert tb.calmar_ratio == 4.0  # 40 / 10

    zero_exposure = compute_time_based_performance([], total_return_pct=20.0, annualized_return_pct=40.0, max_drawdown_pct=0.0, exposure_pct=0.0)
    assert zero_exposure.exposure_adjusted_return_pct is None
    assert zero_exposure.calmar_ratio is None  # division by zero max_drawdown avoided


# --- 5. Trade distribution. ---


def test_trade_distribution_none_when_zero_trades():
    assert compute_trade_distribution([], starting_equity=Decimal(50)) is None


def test_trade_distribution_winners_losers_and_best_trade_contribution():
    trades = [
        _trade(START, START + STEP, Decimal(10)),
        _trade(START + STEP, START + 2 * STEP, Decimal(-4)),
        _trade(START + 2 * STEP, START + 3 * STEP, Decimal(2)),
    ]
    dist = compute_trade_distribution(trades, starting_equity=Decimal(50))
    assert dist is not None
    assert dist.trade_count == 3
    assert dist.average_winner_quote == 6.0  # (10+2)/2
    assert dist.average_loser_quote == -4.0
    assert dist.largest_winner_quote == 10.0
    assert dist.largest_loser_quote == -4.0
    assert dist.best_trade_pnl_quote == Decimal(10)
    # total realized = 8, best trade contributes 10/8*100 = 125%
    assert round(dist.best_trade_contribution_pct, 4) == 125.0
    assert dist.total_pnl_excluding_best_trade_quote == Decimal(-2)
    assert dist.return_pct_excluding_best_trade == Decimal(-2) / Decimal(50) * 100


def test_trade_distribution_consecutive_streaks():
    pnls = [1, 1, -1, 1, -1, -1, -1, 1]
    trades = [_trade(START + i * STEP, START + (i + 1) * STEP, Decimal(p)) for i, p in enumerate(pnls)]
    dist = compute_trade_distribution(trades, starting_equity=Decimal(50))
    assert dist is not None
    assert dist.max_consecutive_wins == 2
    assert dist.max_consecutive_losses == 3


def test_trade_distribution_holding_period_hours():
    trades = [
        _trade(START, START + STEP, Decimal(1)),  # 4h
        _trade(START, START + 3 * STEP, Decimal(1)),  # 12h
    ]
    dist = compute_trade_distribution(trades, starting_equity=Decimal(50))
    assert dist is not None
    hp = dist.holding_period
    assert hp.min_hours == 4.0
    assert hp.max_hours == 12.0
    assert hp.median_hours == 8.0
    assert hp.mean_hours == 8.0


# --- 6. Deterministic bootstrap. ---


def test_bootstrap_is_deterministic_across_repeated_calls():
    trades = [_trade(START + i * STEP, START + (i + 1) * STEP, Decimal(v)) for i, v in enumerate([3, -1, 2, -2, 4])]
    first = compute_bootstrap_confidence_interval(trades, starting_equity=Decimal(50))
    second = compute_bootstrap_confidence_interval(trades, starting_equity=Decimal(50))
    assert first == second


def test_bootstrap_none_when_fewer_than_two_trades():
    result = compute_bootstrap_confidence_interval([_trade(START, START + STEP, Decimal(1))], starting_equity=Decimal(50))
    assert result.mean_total_return_pct is None
    assert result.ci_low_pct is None
    assert result.ci_high_pct is None


def test_bootstrap_caveat_is_prominent_and_never_omitted():
    trades = [_trade(START + i * STEP, START + (i + 1) * STEP, Decimal(v)) for i, v in enumerate([1, -1])]
    result = compute_bootstrap_confidence_interval(trades, starting_equity=Decimal(50))
    assert "does not" in result.caveat.lower() or "does not preserve" in result.caveat.lower()
    assert "NOT evidence" in result.caveat
    assert "regime" in result.caveat.lower()


# --- 7. Rolling-window diagnostics: diagnostic only, never ranks/selects. ---


def test_rolling_windows_are_chronological_groups_never_reordered():
    trades = [_trade(START + i * STEP, START + (i + 1) * STEP, Decimal(1 if i % 2 == 0 else -1)) for i in range(25)]
    rolling = compute_rolling_window_diagnostics(trades, starting_equity=Decimal(50), trades_per_window=10)
    assert [w.window_index for w in rolling.windows] == [0, 1, 2]
    assert rolling.windows[0].trade_count == 10
    assert rolling.windows[2].trade_count == 5
    # Chronological, not sorted by return - window 0's start time must precede window 1's.
    assert rolling.windows[0].start_time_ms < rolling.windows[1].start_time_ms
    assert "never ranked" in rolling.caveat


def test_rolling_windows_empty_when_no_trades():
    rolling = compute_rolling_window_diagnostics([], starting_equity=Decimal(50))
    assert rolling.windows == []


# --- Integration: engine wiring, gap segmentation, zero/one-trade windows, no invented liquidation. ---

INTERVAL_ = "4h"


def _zigzag_closes(segment_len: int, num_segments: int, base: float, amplitude: float) -> list[float]:
    closes: list[float] = []
    price = base
    direction = 1
    for _ in range(num_segments):
        for _ in range(segment_len):
            price += direction * amplitude
            closes.append(price)
        direction *= -1
    return closes


def _candles(closes: list[float], start: int = START) -> list[Candle]:
    return [
        Candle(
            symbol="BTCUSDT", interval=INTERVAL, open_time_ms=start + i * STEP, close_time_ms=start + i * STEP + STEP - 1,
            open=Decimal(str(close)), high=Decimal(str(close + 5)), low=Decimal(str(close - 5)),
            close=Decimal(str(close)), volume=Decimal(1),
        )
        for i, close in enumerate(closes)
    ]


def _config(**overrides) -> AppConfig:
    risk_overrides = overrides.pop("risk", {})
    backtest_overrides = {"train_fraction": 0.5, "validation_fraction": 0.25, "test_fraction": 0.25, "min_trades_for_significance": 3}
    backtest_overrides.update(overrides.pop("backtest", {}))
    return AppConfig(mode="backtest", strategy={"ema_fast": 3, "ema_slow": 6}, backtest=backtest_overrides, risk=risk_overrides, **overrides)


def _filters() -> SymbolFilters:
    return SymbolFilters.from_exchange_info(make_exchange_info(min_notional="1"))


def test_extended_diagnostics_present_per_segment_across_a_gap():
    config = _config()
    segment0 = _candles(_zigzag_closes(20, 20, 30000.0, 400.0))
    gap_start = segment0[-1].open_time_ms + 2 * STEP
    segment1 = _candles(_zigzag_closes(20, 10, 30000.0, 400.0), start=gap_start)
    result = run_backtest(segment0 + segment1, config, _filters())
    assert len(result.gaps) == 1
    for seg in result.segments:
        assert seg.extended is not None
        assert seg.extended.accounting.identity_holds is True


def test_zero_trade_window_extended_diagnostics_handled_gracefully():
    # A single flat candle series generates no signals at all.
    candles = _candles([100.0] * 30)
    config = _config()
    holdout = run_independent_holdout_evaluation(candles, config, _filters())
    for window in holdout.windows:
        assert window.performance.trade_count == 0
        assert window.extended.trade_distribution is None
        assert window.extended.bootstrap.mean_total_return_pct is None
        assert window.extended.accounting.identity_holds is True


def test_one_trade_window_extended_diagnostics_handled_gracefully():
    config = _config(risk={"max_drawdown_pct": 0.0001})
    candles = _candles(_zigzag_closes(20, 20, 30000.0, 400.0))
    result = run_backtest(candles, config, _filters())
    assert result.reports["train"].trade_count == 1
    extended = result.extended_reports["train"]
    assert extended.trade_distribution is not None
    assert extended.trade_distribution.trade_count == 1
    assert extended.bootstrap.mean_total_return_pct is None  # fewer than 2 trades


def test_no_invented_liquidation_for_an_ending_open_position():
    config = _config()
    closes = _zigzag_closes(20, 15, 30000.0, 400.0) + [40000 + i * 50 for i in range(20)]
    candles = _candles(closes)
    holdout = run_independent_holdout_evaluation(candles, config, _filters())
    open_windows = [w for w in holdout.windows if w.ends_with_open_position]
    assert open_windows, "expected at least one window to end with an open position for this fixture"
    for window in open_windows:
        assert window.open_position is not None
        # The still-open entry executed but was never force-closed into a
        # synthetic Trade - closed_trade_count is exactly one less than
        # executed_entries, and only an unrealized (never a realized) PnL
        # reflects it.
        assert window.performance.trade_count == window.diagnostics.executed_entries - 1
        assert window.extended.pnl_breakdown.unrealized_open_position_pnl_quote is not None
        assert window.extended.accounting.identity_holds is True
        base_qty_from_open_position = window.open_position.quantity
        assert window.diagnostics.ending_base_quantity == base_qty_from_open_position


def test_already_consumed_warning_only_on_test_window():
    config = _config()
    candles = _candles(_zigzag_closes(20, 20, 30000.0, 400.0))
    holdout = run_independent_holdout_evaluation(candles, config, _filters())
    by_label = {w.label: w for w in holdout.windows}
    assert by_label["train"].extended.already_consumed_warning is None
    assert by_label["validation"].extended.already_consumed_warning is None
    assert by_label["test"].extended.already_consumed_warning is not None
    assert "must NOT be treated as an untouched final holdout" in by_label["test"].extended.already_consumed_warning


def test_scope_note_present_on_continuous_splits_but_not_overall():
    config = _config()
    candles = _candles(_zigzag_closes(20, 20, 30000.0, 400.0))
    result = run_backtest(candles, config, _filters())
    assert result.extended_reports["train"].scope_note is not None
    assert result.extended_reports["validation"].scope_note is not None
    assert result.extended_reports["test"].scope_note is not None
    assert result.extended_reports["overall"].scope_note is None
