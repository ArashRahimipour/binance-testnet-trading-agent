"""Complete round-2 report for `breakout_regime_D1_round2` - evaluated
ONLY on pre-cutoff development data, using the duration-normalized
sensitivity blocks (`research/fixed_duration_evaluation.py`), and NEVER on
the consumed post-cutoff period (both this module and the fixed-duration
evaluator it calls enforce `research/cutoff.py::assert_pre_cutoff`).

Reuses, verbatim, the SAME already-built and already-tested pieces the
rest of this project's research phase relies on - no new scoring rule, no
new statistic engine:
  - `research/scorecard.py::score_candidate` - the SAME conservative,
    pre-declared thresholds as round 1 (30+ trades, 4+ full blocks,
    positive median/aggregate realized PnL, no block worse than -10%,
    max drawdown <= 15%, >= 60% positive blocks, limited best-trade
    dependence). Nothing here loosens or tightens a single threshold.
  - `research/post_mortem.py::build_candidate_post_mortem` - trade counts/
    win rate, expectancy in quote/%/R, payoff ratio, profit factor,
    exit-reason breakdown, realized R distribution, best-trade/best-3/
    best-5% exclusion analysis, per-calendar-year results, longest
    underwater period, and cost totals - ALL of the per-trade statistics
    the round-2 report is required to disclose, computed identically to
    every round-1 candidate's own post-mortem.

Adds only what round 1's post-mortem does not already compute: the worst
per-block max drawdown, and the percentage of underlying breakout signals
the EMA200 regime gate blocked (from `BreakoutWithBullishRegimeGateStrategy`
's own read-only instance counters - see that module's docstring).

IDENTICAL-DATES CORRECTION (post-commit-50a5a5b code review finding): D1
requires ~220 warm-up candles (its own breakout requirement plus the
EMA200+slope lookback); `breakout_B1` requires only ~21. Calling `research/
fixed_duration_evaluation.py::run_candidate_fixed_duration_evaluation`
independently for each (its default, per-candidate-anchored mode) would
therefore anchor each one's block 0 at a DIFFERENT calendar date, despite
both nominally covering "5 duration-normalized blocks" - not the identical
dates this comparison requires. `b1_on_identical_dates` is instead built
by constructing ONE shared `FixedDurationBlockSchedule`
(`build_fixed_duration_schedule`), ANCHORED at `max(d1_min_required,
b1_min_required)` (i.e. D1's own, larger requirement), and passing that
SAME schedule (via `schedule=`) to both candidates' evaluations - each
still gets its own warm-up length preceding a window, but the window
itself is identical for both. `_assert_identical_trading_windows` then
RE-VERIFIES, at runtime, that every block D1 and B1 actually traded shares
the exact same `(segment_index, window_start_time_ms, window_end_time_ms)`
- this is not merely trusted, it is checked on every call. This does NOT
alter, recompute, or supersede breakout_B1's own `round_1_original_evaluation`
status in any way (see `research/sensitivity_comparison.py` for that,
separate, comparison, which legitimately still uses independent
per-candidate anchoring across nine DIFFERENT candidates it does not need
identical dates for).

Even if D1 becomes a RESEARCH_SURVIVOR under these thresholds,
`D1_NOT_APPROVED_NOTE` is attached to every report - never a claim of
profitability, never approval for live or Testnet trading.
"""

from __future__ import annotations

from dataclasses import dataclass

from trading_agent.config.models import AppConfig
from trading_agent.data.models import Candle
from trading_agent.journal.journal import Journal
from trading_agent.research.blocked_chronological_evaluation import BlockResult
from trading_agent.research.candidate_registry import CANDIDATE_REGISTRY
from trading_agent.research.candidate_registry_round2 import (
    CUMULATIVE_CANDIDATE_CONFIGURATIONS_EXAMINED,
    ROUND2_CANDIDATE_REGISTRY,
    ROUND2_MULTIPLE_TESTING_WARNING,
    ROUND_NUMBER,
)
from trading_agent.research.candidates.breakout_regime_gate import (
    BreakoutWithBullishRegimeGateStrategy,
)
from trading_agent.research.fixed_duration_evaluation import (
    FixedDurationScheduleError,
    InsufficientDurationFragment,
    LeftoverPartialWindow,
    build_fixed_duration_schedule,
    run_candidate_fixed_duration_evaluation,
)
from trading_agent.research.post_mortem import CandidatePostMortem, build_candidate_post_mortem
from trading_agent.research.scorecard import ScorecardEntry, score_candidate
from trading_agent.sizing.exchange_filters import SymbolFilters


class IdenticalTradingWindowAssertionError(FixedDurationScheduleError):
    """D1 and B1 were built from the SAME `FixedDurationBlockSchedule` but
    ended up trading different window timestamps - this must never happen
    and is always a bug, never a data condition to tolerate."""

D1_NOT_APPROVED_NOTE = (
    "Even if breakout_regime_D1_round2 becomes a RESEARCH_SURVIVOR under these thresholds, this is "
    "NOT a claim of profitability and NOT approval for live or Testnet trading (see research/"
    "scorecard.py::RESEARCH_SURVIVOR_CAVEAT). It must also be read together with "
    "ROUND2_MULTIPLE_TESTING_WARNING: it is a result-informed, round-2 hypothesis, never an untouched "
    "pre-registered test."
)

B1_COMPARISON_NOTE = (
    "breakout_B1 below was re-run through ONE SHARED FixedDurationBlockSchedule, anchored using D1's own "
    "(larger) warm-up requirement, so both candidates trade IDENTICAL block window timestamps - never "
    "independently derived per candidate (that would misalign dates, since D1's ~220-candle warm-up and "
    "B1's ~21-candle warm-up would otherwise anchor block 0 on different calendar dates). This is verified "
    "at runtime, not merely trusted - see _assert_identical_trading_windows. This does NOT alter, "
    "recompute, or supersede breakout_B1's own round_1_original_evaluation status (research/"
    "blocked_chronological_evaluation.py, unmodified) - see research/sensitivity_comparison.py for that "
    "separate comparison."
)


@dataclass(frozen=True, slots=True)
class Round2CandidateReport:
    candidate_id: str
    family: str
    params: dict[str, float | int]
    round_number: int
    cumulative_candidate_configurations_examined: int
    multiple_testing_warning: str
    scorecard: ScorecardEntry
    post_mortem: CandidatePostMortem
    max_drawdown_pct: float | None
    regime_gate_signals_evaluated: int
    regime_gate_signals_blocked: int
    regime_gate_blocked_pct: float | None
    fragments: list[InsufficientDurationFragment]
    leftovers: list[LeftoverPartialWindow]
    not_approved_note: str = D1_NOT_APPROVED_NOTE


@dataclass(frozen=True, slots=True)
class Round2Comparison:
    d1: Round2CandidateReport
    b1_scorecard_on_identical_dates: ScorecardEntry
    b1_post_mortem_on_identical_dates: CandidatePostMortem
    comparison_note: str = B1_COMPARISON_NOTE


def _assert_identical_trading_windows(d1_result_blocks: list[BlockResult], b1_result_blocks: list[BlockResult]) -> None:
    """Requirement: D1 and B1 must trade the IDENTICAL set of block window
    timestamps - re-verified at runtime on every call, never merely
    trusted because both were built from the same schedule object."""
    d1_keys = [
        (b.block.segment_index, b.block.window_start_time_ms, b.block.window_end_time_ms)
        for b in d1_result_blocks
        if b.block.skipped_reason is None
    ]
    b1_keys = [
        (b.block.segment_index, b.block.window_start_time_ms, b.block.window_end_time_ms)
        for b in b1_result_blocks
        if b.block.skipped_reason is None
    ]
    if d1_keys != b1_keys:
        raise IdenticalTradingWindowAssertionError(
            f"D1 and B1 were evaluated from one shared schedule but traded DIFFERENT window timestamps - "
            f"this must never happen: d1_windows={d1_keys} b1_windows={b1_keys}"
        )


def build_round2_report(
    pre_cutoff_candles: list[Candle],
    config: AppConfig,
    filters: SymbolFilters,
    journal: Journal | None = None,
) -> Round2Comparison:
    d1_spec = ROUND2_CANDIDATE_REGISTRY[0]
    d1_strategy = d1_spec.build()
    assert isinstance(d1_strategy, BreakoutWithBullishRegimeGateStrategy)
    b1_spec = next(s for s in CANDIDATE_REGISTRY if s.candidate_id == "breakout_B1")
    b1_strategy = b1_spec.build()

    # ONE shared, candidate-agnostic schedule, anchored using the LARGER
    # of the two warm-up requirements (D1's own ~220, vs B1's ~21) - see
    # the module docstring's "IDENTICAL-DATES CORRECTION" section. Fails
    # closed (via build_fixed_duration_schedule's own validation) on any
    # duration/overlap/cutoff inconsistency before either candidate is
    # ever evaluated against it.
    anchor_warm_up = max(d1_strategy.min_required_candles, b1_strategy.min_required_candles)
    schedule = build_fixed_duration_schedule(pre_cutoff_candles, config, anchor_warm_up_candles_required=anchor_warm_up)

    d1_result = run_candidate_fixed_duration_evaluation(
        d1_spec, pre_cutoff_candles, config, filters, journal, strategy=d1_strategy, schedule=schedule
    )
    b1_result = run_candidate_fixed_duration_evaluation(
        b1_spec, pre_cutoff_candles, config, filters, journal, strategy=b1_strategy, schedule=schedule
    )
    _assert_identical_trading_windows(d1_result.blocks, b1_result.blocks)

    d1_blocked_result = d1_result.as_blocked_chronological_result()
    d1_scorecard = score_candidate(d1_blocked_result)
    d1_post_mortem = build_candidate_post_mortem(d1_blocked_result)

    evaluated_drawdowns = [
        b.performance.max_drawdown_pct
        for b in d1_result.blocks
        if b.block.skipped_reason is None and b.performance is not None
    ]
    max_drawdown_pct = max(evaluated_drawdowns, default=None)

    gate_evaluated = d1_strategy.breakout_signals_evaluated
    gate_blocked = d1_strategy.breakout_signals_blocked_by_regime_gate
    gate_blocked_pct = 100.0 * gate_blocked / gate_evaluated if gate_evaluated > 0 else None

    d1_report = Round2CandidateReport(
        candidate_id=d1_spec.candidate_id,
        family=d1_spec.family,
        params=d1_spec.params,
        round_number=ROUND_NUMBER,
        cumulative_candidate_configurations_examined=CUMULATIVE_CANDIDATE_CONFIGURATIONS_EXAMINED,
        multiple_testing_warning=ROUND2_MULTIPLE_TESTING_WARNING,
        scorecard=d1_scorecard,
        post_mortem=d1_post_mortem,
        max_drawdown_pct=max_drawdown_pct,
        regime_gate_signals_evaluated=gate_evaluated,
        regime_gate_signals_blocked=gate_blocked,
        regime_gate_blocked_pct=gate_blocked_pct,
        fragments=d1_result.fragments,
        leftovers=d1_result.leftovers,
    )

    b1_blocked_result = b1_result.as_blocked_chronological_result()

    return Round2Comparison(
        d1=d1_report,
        b1_scorecard_on_identical_dates=score_candidate(b1_blocked_result),
        b1_post_mortem_on_identical_dates=build_candidate_post_mortem(b1_blocked_result),
    )
