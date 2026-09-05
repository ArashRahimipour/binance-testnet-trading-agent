"""Complete round-3 report for `multitimeframe_breakout_E1_round3` -
evaluated ONLY on pre-cutoff development data, using the SAME duration-
normalized fixed-duration blocks round 2 introduced (`research/
fixed_duration_evaluation.py`, unmodified), and NEVER on the consumed
post-cutoff period (`run_candidate_fixed_duration_evaluation` enforces
`research/cutoff.py::assert_pre_cutoff` regardless of what the caller
already did).

E1 must be evaluated at the 1h interval (`research/candidate_registry_round3.py
::REQUIRED_MARKET_INTERVAL`) - its own weekly/4h context is derived by
aggregating whatever 1h candles it is given (see `research/candidates/
multitimeframe_breakout.py`'s module docstring), never fetched or stored
separately.

Reuses, verbatim, the SAME already-built and already-tested pieces every
other round's report relies on - no new scoring rule, no new statistic
engine:
  - `research/scorecard.py::score_candidate` - the SAME conservative,
    pre-declared thresholds as rounds 1 and 2 (30+ trades, 4+ full
    blocks, positive median/aggregate realized PnL, no block worse than
    -10%, max drawdown <= 15%, >= 60% positive blocks, limited best-trade
    dependence). Nothing here loosens or tightens a single threshold.
  - `research/post_mortem.py::build_candidate_post_mortem` - trade
    counts/win rate, expectancy in quote/%/R, payoff ratio, profit
    factor, exit-reason breakdown, realized R distribution, planned-vs-
    realized R/R, fee/slippage totals, best-trade/best-3/best-5%
    exclusion analysis, per-calendar-year results, chronological
    stability, longest underwater period, and the risk/reward policy's
    own rejection/1%-compliance diagnostics - ALL of the per-trade
    statistics this report is required to disclose beyond E1's own
    multi-timeframe-specific counters, computed identically to every
    other candidate's own post-mortem.

Adds only what round 1/2's post-mortem does not already compute: E1's own
multi-timeframe funnel counters (`weekly_filter_rejections`,
`four_h_setups_detected`, `setups_armed`, `setups_expired`,
`one_h_confirmations`, `strategy_entries`) from `MultiTimeframeBreakoutStrategy`
's own read-only instance counters - see that module's docstring.

Even if E1 becomes a RESEARCH_SURVIVOR under these thresholds,
`E1_NOT_APPROVED_NOTE` is attached to every report - never a claim of
profitability, never approval for live or Testnet trading.
"""

from __future__ import annotations

from dataclasses import dataclass

from trading_agent.config.models import AppConfig
from trading_agent.data.models import Candle
from trading_agent.journal.journal import Journal
from trading_agent.research.candidate_registry_round3 import (
    CUMULATIVE_CANDIDATE_CONFIGURATIONS_EXAMINED,
    ROUND3_CANDIDATE_REGISTRY,
    ROUND3_MULTIPLE_TESTING_WARNING,
    ROUND_NUMBER,
)
from trading_agent.research.candidates.multitimeframe_breakout import (
    MultiTimeframeBreakoutStrategy,
)
from trading_agent.research.fixed_duration_evaluation import (
    InsufficientDurationFragment,
    LeftoverPartialWindow,
    run_candidate_fixed_duration_evaluation,
)
from trading_agent.research.post_mortem import CandidatePostMortem, build_candidate_post_mortem
from trading_agent.research.scorecard import ScorecardEntry, score_candidate
from trading_agent.sizing.exchange_filters import SymbolFilters

E1_NOT_APPROVED_NOTE = (
    "Even if multitimeframe_breakout_E1_round3 becomes a RESEARCH_SURVIVOR under these thresholds, this is "
    "NOT a claim of profitability and NOT approval for live or Testnet trading (see research/scorecard.py::"
    "RESEARCH_SURVIVOR_CAVEAT). It must also be read together with ROUND3_MULTIPLE_TESTING_WARNING: it is a "
    "round-3 hypothesis examined after round 1's nine candidates and round 2's D1 (OFFICIAL REJECTED, "
    "preserved unchanged) were already observed - never an untouched pre-registered test."
)


@dataclass(frozen=True, slots=True)
class Round3CandidateReport:
    candidate_id: str
    family: str
    params: dict[str, float | int]
    round_number: int
    cumulative_candidate_configurations_examined: int
    multiple_testing_warning: str
    scorecard: ScorecardEntry
    post_mortem: CandidatePostMortem
    #: E1's own multi-timeframe funnel diagnostics - see module docstring.
    weekly_filter_rejections: int
    four_h_setups_detected: int
    setups_armed: int
    setups_expired: int
    one_h_confirmations: int
    strategy_entries: int
    fragments: list[InsufficientDurationFragment]
    leftovers: list[LeftoverPartialWindow]
    not_approved_note: str = E1_NOT_APPROVED_NOTE


def build_round3_report(
    pre_cutoff_candles_1h: list[Candle],
    config: AppConfig,
    filters: SymbolFilters,
    journal: Journal | None = None,
) -> Round3CandidateReport:
    e1_spec = ROUND3_CANDIDATE_REGISTRY[0]
    e1_strategy = e1_spec.build()
    assert isinstance(e1_strategy, MultiTimeframeBreakoutStrategy)

    e1_result = run_candidate_fixed_duration_evaluation(
        e1_spec, pre_cutoff_candles_1h, config, filters, journal, strategy=e1_strategy
    )
    blocked_result = e1_result.as_blocked_chronological_result()
    scorecard = score_candidate(blocked_result)
    post_mortem = build_candidate_post_mortem(blocked_result)

    return Round3CandidateReport(
        candidate_id=e1_spec.candidate_id,
        family=e1_spec.family,
        params=e1_spec.params,
        round_number=ROUND_NUMBER,
        cumulative_candidate_configurations_examined=CUMULATIVE_CANDIDATE_CONFIGURATIONS_EXAMINED,
        multiple_testing_warning=ROUND3_MULTIPLE_TESTING_WARNING,
        scorecard=scorecard,
        post_mortem=post_mortem,
        weekly_filter_rejections=e1_strategy.weekly_filter_rejections,
        four_h_setups_detected=e1_strategy.four_h_setups_detected,
        setups_armed=e1_strategy.setups_armed,
        setups_expired=e1_strategy.setups_expired,
        one_h_confirmations=e1_strategy.one_h_confirmations,
        strategy_entries=e1_strategy.entries,
        fragments=e1_result.fragments,
        leftovers=e1_result.leftovers,
    )
