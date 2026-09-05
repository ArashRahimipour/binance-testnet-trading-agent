"""Proofs for research/blocked_chronological_evaluation.py: block isolation,
gap boundaries, rejected/skipped blocks always reported, exchange-filter
rejections surfaced, cutoff enforcement, and deterministic reports.

Renamed from "walk-forward" (pre-real-evaluation code review correction -
see CHANGELOG.md): nothing in this module fits, trains, or selects
anything per block - every candidate's parameters are fixed in
`candidate_registry.py` before this module ever runs. These tests
deliberately use the term "block", never "fold" or "walk-forward window",
to keep that distinction visible.
"""

import importlib
from dataclasses import fields
from decimal import Decimal
from itertools import pairwise

import pytest

from tests.fixtures.exchange_info import make_exchange_info
from trading_agent.config.models import AppConfig
from trading_agent.data.models import Candle, interval_to_ms
from trading_agent.research.blocked_chronological_evaluation import (
    run_candidate_blocked_chronological_evaluation,
)
from trading_agent.research.candidate_registry import CANDIDATE_REGISTRY
from trading_agent.research.cutoff import RESEARCH_CUTOFF_MS, ResearchCutoffViolation
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


def test_blocked_chronological_evaluation_raises_on_post_cutoff_data():
    candles = _zigzag_candles()
    post_cutoff = _candle(len(candles), 30000.0, start=RESEARCH_CUTOFF_MS)
    with pytest.raises(ResearchCutoffViolation):
        run_candidate_blocked_chronological_evaluation(_TREND_A1, candles + [post_cutoff], _config(), _filters())


def test_blocked_chronological_evaluation_accepts_strictly_pre_cutoff_data():
    candles = _zigzag_candles()
    result = run_candidate_blocked_chronological_evaluation(_TREND_A1, candles, _config(), _filters())
    assert result.candidate_id == "trend_regime_A1"


# --- Every block reported, including zero-trade and skipped blocks. ---


def test_blocked_chronological_evaluation_reports_every_block_even_with_zero_trades():
    flat_candles = [_candle(i, 100.0) for i in range(300)]
    result = run_candidate_blocked_chronological_evaluation(_TREND_A1, flat_candles, _config(), _filters(), block_count=5)
    evaluated = [b for b in result.blocks if b.block.skipped_reason is None]
    assert len(evaluated) == 5
    for block in evaluated:
        assert block.performance is not None
        assert block.performance.trade_count == 0


def test_blocked_chronological_evaluation_reports_a_skipped_block_when_segment_too_small():
    strat = _TREND_A1.build()
    too_few = [_candle(i, 100.0) for i in range(strat.min_required_candles - 1)]
    result = run_candidate_blocked_chronological_evaluation(_TREND_A1, too_few, _config(), _filters())
    assert len(result.blocks) == 1
    assert result.blocks[0].block.skipped_reason is not None
    assert result.blocks[0].performance is None
    assert any("skipped" in w for w in result.warnings)


def test_blocked_chronological_evaluation_reports_each_blocks_own_closed_trades():
    # Read-only instrumentation added for research/post_mortem.py:
    # BlockResult.trades must carry the SAME closed-trade list
    # `performance`/`extended` were already computed from - not a
    # recomputation, just retaining what run_segment already produced.
    candles = _zigzag_candles()
    result = run_candidate_blocked_chronological_evaluation(_BREAKOUT_B1, candles, _config(), _filters(), block_count=5)
    evaluated = [b for b in result.blocks if b.block.skipped_reason is None]
    assert any(len(b.trades) > 0 for b in evaluated)
    for block in evaluated:
        assert block.performance is not None
        assert len(block.trades) == block.performance.trade_count


def test_blocked_chronological_evaluation_never_reports_only_the_best_block():
    # With a strict per-block drawdown shutdown, at least one block trades
    # and at least one is starved by the latch - BOTH must appear, not
    # just whichever looks better.
    candles = _zigzag_candles()
    result = run_candidate_blocked_chronological_evaluation(
        _BREAKOUT_B1, candles, _config(risk={"max_drawdown_pct": 0.0001}), _filters(), block_count=5
    )
    evaluated = [b for b in result.blocks if b.block.skipped_reason is None]
    assert len(evaluated) == 5  # all five blocks present, not trimmed to a subset


# --- Block isolation: no risk/position state crosses a block boundary. ---


def test_blocked_chronological_evaluation_block_isolation_resets_risk_state_between_blocks():
    candles = _zigzag_candles()
    # Zero fees/slippage so the fixed 1:2 risk/reward gate's NET ratio
    # equals its GROSS ratio (exactly 2.0, always approved) - this test is
    # about block isolation, not the R/R gate's own cost-erosion mechanics
    # (see test_backtest_risk_reward.py for those).
    strict = run_candidate_blocked_chronological_evaluation(
        _BREAKOUT_B1, candles,
        _config(risk={"max_drawdown_pct": 0.0001}, fees={"taker_fee_pct": 0.0, "slippage_pct": 0.0}),
        _filters(), block_count=5,
    )
    evaluated = [b for b in strict.blocks if b.block.skipped_reason is None]
    # Every block starts fresh from the configured starting_equity - a
    # shutdown latched in an earlier block cannot suppress a later block's
    # own ability to trade on its own terms.
    for block in evaluated:
        assert block.diagnostics is not None
        assert block.diagnostics.starting_equity == Decimal(50)
    # At least one block actually executes something (proving the strict
    # threshold doesn't just block everything everywhere structurally).
    assert any(b.performance is not None and b.performance.trade_count > 0 for b in evaluated)


def test_blocked_chronological_evaluation_block_boundaries_are_chronological_and_non_overlapping():
    candles = _zigzag_candles()
    result = run_candidate_blocked_chronological_evaluation(_TREND_A1, candles, _config(), _filters(), block_count=4)
    evaluated = [b for b in result.blocks if b.block.skipped_reason is None]
    for earlier, later in pairwise(evaluated):
        assert earlier.block.window_end_time_ms is not None
        assert later.block.window_start_time_ms is not None
        assert earlier.block.window_end_time_ms < later.block.window_start_time_ms


# --- Gap boundaries: blocks never cross a confirmed gap. ---


def test_blocked_chronological_evaluation_never_builds_a_block_across_a_confirmed_gap():
    segment0 = _zigzag_candles()
    gap_start = segment0[-1].open_time_ms + 2 * STEP  # one missing interval -> confirmed gap
    segment1 = [_candle(i, c, start=gap_start) for i, c in enumerate(_zigzag_closes(20, 20, 30000.0, 400.0))]
    combined = segment0 + segment1

    result = run_candidate_blocked_chronological_evaluation(_TREND_A1, combined, _config(), _filters(), block_count=3)
    evaluated = [b for b in result.blocks if b.block.skipped_reason is None]

    segment_indices = {b.block.segment_index for b in evaluated}
    assert segment_indices == {0, 1}
    for block in evaluated:
        assert block.block.window_start_time_ms is not None and block.block.window_end_time_ms is not None
        if block.block.segment_index == 0:
            assert block.block.window_end_time_ms <= segment0[-1].close_time_ms
        else:
            assert block.block.window_start_time_ms >= segment1[0].open_time_ms


# --- Exchange-filter rejections surfaced. ---


def test_blocked_chronological_evaluation_surfaces_exchange_filter_rejections():
    strat = _BREAKOUT_B1.build()
    plateau_len = strat.min_required_candles
    candles = (
        [_candle(i, 100.0, high=101.0, low=99.0) for i in range(plateau_len)]
        + [_candle(plateau_len, 110.0, high=111.0, low=109.0)]  # confirmed breakout candle
        + [_candle(plateau_len + 1, 110.0, high=111.0, low=109.0)]  # a following candle for the BUY to resolve against
    )
    # min_notional far beyond what $50 starting equity risk-sized at 1% (the fixed
    # risk/reward policy's budget - see backtest/risk_reward.py) could ever satisfy.
    impossible_filters = _filters(min_notional="100000")
    result = run_candidate_blocked_chronological_evaluation(_BREAKOUT_B1, candles, _config(), impossible_filters, block_count=1)
    evaluated = [b for b in result.blocks if b.block.skipped_reason is None]
    all_reasons: dict[str, int] = {}
    for block in evaluated:
        assert block.diagnostics is not None
        all_reasons.update(block.diagnostics.rejected_entries_by_reason)
    assert "RR_REJECTED_BELOW_MIN_NOTIONAL" in all_reasons


# --- Deterministic reports. ---


def test_blocked_chronological_evaluation_is_deterministic():
    candles = _zigzag_candles()
    result1 = run_candidate_blocked_chronological_evaluation(_TREND_A1, candles, _config(), _filters())
    result2 = run_candidate_blocked_chronological_evaluation(_TREND_A1, candles, _config(), _filters())
    assert result1 == result2


# --- Terminology accuracy (pre-real-evaluation code review correction). ---
# The old "walk-forward" module/names must be gone, not just aliased, and
# this module must explicitly disclaim being walk-forward optimization
# rather than merely omitting the word.


def test_old_walk_forward_module_no_longer_exists():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("trading_agent.research.walk_forward")


def test_module_explicitly_disclaims_walk_forward_optimization():
    import trading_agent.research.blocked_chronological_evaluation as module

    assert module.__doc__ is not None
    assert "NOT" in module.__doc__ and "walk-forward optimization" in module.__doc__
    assert "fits, trains, or selects" in module.__doc__


def test_block_terminology_never_uses_fold_naming():
    from trading_agent.research.blocked_chronological_evaluation import BlockResult, BlockSpec

    block_spec_fields = {f.name for f in fields(BlockSpec)}
    block_result_fields = {f.name for f in fields(BlockResult)}
    assert not any("fold" in name for name in block_spec_fields)
    assert not any("fold" in name for name in block_result_fields)
