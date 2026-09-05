"""The declared, fixed candidate search space for this research phase.

Exactly NINE configurations - three per family - chosen as simple, round,
textbook-standard parameter values (a classic Donchian 20/40/55 lookback
set, MACD-style 12/26 EMA periods, a standard 20-period/2-std-dev
Bollinger setup) BEFORE any of them were run against pre-cutoff data -
never fitted, tuned, or adjusted against a result. This tuple is declared
here, in code, and frozen before evaluation: `research/walk_forward.py`
iterates over exactly this list and nothing else. There is no grid search,
no optimizer, no genetic/Bayesian search, and no machine learning anywhere
in this module or its callers that could add, remove, or adjust an entry
based on a result.

Multiple-testing note: evaluating nine candidates and reporting whichever
happens to look best would introduce selection bias (see
`research/scorecard.py`'s prominent multiple-testing warning). The
robustness rule in `scorecard.py` is a PRE-DECLARED, threshold-based
pass/fail test applied identically to all nine - never a "pick the
best-performing one" ranking.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from trading_agent.research.candidates.mean_reversion import FAMILY as MEAN_REVERSION_FAMILY
from trading_agent.research.candidates.mean_reversion import ConservativeMeanReversionStrategy
from trading_agent.research.candidates.trend_regime import FAMILY as TREND_REGIME_FAMILY
from trading_agent.research.candidates.trend_regime import TrendWithRegimeFilterStrategy
from trading_agent.research.candidates.volatility_breakout import FAMILY as BREAKOUT_FAMILY
from trading_agent.research.candidates.volatility_breakout import (
    VolatilityNormalizedBreakoutStrategy,
)
from trading_agent.strategy.base import CandidateStrategy


@dataclass(frozen=True, slots=True)
class CandidateSpec:
    candidate_id: str
    family: str
    params: dict[str, float | int]
    build: Callable[[], CandidateStrategy]


def _trend_regime(
    candidate_id: str, ema_fast: int, ema_slow: int, atr_period: int, min_trend_strength_atr: float
) -> CandidateSpec:
    params: dict[str, float | int] = {
        "ema_fast": ema_fast, "ema_slow": ema_slow, "atr_period": atr_period,
        "min_trend_strength_atr": min_trend_strength_atr,
    }
    return CandidateSpec(
        candidate_id, TREND_REGIME_FAMILY, params,
        lambda: TrendWithRegimeFilterStrategy(
            ema_fast=ema_fast, ema_slow=ema_slow, atr_period=atr_period, min_trend_strength_atr=min_trend_strength_atr
        ),
    )


def _breakout(candidate_id: str, channel_period: int, atr_period: int, breakout_atr_multiple: float) -> CandidateSpec:
    params: dict[str, float | int] = {
        "channel_period": channel_period, "atr_period": atr_period, "breakout_atr_multiple": breakout_atr_multiple,
    }
    return CandidateSpec(
        candidate_id, BREAKOUT_FAMILY, params,
        lambda: VolatilityNormalizedBreakoutStrategy(
            channel_period=channel_period, atr_period=atr_period, breakout_atr_multiple=breakout_atr_multiple
        ),
    )


def _mean_reversion(
    candidate_id: str, bb_period: int, bb_std_mult: float, atr_period: int, max_trend_strength_atr: float
) -> CandidateSpec:
    params: dict[str, float | int] = {
        "bb_period": bb_period, "bb_std_mult": bb_std_mult, "atr_period": atr_period,
        "max_trend_strength_atr": max_trend_strength_atr,
    }
    return CandidateSpec(
        candidate_id, MEAN_REVERSION_FAMILY, params,
        lambda: ConservativeMeanReversionStrategy(
            bb_period=bb_period, bb_std_mult=bb_std_mult, atr_period=atr_period,
            max_trend_strength_atr=max_trend_strength_atr,
        ),
    )


#: The complete, declared candidate search space - see module docstring.
CANDIDATE_REGISTRY: tuple[CandidateSpec, ...] = (
    _trend_regime("trend_regime_A1", ema_fast=10, ema_slow=30, atr_period=14, min_trend_strength_atr=1.0),
    _trend_regime("trend_regime_A2", ema_fast=20, ema_slow=50, atr_period=14, min_trend_strength_atr=1.5),
    _trend_regime("trend_regime_A3", ema_fast=12, ema_slow=26, atr_period=20, min_trend_strength_atr=2.0),
    _breakout("breakout_B1", channel_period=20, atr_period=14, breakout_atr_multiple=0.25),
    _breakout("breakout_B2", channel_period=40, atr_period=14, breakout_atr_multiple=0.5),
    _breakout("breakout_B3", channel_period=55, atr_period=20, breakout_atr_multiple=0.75),
    _mean_reversion("mean_reversion_C1", bb_period=20, bb_std_mult=2.0, atr_period=14, max_trend_strength_atr=0.5),
    _mean_reversion("mean_reversion_C2", bb_period=20, bb_std_mult=2.5, atr_period=14, max_trend_strength_atr=0.75),
    _mean_reversion("mean_reversion_C3", bb_period=30, bb_std_mult=2.0, atr_period=20, max_trend_strength_atr=1.0),
)

TOTAL_CANDIDATE_COUNT = len(CANDIDATE_REGISTRY)
