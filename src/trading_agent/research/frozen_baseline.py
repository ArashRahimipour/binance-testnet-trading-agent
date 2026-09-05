"""The rejected v0.1 EMA-crossover baseline, frozen for regression comparison.

`ema_crossover_v0_1_rejected` is preserved EXACTLY as it was evaluated when
formally rejected against the observed 2025-05-16 to 2026-09-04 test
period (see `FROZEN_BASELINE_VERDICT`). It is NEVER modified, tuned, or
re-optimized - it exists solely as a fixed regression point.

`reproduce_frozen_baseline_report` is the ONLY function in this codebase
allowed to touch the consumed 2025-05-16..2026-09-04 period, and it takes
no candidate parameter at all - there is structurally nothing for it to
score but this one frozen configuration, via the unmodified `run_backtest`/
`run_independent_holdout_evaluation` engine.
"""

from __future__ import annotations

from dataclasses import dataclass

from trading_agent.backtest.engine import (
    BacktestResult,
    HoldoutEvaluationResult,
    run_backtest,
    run_independent_holdout_evaluation,
)
from trading_agent.config.models import AppConfig, StrategyConfig
from trading_agent.data.models import Candle
from trading_agent.journal.journal import Journal
from trading_agent.sizing.exchange_filters import SymbolFilters

#: Frozen identifier - use this exact string wherever the rejected v0.1
#: baseline is referenced (reports, tests, comparisons). Never reused for
#: any research candidate.
FROZEN_BASELINE_ID = "ema_crossover_v0_1_rejected"

#: Exactly the strategy parameters the rejected baseline was evaluated
#: with (config/default.yaml's strategy section at the time of rejection) -
#: frozen here so a future change to the live default config can never
#: silently change what this regression point reproduces.
FROZEN_BASELINE_STRATEGY_CONFIG = StrategyConfig(name="ema_crossover_trend", ema_fast=20, ema_slow=50)

#: The formal rejection verdict, verbatim from the observed test-period
#: evidence. Recorded here so it travels with the code, not only a chat
#: transcript.
FROZEN_BASELINE_VERDICT = (
    "REJECTED. Independent holdout test window (post-gap segment, from 2025-05-16): 27 closed "
    "trades plus one open position; headline marked-to-market return -1.22%; max drawdown "
    "14.07%; closed-trade bootstrap mean return -9.67% (95% CI [-21.65%, +3.75%]); "
    "chronological rolling groups -2.99%, -7.02%, +0.26%; max consecutive losses 7; "
    "buy-and-hold over the same window -22.35%. The train window separately latched "
    "MAX_DRAWDOWN_SHUTDOWN after rejecting 59 of 83 BUY signals (16.43% max drawdown, 23 "
    "closed trades). This verdict is FINAL for ema_crossover_v0_1_rejected - it is not "
    "re-evaluated, tuned, or reconsidered; it exists only as a frozen comparison point for "
    "future research candidates."
)


@dataclass(frozen=True, slots=True)
class FrozenBaselineReport:
    baseline_id: str
    verdict: str
    continuous: BacktestResult
    holdout: HoldoutEvaluationResult


def reproduce_frozen_baseline_report(
    candles: list[Candle],
    config: AppConfig,
    filters: SymbolFilters,
    journal: Journal | None = None,
) -> FrozenBaselineReport:
    """Reproduce the frozen v0.1 EMA baseline's report - and ONLY that
    report - over `candles`. This is the one function permitted to receive
    candles at or after the research cutoff (`research/cutoff.py`), since
    it takes no candidate parameter: there is nothing for a caller to
    substitute or score here. `config.strategy` is IGNORED and replaced
    with `FROZEN_BASELINE_STRATEGY_CONFIG` so a change to the live default
    config can never silently change what this reproduces; every other
    section of `config` (fees, slippage, risk, sizing, stop-loss,
    starting_equity) is used as given, unmodified.
    """
    frozen_config = config.model_copy(update={"strategy": FROZEN_BASELINE_STRATEGY_CONFIG})
    continuous = run_backtest(candles, frozen_config, filters, journal)
    holdout = run_independent_holdout_evaluation(candles, frozen_config, filters, journal)
    return FrozenBaselineReport(
        baseline_id=FROZEN_BASELINE_ID,
        verdict=FROZEN_BASELINE_VERDICT,
        continuous=continuous,
        holdout=holdout,
    )
