"""ROUND-2 candidate registry: exactly ONE new candidate, examined only
AFTER round 1's real pre-cutoff results were available.

This is explicitly NOT a blind, pre-round-1 declared search space like
`candidate_registry.py`'s nine (`CANDIDATE_REGISTRY`, declared and frozen
before any of them ever saw data). `breakout_regime_D1_round2` exists
BECAUSE round 1's real evaluation showed `breakout_B1` had broad
trade-level profitability but sustained losses during an unfavorable
2021-2022 regime - see CHANGELOG.md and `research/candidates/
breakout_regime_gate.py`'s own docstring for the economic hypothesis this
tests.

Evaluating a hypothesis chosen because of an observed result on the same
family of historical data is a well-known source of selection-bias/
multiple-testing distortion, even though (as here) D1's own parameters are
declared and fixed BEFORE it is itself scored, and even though it changes
nothing about B1's breakout sensitivity or the risk policy. This module
never claims otherwise: `ROUND2_MULTIPLE_TESTING_WARNING` is printed
alongside every D1 result, and D1 is never presented as an untouched,
pre-registered test.

`CUMULATIVE_CANDIDATE_CONFIGURATIONS_EXAMINED` (10 = the original nine +
this one) is the running total across BOTH rounds - it must be disclosed
alongside any result, since it is exactly the count that determines how
suspicious an apparently-good result should be (see `research/
scorecard.py::multiple_testing_warning` for the same principle applied to
round 1's own nine).
"""

from __future__ import annotations

from trading_agent.research.candidate_registry import CANDIDATE_REGISTRY, CandidateSpec
from trading_agent.research.candidates.breakout_regime_gate import (
    FAMILY as BREAKOUT_REGIME_GATE_FAMILY,
)
from trading_agent.research.candidates.breakout_regime_gate import (
    BreakoutWithBullishRegimeGateStrategy,
)

ROUND_NUMBER = 2

#: B1's own exact parameters (`research/candidate_registry.py::
#: CANDIDATE_REGISTRY`, candidate_id="breakout_B1") - preserved unchanged.
#: Never tuned after seeing a D1 result.
_D1_CHANNEL_PERIOD = 20
_D1_ATR_PERIOD = 14
_D1_BREAKOUT_ATR_MULTIPLE = 0.25


def _d1_breakout_regime() -> CandidateSpec:
    params: dict[str, float | int] = {
        "channel_period": _D1_CHANNEL_PERIOD,
        "atr_period": _D1_ATR_PERIOD,
        "breakout_atr_multiple": _D1_BREAKOUT_ATR_MULTIPLE,
    }
    return CandidateSpec(
        candidate_id="breakout_regime_D1_round2",
        family=BREAKOUT_REGIME_GATE_FAMILY,
        params=params,
        build=lambda: BreakoutWithBullishRegimeGateStrategy(
            channel_period=_D1_CHANNEL_PERIOD, atr_period=_D1_ATR_PERIOD,
            breakout_atr_multiple=_D1_BREAKOUT_ATR_MULTIPLE,
        ),
    )


#: Exactly ONE round-2 candidate - see module docstring.
ROUND2_CANDIDATE_REGISTRY: tuple[CandidateSpec, ...] = (_d1_breakout_regime(),)

CUMULATIVE_CANDIDATE_CONFIGURATIONS_EXAMINED = len(CANDIDATE_REGISTRY) + len(ROUND2_CANDIDATE_REGISTRY)

ROUND2_MULTIPLE_TESTING_WARNING = (
    f"ROUND {ROUND_NUMBER} HYPOTHESIS - NOT AN UNTOUCHED TEST: breakout_regime_D1_round2 was chosen "
    "BECAUSE OF an observed round-1 result (breakout_B1's 2021-2022 regime losses), not declared "
    "blind before any evaluation ever ran. Cumulative candidate configurations examined across both "
    f"rounds so far: {CUMULATIVE_CANDIDATE_CONFIGURATIONS_EXAMINED} "
    f"({len(CANDIDATE_REGISTRY)} round-1 + {len(ROUND2_CANDIDATE_REGISTRY)} round-2). Every additional "
    "configuration examined after seeing a result increases the chance that an apparently-improved "
    "outcome reflects multiple-testing/selection bias rather than a genuine effect - this is the same "
    "principle as research/scorecard.py's own multiple-testing warning, now compounding across "
    "rounds. A RESEARCH_SURVIVOR verdict for D1 (if it occurs) carries this caveat permanently and is "
    "NOT a claim of profitability or approval for live/Testnet trading."
)
