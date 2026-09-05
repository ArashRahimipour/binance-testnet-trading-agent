"""ROUND-3 candidate registry: exactly ONE new candidate, examined only
AFTER round 1's nine candidates AND round 2's `breakout_regime_D1_round2`
had already been evaluated and their results already observed.

Round 1: nine pre-declared candidates (`research/candidate_registry.py`),
all REJECTED or INSUFFICIENT_EVIDENCE. Round 2: one result-informed
candidate, `breakout_regime_D1_round2` (`research/candidate_registry_round2.py`),
whose OFFICIAL verdict is REJECTED - already observed, and PRESERVED
UNCHANGED here: nothing in this module, `research/candidates/
multitimeframe_breakout.py`, or `research/round3_report.py` ever imports
mutably from, recomputes, or in any way alters `research/
candidate_registry_round2.py`, `research/candidates/breakout_regime_gate.py`,
or `research/round2_report.py`.

`multitimeframe_breakout_E1_round3` (`research/candidates/
multitimeframe_breakout.py`) is this round's one hypothesis: a weekly
regime filter above D1/B1's own 4h breakout+regime setup, with a 1h
confirmation/fill layer. `CUMULATIVE_CANDIDATE_CONFIGURATIONS_EXAMINED`
(11 = 9 round-1 + 1 round-2 + 1 round-3) is the running total across all
three rounds - disclosed alongside every result, exactly the same
principle as `research/candidate_registry_round2.py`'s own warning and
`research/scorecard.py::multiple_testing_warning`.
"""

from __future__ import annotations

from trading_agent.research.candidate_registry import CANDIDATE_REGISTRY, CandidateSpec
from trading_agent.research.candidate_registry_round2 import ROUND2_CANDIDATE_REGISTRY
from trading_agent.research.candidates.multitimeframe_breakout import (
    CONFIRMATION_WINDOW_1H_CANDLES,
    FOUR_H_ATR_PERIOD,
    FOUR_H_BREAKOUT_ATR_MULTIPLE,
    FOUR_H_CHANNEL_PERIOD,
    FOUR_H_EMA_REGIME_PERIOD,
    FOUR_H_EMA_SLOPE_LOOKBACK_CANDLES,
    WEEKLY_EMA_PERIOD,
    WEEKLY_EMA_SLOPE_LOOKBACK_CANDLES,
    MultiTimeframeBreakoutStrategy,
)
from trading_agent.research.candidates.multitimeframe_breakout import (
    FAMILY as MULTITIMEFRAME_BREAKOUT_FAMILY,
)

ROUND_NUMBER = 3

#: The operating interval this candidate MUST be evaluated at - its own
#: setup/confirmation logic derives 4h and weekly candles by aggregating
#: THIS 1h stream (see multitimeframe_breakout.py's module docstring);
#: running it against any other stored interval would silently starve it
#: of the 1h granularity its own confirmation layer requires.
REQUIRED_MARKET_INTERVAL = "1h"


def _e1_multitimeframe_breakout() -> CandidateSpec:
    params: dict[str, float | int] = {
        "weekly_ema_period": WEEKLY_EMA_PERIOD,
        "weekly_ema_slope_lookback_candles": WEEKLY_EMA_SLOPE_LOOKBACK_CANDLES,
        "four_h_channel_period": FOUR_H_CHANNEL_PERIOD,
        "four_h_atr_period": FOUR_H_ATR_PERIOD,
        "four_h_breakout_atr_multiple": FOUR_H_BREAKOUT_ATR_MULTIPLE,
        "four_h_ema_regime_period": FOUR_H_EMA_REGIME_PERIOD,
        "four_h_ema_slope_lookback_candles": FOUR_H_EMA_SLOPE_LOOKBACK_CANDLES,
        "one_h_confirmation_window_candles": CONFIRMATION_WINDOW_1H_CANDLES,
    }
    return CandidateSpec(
        candidate_id="multitimeframe_breakout_E1_round3",
        family=MULTITIMEFRAME_BREAKOUT_FAMILY,
        params=params,
        build=lambda: MultiTimeframeBreakoutStrategy(),
    )


#: Exactly ONE round-3 candidate - see module docstring.
ROUND3_CANDIDATE_REGISTRY: tuple[CandidateSpec, ...] = (_e1_multitimeframe_breakout(),)

CUMULATIVE_CANDIDATE_CONFIGURATIONS_EXAMINED = (
    len(CANDIDATE_REGISTRY) + len(ROUND2_CANDIDATE_REGISTRY) + len(ROUND3_CANDIDATE_REGISTRY)
)

ROUND3_MULTIPLE_TESTING_WARNING = (
    f"ROUND {ROUND_NUMBER} HYPOTHESIS - NOT AN UNTOUCHED TEST: multitimeframe_breakout_E1_round3 is examined "
    "AFTER round 1's nine candidates (all REJECTED/INSUFFICIENT_EVIDENCE) and round 2's "
    "breakout_regime_D1_round2 (OFFICIAL REJECTED verdict, already observed and PRESERVED UNCHANGED here - "
    "see research/round2_report.py, never recomputed or contradicted by anything in this round) were already "
    "evaluated and their results already observed. Cumulative candidate configurations examined across all "
    f"three rounds so far: {CUMULATIVE_CANDIDATE_CONFIGURATIONS_EXAMINED} "
    f"({len(CANDIDATE_REGISTRY)} round-1 + {len(ROUND2_CANDIDATE_REGISTRY)} round-2 + "
    f"{len(ROUND3_CANDIDATE_REGISTRY)} round-3). Every additional configuration examined after seeing prior "
    "results increases the chance that an apparently-improved outcome reflects multiple-testing/selection "
    "bias rather than a genuine effect - the same principle as research/scorecard.py's own multiple-testing "
    "warning, now compounding across three rounds. A RESEARCH_SURVIVOR verdict for E1 (if it occurs) carries "
    "this caveat permanently and is NOT a claim of profitability or approval for live/Testnet trading."
)
