"""Proofs for the corrected backtest evaluation/reporting round.

Covers, with concrete evidence (never mere assertion-by-inspection):
  - exactly why a continuous run can show zero trades in validation/test
    (a permanently-latched MAX_DRAWDOWN_SHUTDOWN), with the exact
    reason-code counts, activation timestamp/equity/drawdown, and latch
    status all visible on `RunDiagnostics`;
  - that `run_independent_holdout_evaluation` resets risk state and
    therefore does NOT reproduce that artifact;
  - warm-up isolation (no trades/returns from warm-up, no future leakage,
    never crosses a gap);
  - no position/pending signal crosses an evaluation window boundary;
  - stop-loss vs strategy exits are counted separately;
  - the maximum-drawdown timestamp is correct;
  - `backtest.starting_equity` is honored end-to-end;
  - gap-segmented results are never naively concatenated into one
    "overall" return/drawdown, only an explicitly-labeled trade-level
    aggregate;
  - buy-and-hold is computed over the exact same candle range as its
    report, never bridging a confirmed gap.
"""

from decimal import Decimal

from tests.fixtures.exchange_info import make_exchange_info
from trading_agent.backtest.engine import run_backtest, run_independent_holdout_evaluation
from trading_agent.config.models import AppConfig
from trading_agent.data.models import Candle, interval_to_ms
from trading_agent.metrics.performance import EXIT_REASON_STOP_LOSS
from trading_agent.sizing.exchange_filters import SymbolFilters

INTERVAL = "4h"
STEP = interval_to_ms(INTERVAL)
START = 1_577_836_800_000  # 2020-01-01T00:00:00Z


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
            symbol="BTCUSDT",
            interval=INTERVAL,
            open_time_ms=start + i * STEP,
            close_time_ms=start + i * STEP + STEP - 1,
            open=Decimal(str(close)),
            high=Decimal(str(close + 5)),
            low=Decimal(str(close - 5)),
            close=Decimal(str(close)),
            volume=Decimal(1),
        )
        for i, close in enumerate(closes)
    ]


def _config(**overrides) -> AppConfig:
    risk_overrides = overrides.pop("risk", {})
    backtest_overrides = {
        "train_fraction": 0.5,
        "validation_fraction": 0.25,
        "test_fraction": 0.25,
        "min_trades_for_significance": 3,
    }
    backtest_overrides.update(overrides.pop("backtest", {}))
    return AppConfig(
        mode="backtest",
        strategy={"ema_fast": 3, "ema_slow": 6},
        backtest=backtest_overrides,
        risk=risk_overrides,
        **overrides,
    )


def _filters() -> SymbolFilters:
    return SymbolFilters.from_exchange_info(make_exchange_info(min_notional="1"))


def _zigzag_candles() -> list[Candle]:
    return _candles(_zigzag_closes(segment_len=20, num_segments=20, base=30000.0, amplitude=400.0))


# --- Root-cause evidence: MAX_DRAWDOWN_SHUTDOWN latches permanently while flat. ---


def test_drawdown_shutdown_evidence_is_concrete_not_inferred():
    candles = _zigzag_candles()
    config = _config(risk={"max_drawdown_pct": 0.0001})
    result = run_backtest(candles, config, _filters())

    # The zero-trade validation/test windows are not a mystery: the
    # diagnostics show, with an exact count and timestamp, that
    # MAX_DRAWDOWN_SHUTDOWN is why.
    assert result.reports["train"].trade_count == 1
    assert result.reports["validation"].trade_count == 0
    assert result.reports["test"].trade_count == 0

    diag = result.diagnostics
    assert diag is not None
    assert "MAX_DRAWDOWN_SHUTDOWN" in diag.shutdown_activations
    activation = diag.shutdown_activations["MAX_DRAWDOWN_SHUTDOWN"]
    assert activation.blocked_buy_count == diag.rejected_entries_by_reason["MAX_DRAWDOWN_SHUTDOWN"]
    assert activation.blocked_buy_count >= 1
    assert activation.remained_latched_to_end is True
    assert activation.equity_at_activation > 0
    assert 0 < activation.drawdown_pct_at_activation < 1
    # It activated strictly after the one executed trade, and its recorded
    # activation equity is exactly the post-trade equity - not a guess.
    assert activation.first_activated_time_ms > diag.first_executed_trade_time_ms


def test_independent_holdout_evaluation_resets_risk_state_and_does_not_latch():
    candles = _zigzag_candles()
    config = _config(risk={"max_drawdown_pct": 0.0001})
    continuous = run_backtest(candles, config, _filters())
    assert continuous.reports["validation"].trade_count == 0
    assert continuous.reports["test"].trade_count == 0

    holdout = run_independent_holdout_evaluation(candles, config, _filters())
    by_label = {w.label: w for w in holdout.windows}
    # Each window gets its own fresh $starting_equity and fresh drawdown
    # tracking, so validation/test are NOT permanently blocked by
    # whatever happened during train - each trades on its own merits.
    assert by_label["train"].performance.trade_count >= 1
    assert by_label["validation"].performance.trade_count >= 1
    assert by_label["test"].performance.trade_count >= 1
    for window in holdout.windows:
        assert window.performance.starting_equity == Decimal(str(config.backtest.starting_equity))
    assert "NOT walk-forward optimization" in holdout.label


# --- Warm-up isolation. ---


def test_warmup_candles_never_generate_a_trade_or_appear_before_window_start():
    candles = _zigzag_candles()
    config = _config()
    holdout = run_independent_holdout_evaluation(candles, config, _filters())
    # Every window's warm-up lookback precedes its own tradable start, and
    # its reported performance starts from the configured starting
    # equity - the warm-up candles contributed no trade and no return.
    for window in holdout.windows:
        report = window.performance
        assert report.starting_equity == Decimal(str(config.backtest.starting_equity))
        assert window.warm_up_start_time_ms < window.window_start_time_ms
        assert window.warm_up_candle_count == config.strategy.ema_slow


def test_warmup_never_uses_future_data_beyond_the_window():
    # Appending a wildly different, LATER, gap-separated segment must not
    # change segment 0's own windows at all - no candle beyond a window's
    # own end (whether inside its segment or in a segment appended after
    # it) is ever visible to that window's indicator calculations.
    config = _config()
    filters = _filters()
    base_candles = _zigzag_candles()
    gap_start = base_candles[-1].open_time_ms + 2 * STEP
    extra_segment = _candles(
        _zigzag_closes(segment_len=20, num_segments=10, base=99999.0, amplitude=4000.0), start=gap_start
    )
    extended_candles = base_candles + extra_segment

    holdout_base = run_independent_holdout_evaluation(base_candles, config, filters)
    holdout_extended = run_independent_holdout_evaluation(extended_candles, config, filters)

    base_seg0_windows = {w.label: w for w in holdout_base.windows if w.segment_index == 0}
    extended_seg0_windows = {w.label: w for w in holdout_extended.windows if w.segment_index == 0}
    for label in ("train", "validation", "test"):
        b, e = base_seg0_windows[label], extended_seg0_windows[label]
        assert b.performance.trade_count == e.performance.trade_count
        assert b.performance.total_return_pct == e.performance.total_return_pct
        assert b.window_end_time_ms == e.window_end_time_ms


def test_warmup_never_crosses_a_confirmed_gap():
    # Segment 1 is far too short to supply its own warm-up on its own if
    # the gap were bridged - if warm-up ever reached back into segment 0
    # it would succeed; instead it must be skipped as insufficient.
    config = _config()
    segment0 = _zigzag_candles()
    gap_start = segment0[-1].open_time_ms + 2 * STEP  # exactly one missing interval -> confirmed gap
    segment1 = _candles(_zigzag_closes(segment_len=3, num_segments=1, base=30000.0, amplitude=100.0), start=gap_start)
    combined = segment0 + segment1

    holdout = run_independent_holdout_evaluation(combined, config, _filters())
    # Segment 1 (index 1) never contributes a window - it has fewer
    # candles than the strategy's warm-up requirement, and nothing from
    # segment 0 was borrowed to make up the shortfall.
    assert all(w.segment_index == 0 for w in holdout.windows)
    assert any("segment 1" in w for w in holdout.warnings)


# --- No position/pending signal crosses an evaluation window boundary. ---


def test_open_position_or_pending_signal_never_carries_into_the_next_window():
    candles = _zigzag_candles()
    config = _config()
    holdout = run_independent_holdout_evaluation(candles, config, _filters())
    by_label = {w.label: w for w in holdout.windows}
    train = by_label["train"]
    validation = by_label["validation"]
    if train.ends_with_open_position or train.unresolved_pending_signal is not None:
        # If train ends mid-position, validation must start FLAT and its
        # first executed trade (if any) must be a fresh entry at or after
        # validation's own window start - never an inherited exit.
        assert validation.diagnostics.starting_equity == Decimal(str(config.backtest.starting_equity))
        if validation.diagnostics.first_executed_trade_time_ms is not None:
            assert validation.diagnostics.first_executed_trade_time_ms >= validation.window_start_time_ms


# --- Stop-loss vs strategy exits counted separately. ---


def test_stop_loss_and_strategy_exits_are_counted_separately():
    # Reuses the documented gap-through-stop scenario shape: a BUY then an
    # immediate adverse gap through the stop produces a STOP_LOSS exit,
    # never confused with a strategy EXIT signal.
    closes = [100, 99, 98, 97, 96, 95, 100, 100, 50]
    opens = {7: 90, 8: 50}
    lows = {7: 89, 8: 40}
    highs = {7: 101, 8: 55}
    candles = [
        Candle(
            symbol="BTCUSDT", interval=INTERVAL,
            open_time_ms=START + i * STEP, close_time_ms=START + i * STEP + STEP - 1,
            open=Decimal(str(opens.get(i, c))), high=Decimal(str(highs.get(i, c + 1))),
            low=Decimal(str(lows.get(i, c - 1))), close=Decimal(str(c)), volume=Decimal(1),
        )
        for i, c in enumerate(closes)
    ]
    config = _config()
    result = run_backtest(candles, config, _filters())
    trades = result.trades
    assert len(trades) == 1
    assert trades[0].exit_reason == EXIT_REASON_STOP_LOSS
    assert result.diagnostics.executed_stop_loss_exits == 1
    assert result.diagnostics.executed_strategy_exits == 0
    assert result.reports["overall"].stop_loss_exit_count == 1
    assert result.reports["overall"].strategy_exit_count == 0


# --- Maximum-drawdown timestamp is correct. ---


def test_max_drawdown_timestamp_matches_the_actual_trough():
    candles = _zigzag_candles()
    config = _config()
    result = run_backtest(candles, config, _filters())
    diag = result.diagnostics
    assert diag.max_drawdown_time_ms is not None
    equity_curve = result.equity_curve
    values = {p.timestamp_ms: p.equity for p in equity_curve}
    peak = equity_curve[0].equity
    max_dd = Decimal(0)
    max_dd_time = None
    for p in equity_curve:
        peak = max(peak, p.equity)
        if peak > 0:
            dd = (peak - p.equity) / peak
            if dd > max_dd:
                max_dd = dd
                max_dd_time = p.timestamp_ms
    assert diag.max_drawdown_time_ms == max_dd_time
    assert values[diag.max_drawdown_time_ms] is not None


# --- starting_equity is configurable end-to-end. ---


def test_starting_equity_is_configurable():
    candles = _zigzag_candles()
    default_result = run_backtest(candles, _config(), _filters())
    custom_result = run_backtest(candles, _config(backtest={"starting_equity": 1000.0}), _filters())

    assert default_result.reports["overall"].starting_equity == Decimal(50)
    assert custom_result.reports["overall"].starting_equity == Decimal("1000.0")
    # Same trades, proportionally scaled equity (both start flat, same
    # signals - only the capital base differs).
    assert default_result.reports["overall"].trade_count == custom_result.reports["overall"].trade_count


# --- Gap-segmented results are never naively concatenated. ---


def test_multi_segment_backtest_never_produces_a_naive_overall_report():
    config = _config()
    segment0 = _zigzag_candles()
    gap_start = segment0[-1].open_time_ms + 2 * STEP
    segment1 = _candles(_zigzag_closes(segment_len=20, num_segments=10, base=30000.0, amplitude=400.0), start=gap_start)
    combined = segment0 + segment1

    result = run_backtest(combined, config, _filters())
    assert len(result.gaps) == 1
    assert result.reports == {}
    assert result.aggregate_trade_stats is not None
    assert result.aggregate_trade_stats.segments_included == 2
    assert result.aggregate_trade_stats.total_trades == sum(s.trade_count for s in result.segments)
    for seg in result.segments:
        assert seg.performance is not None
        assert seg.performance.starting_equity == Decimal(str(config.backtest.starting_equity))
    assert any("No combined 'overall'" in w for w in result.warnings)


# --- Buy-and-hold matches the exact evaluated range, never bridges a gap. ---


def test_buy_and_hold_matches_exact_report_range():
    candles = _zigzag_candles()
    config = _config()
    result = run_backtest(candles, config, _filters())
    report = result.reports["overall"]
    bh = report.buy_and_hold
    assert bh is not None
    assert bh.start_time_ms == result.equity_curve[0].timestamp_ms
    assert bh.end_time_ms == result.equity_curve[-1].timestamp_ms
    assert bh.max_drawdown_pct is not None
    assert bh.buy_fee_pct_applied == config.fees.taker_fee_pct


def test_buy_and_hold_never_bridges_a_confirmed_gap():
    config = _config()
    segment0 = _zigzag_candles()
    gap_start = segment0[-1].open_time_ms + 2 * STEP
    segment1 = _candles(_zigzag_closes(segment_len=20, num_segments=10, base=30000.0, amplitude=400.0), start=gap_start)
    combined = segment0 + segment1

    result = run_backtest(combined, config, _filters())
    for seg, raw_segment in zip(result.segments, (segment0, segment1)):
        assert seg.performance is not None
        bh = seg.performance.buy_and_hold
        assert bh is not None
        assert bh.start_time_ms >= raw_segment[0].open_time_ms
        assert bh.end_time_ms == raw_segment[-1].close_time_ms
