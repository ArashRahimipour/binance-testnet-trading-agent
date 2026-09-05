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

`b1_on_identical_dates` re-runs the ORIGINAL, UNMODIFIED `breakout_B1`
candidate through the SAME fixed-duration block construction D1 itself
uses, so both share IDENTICAL block date ranges - a fair, apples-to-apples
comparison. This does NOT alter, recompute, or supersede breakout_B1's own
`round_1_original_evaluation` status in any way (see `research/
sensitivity_comparison.py` for that, separate, comparison).

Even if D1 becomes a RESEARCH_SURVIVOR under these thresholds,
`D1_NOT_APPROVED_NOTE` is attached to every report - never a claim of
profitability, never approval for live or Testnet trading.
"""

from __future__ import annotations

from dataclasses import dataclass

from trading_agent.config.models import AppConfig
from trading_agent.data.models import Candle
from trading_agent.journal.journal import Journal
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
    InsufficientDurationFragment,
    LeftoverPartialWindow,
    run_candidate_fixed_duration_evaluation,
)
from trading_agent.research.post_mortem import CandidatePostMortem, build_candidate_post_mortem
from trading_agent.research.scorecard import ScorecardEntry, score_candidate
from trading_agent.sizing.exchange_filters import SymbolFilters

D1_NOT_APPROVED_NOTE = (
    "Even if breakout_regime_D1_round2 becomes a RESEARCH_SURVIVOR under these thresholds, this is "
    "NOT a claim of profitability and NOT approval for live or Testnet trading (see research/"
    "scorecard.py::RESEARCH_SURVIVOR_CAVEAT). It must also be read together with "
    "ROUND2_MULTIPLE_TESTING_WARNING: it is a result-informed, round-2 hypothesis, never an untouched "
    "pre-registered test."
)

B1_COMPARISON_NOTE = (
    "breakout_B1 below was re-run through the SAME duration-normalized block construction D1 itself "
    "uses (research/fixed_duration_evaluation.py), so both share IDENTICAL block date ranges - a fair, "
    "apples-to-apples comparison on the same stretches of history. This does NOT alter, recompute, or "
    "supersede breakout_B1's own round_1_original_evaluation status (research/blocked_chronological_"
    "evaluation.py, unmodified) - see research/sensitivity_comparison.py for that separate comparison."
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


def build_round2_report(
    pre_cutoff_candles: list[Candle],
    config: AppConfig,
    filters: SymbolFilters,
    journal: Journal | None = None,
) -> Round2Comparison:
    d1_spec = ROUND2_CANDIDATE_REGISTRY[0]
    d1_strategy = d1_spec.build()
    assert isinstance(d1_strategy, BreakoutWithBullishRegimeGateStrategy)
    d1_result = run_candidate_fixed_duration_evaluation(
        d1_spec, pre_cutoff_candles, config, filters, journal, strategy=d1_strategy
    )
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

    b1_spec = next(s for s in CANDIDATE_REGISTRY if s.candidate_id == "breakout_B1")
    b1_result = run_candidate_fixed_duration_evaluation(b1_spec, pre_cutoff_candles, config, filters, journal)
    b1_blocked_result = b1_result.as_blocked_chronological_result()

    return Round2Comparison(
        d1=d1_report,
        b1_scorecard_on_identical_dates=score_candidate(b1_blocked_result),
        b1_post_mortem_on_identical_dates=build_candidate_post_mortem(b1_blocked_result),
    )
