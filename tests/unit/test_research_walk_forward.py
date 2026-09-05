"""Proofs for research/walk_forward.py: fold isolation, gap boundaries,
rejected/skipped folds always reported, exchange-filter rejections
surfaced, cutoff enforcement, and deterministic reports.
"""

from decimal import Decimal
from itertools import pairwise

import pytest

from tests.fixtures.exchange_info import make_exchange_info
from trading_agent.config.models import AppConfig
from trading_agent.data.models import Candle, interval_to_ms
from trading_agent.research.candidate_registry import CANDIDATE_REGISTRY
from trading_agent.research.cutoff import RESEARCH_CUTOFF_MS, ResearchCutoffViolation
from trading_agent.research.walk_forward import run_candidate_walk_forward
from trading_agent.sizing.exchange_filters import SymbolFilters

INTERVAL = "4h"
STEP = interval_to_ms(INTERVAL)
START = RESEARCH_CUTOFF_MS - 2000 * STEP  # comfortably pre-cutoff


def _candle(i: int, close: float, start: int = START, high: float | None = None, low: float | None = None) -> Candle:
    high = close if high is None else high
    low = close if low is None else low
    return Candle(
        symbol="BTCUSDT", interval=INTERVAL, open_time_ms=start + i * STEP, close_time_ms=start + i * STEP + STEP - 1,
        open=Decimal(str(close)), high=Decimal(str(high)), low=Decimal(str(low)), close=Decimal(str(close)),
        volume=Decimal(1),
    )


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


def _zigzag_candles(start: int = START) -> list[Candle]:
    return [_candle(i, c, start=start) for i, c in enumerate(_zigzag_closes(20, 20, 30000.0, 400.0))]


def _config(**overrides) -> AppConfig:
    risk_overrides = overrides.pop("risk", {})
    backtest_overrides = {"train_fraction": 0.6, "validation_fraction": 0.2, "test_fraction": 0.2, "min_trades_for_significance": 3}
    backtest_overrides.update(overrides.pop("backtest", {}))
    return AppConfig(mode="backtest", backtest=backtest_overrides, risk=risk_overrides, **overrides)


def _filters(**overrides) -> SymbolFilters:
    return SymbolFilters.from_exchange_info(make_exchange_info(**overrides))


_TREND_A1 = next(spec for spec in CANDIDATE_REGISTRY if spec.candidate_id == "trend_regime_A1")
_BREAKOUT_B1 = next(spec for spec in CANDIDATE_REGISTRY if spec.candidate_id == "breakout_B1")


# --- Cutoff enforcement. ---


def test_walk_forward_raises_on_post_cutoff_data():
    candles = _zigzag_candles()
    post_cutoff = _candle(len(candles), 30000.0, start=RESEARCH_CUTOFF_MS)
    with pytest.raises(ResearchCutoffViolation):
        run_candidate_walk_forward(_TREND_A1, candles + [post_cutoff], _config(), _filters())


def test_walk_forward_accepts_strictly_pre_cutoff_data():
    candles = _zigzag_candles()
    result = run_candidate_walk_forward(_TREND_A1, candles, _config(), _filters())
    assert result.candidate_id == "trend_regime_A1"


# --- Every fold reported, including zero-trade and skipped folds. ---


def test_walk_forward_reports_every_fold_even_with_zero_trades():
    flat_candles = [_candle(i, 100.0) for i in range(300)]
    result = run_candidate_walk_forward(_TREND_A1, flat_candles, _config(), _filters(), fold_count=5)
    evaluated = [f for f in result.folds if f.fold.skipped_reason is None]
    assert len(evaluated) == 5
    for fold in evaluated:
        assert fold.performance is not None
        assert fold.performance.trade_count == 0


def test_walk_forward_reports_a_skipped_fold_when_segment_too_small():
    strat = _TREND_A1.build()
    too_few = [_candle(i, 100.0) for i in range(strat.min_required_candles - 1)]
    result = run_candidate_walk_forward(_TREND_A1, too_few, _config(), _filters())
    assert len(result.folds) == 1
    assert result.folds[0].fold.skipped_reason is not None
    assert result.folds[0].performance is None
    assert any("skipped" in w for w in result.warnings)


def test_walk_forward_never_reports_only_the_best_fold():
    # With a strict per-fold drawdown shutdown, at least one fold trades
    # and at least one is starved by the latch - BOTH must appear, not
    # just whichever looks better.
    candles = _zigzag_candles()
    result = run_candidate_walk_forward(
        _BREAKOUT_B1, candles, _config(risk={"max_drawdown_pct": 0.0001}), _filters(), fold_count=5
    )
    evaluated = [f for f in result.folds if f.fold.skipped_reason is None]
    assert len(evaluated) == 5  # all five folds present, not trimmed to a subset


# --- Fold isolation: no risk/position state crosses a fold boundary. ---


def test_walk_forward_fold_isolation_resets_risk_state_between_folds():
    candles = _zigzag_candles()
    strict = run_candidate_walk_forward(
        _BREAKOUT_B1, candles, _config(risk={"max_drawdown_pct": 0.0001}), _filters(), fold_count=5
    )
    evaluated = [f for f in strict.folds if f.fold.skipped_reason is None]
    # Every fold starts fresh from the configured starting_equity - a
    # shutdown latched in an earlier fold cannot suppress a later fold's
    # own ability to trade on its own terms.
    for fold in evaluated:
        assert fold.diagnostics is not None
        assert fold.diagnostics.starting_equity == Decimal(50)
    # At least one fold actually executes something (proving the strict
    # threshold doesn't just block everything everywhere structurally).
    assert any(f.performance is not None and f.performance.trade_count > 0 for f in evaluated)


def test_walk_forward_fold_boundaries_are_chronological_and_non_overlapping():
    candles = _zigzag_candles()
    result = run_candidate_walk_forward(_TREND_A1, candles, _config(), _filters(), fold_count=4)
    evaluated = [f for f in result.folds if f.fold.skipped_reason is None]
    for earlier, later in pairwise(evaluated):
        assert earlier.fold.window_end_time_ms is not None
        assert later.fold.window_start_time_ms is not None
        assert earlier.fold.window_end_time_ms < later.fold.window_start_time_ms


# --- Gap boundaries: folds never cross a confirmed gap. ---


def test_walk_forward_never_builds_a_fold_across_a_confirmed_gap():
    segment0 = _zigzag_candles()
    gap_start = segment0[-1].open_time_ms + 2 * STEP  # one missing interval -> confirmed gap
    segment1 = [_candle(i, c, start=gap_start) for i, c in enumerate(_zigzag_closes(20, 20, 30000.0, 400.0))]
    combined = segment0 + segment1

    result = run_candidate_walk_forward(_TREND_A1, combined, _config(), _filters(), fold_count=3)
    evaluated = [f for f in result.folds if f.fold.skipped_reason is None]

    segment_indices = {f.fold.segment_index for f in evaluated}
    assert segment_indices == {0, 1}
    for fold in evaluated:
        assert fold.fold.window_start_time_ms is not None and fold.fold.window_end_time_ms is not None
        if fold.fold.segment_index == 0:
            assert fold.fold.window_end_time_ms <= segment0[-1].close_time_ms
        else:
            assert fold.fold.window_start_time_ms >= segment1[0].open_time_ms


# --- Exchange-filter rejections surfaced. ---


def test_walk_forward_surfaces_exchange_filter_rejections():
    strat = _BREAKOUT_B1.build()
    plateau_len = strat.min_required_candles
    candles = (
        [_candle(i, 100.0, high=101.0, low=99.0) for i in range(plateau_len)]
        + [_candle(plateau_len, 110.0, high=111.0, low=109.0)]  # confirmed breakout candle
        + [_candle(plateau_len + 1, 110.0, high=111.0, low=109.0)]  # a following candle for the BUY to resolve against
    )
    # min_notional far beyond what $50 starting equity risk-sized at 2% could ever satisfy.
    impossible_filters = _filters(min_notional="100000")
    result = run_candidate_walk_forward(_BREAKOUT_B1, candles, _config(), impossible_filters, fold_count=1)
    evaluated = [f for f in result.folds if f.fold.skipped_reason is None]
    all_reasons: dict[str, int] = {}
    for fold in evaluated:
        assert fold.diagnostics is not None
        all_reasons.update(fold.diagnostics.rejected_entries_by_reason)
    assert "BELOW_MIN_NOTIONAL" in all_reasons


# --- Deterministic reports. ---


def test_walk_forward_is_deterministic():
    candles = _zigzag_candles()
    result1 = run_candidate_walk_forward(_TREND_A1, candles, _config(), _filters())
    result2 = run_candidate_walk_forward(_TREND_A1, candles, _config(), _filters())
    assert result1 == result2
