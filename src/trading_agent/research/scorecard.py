"""Rule-based robustness scorecard for a candidate's walk-forward result.

Every threshold here is DECLARED in code, fixed the same for all candidates
in `candidate_registry.py`, BEFORE any candidate is scored - never fitted,
tuned, or chosen after seeing a result. Scoring is a PASS/FAIL test against
these fixed thresholds, never a "pick the best-performing candidate"
ranking - see `MULTIPLE_TESTING_WARNING` for why that distinction matters.
There is no ML, optimizer, or search of any kind in this module.

Final status is always exactly one of:
  - REJECTED - failed one or more pre-declared robustness criteria.
  - RESEARCH_SURVIVOR - passed every pre-declared robustness criterion.
    NEVER "profitable" or "approved for live trading" - see
    `RESEARCH_SURVIVOR_CAVEAT`. Must be frozen (`research/freeze.py`)
    before any further paper testing.
  - INSUFFICIENT_EVIDENCE - too few trades across all folds to judge
    either way.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

from trading_agent.research.walk_forward import CandidateWalkForwardResult


class ScorecardStatus(str, Enum):
    REJECTED = "REJECTED"
    RESEARCH_SURVIVOR = "RESEARCH_SURVIVOR"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


# --- Declared, fixed robustness thresholds - see module docstring. ---
MIN_TOTAL_TRADES_FOR_EVIDENCE = 15
MIN_FOLDS_WITH_A_TRADE = 3
MATERIALLY_NEGATIVE_FOLD_RETURN_PCT = -15.0
MAX_ACCEPTABLE_DRAWDOWN_PCT = 25.0
MAX_BEST_TRADE_CONTRIBUTION_PCT = 75.0
MIN_POSITIVE_FOLD_FRACTION = 0.6

RESEARCH_SURVIVOR_CAVEAT = (
    "RESEARCH_SURVIVOR means this candidate passed every pre-declared robustness threshold on "
    "PRE-CUTOFF development data. It is NOT a claim of profitability and NOT approval for live "
    "or Testnet trading. It has not been tested on any data after the research cutoff, must be "
    "frozen before any further test (see research/freeze.py), and its only valid next test is "
    "genuinely new candles arriving after this evaluation - never previously observed data."
)

INSUFFICIENT_EVIDENCE_CAVEAT = (
    "Too few trades occurred across too few folds to judge this candidate's robustness either "
    "way. This is NOT a pass, and it is not evidence of a working strategy - it means the "
    "declared entry conditions rarely fired on this data."
)

REJECTED_CAVEAT = (
    "REJECTED means this candidate failed at least one pre-declared robustness criterion on "
    "pre-cutoff development data. See `reasons` for exactly which one(s)."
)


def multiple_testing_warning(candidate_count: int) -> str:
    return (
        f"{candidate_count} candidates were evaluated together in this run. Selecting whichever "
        "one happens to look best after the fact is a classic multiple-comparisons/selection-bias "
        "trap: with enough candidates, some will look good on historical data by chance alone. "
        "This scorecard does NOT rank or pick a 'best' candidate - each one is tested "
        "independently against the SAME pre-declared thresholds, and more than one (or none) may "
        "become a RESEARCH_SURVIVOR. A RESEARCH_SURVIVOR is a candidate that cleared a bar, not "
        "the winner of a competition."
    )


@dataclass(frozen=True, slots=True)
class CriterionResult:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class ScorecardEntry:
    candidate_id: str
    family: str
    params: dict[str, float | int]
    status: ScorecardStatus
    criteria: list[CriterionResult]
    reasons: list[str]  # detail of every FAILED criterion only, for a quick read
    total_trade_count: int
    folds_with_a_trade: int
    fold_count: int
    median_fold_return_pct: float | None
    aggregate_realized_pnl_quote: Decimal
    worst_fold_return_pct: float | None
    worst_fold_max_drawdown_pct: float | None
    positive_fold_fraction: float | None
    max_best_trade_contribution_pct: float | None
    mean_buy_and_hold_return_pct: float | None
    caveat: str


def score_candidate(result: CandidateWalkForwardResult) -> ScorecardEntry:
    evaluated = [f for f in result.folds if f.performance is not None]
    total_trades = sum(f.performance.trade_count for f in evaluated if f.performance is not None)
    folds_with_a_trade = sum(1 for f in evaluated if f.performance is not None and f.performance.trade_count > 0)

    evidence_ok = total_trades >= MIN_TOTAL_TRADES_FOR_EVIDENCE and folds_with_a_trade >= MIN_FOLDS_WITH_A_TRADE
    if not evidence_ok:
        detail = (
            f"total_trades={total_trades} (need >= {MIN_TOTAL_TRADES_FOR_EVIDENCE}), "
            f"folds_with_a_trade={folds_with_a_trade} (need >= {MIN_FOLDS_WITH_A_TRADE})"
        )
        criteria = [CriterionResult("sufficient_evidence", False, detail)]
        return ScorecardEntry(
            candidate_id=result.candidate_id, family=result.family, params=result.params,
            status=ScorecardStatus.INSUFFICIENT_EVIDENCE, criteria=criteria, reasons=[detail],
            total_trade_count=total_trades, folds_with_a_trade=folds_with_a_trade, fold_count=len(evaluated),
            median_fold_return_pct=None, aggregate_realized_pnl_quote=Decimal(0),
            worst_fold_return_pct=None, worst_fold_max_drawdown_pct=None, positive_fold_fraction=None,
            max_best_trade_contribution_pct=None, mean_buy_and_hold_return_pct=None,
            caveat=INSUFFICIENT_EVIDENCE_CAVEAT,
        )

    fold_returns = [f.performance.total_return_pct for f in evaluated if f.performance is not None]
    fold_drawdowns = [f.performance.max_drawdown_pct for f in evaluated if f.performance is not None]
    median_fold_return = statistics.median(fold_returns)
    worst_fold_return = min(fold_returns)
    worst_fold_dd = max(fold_drawdowns)
    positive_fraction = sum(1 for r in fold_returns if r > 0) / len(fold_returns)
    aggregate_realized = sum(
        (f.extended.pnl_breakdown.realized_closed_trade_pnl_quote for f in evaluated if f.extended is not None),
        Decimal(0),
    )
    best_trade_contributions = [
        f.extended.trade_distribution.best_trade_contribution_pct
        for f in evaluated
        if f.extended is not None and f.extended.trade_distribution is not None
        and f.extended.trade_distribution.best_trade_contribution_pct is not None
    ]
    max_best_trade_contribution = max(best_trade_contributions) if best_trade_contributions else None
    bh_returns = [
        f.performance.buy_and_hold_return_pct for f in evaluated
        if f.performance is not None and f.performance.buy_and_hold_return_pct is not None
    ]
    mean_bh = sum(bh_returns) / len(bh_returns) if bh_returns else None

    criteria = [
        CriterionResult(
            "positive_median_fold_return", median_fold_return > 0,
            f"median_fold_return_pct={median_fold_return:.2f} (need > 0)",
        ),
        CriterionResult(
            "positive_aggregate_realized_pnl", aggregate_realized > 0,
            f"aggregate_realized_pnl_quote={aggregate_realized} (need > 0)",
        ),
        CriterionResult(
            "no_materially_negative_fold", worst_fold_return >= MATERIALLY_NEGATIVE_FOLD_RETURN_PCT,
            f"worst_fold_return_pct={worst_fold_return:.2f} (need >= {MATERIALLY_NEGATIVE_FOLD_RETURN_PCT})",
        ),
        CriterionResult(
            "acceptable_max_drawdown", worst_fold_dd <= MAX_ACCEPTABLE_DRAWDOWN_PCT,
            f"worst_fold_max_drawdown_pct={worst_fold_dd:.2f} (need <= {MAX_ACCEPTABLE_DRAWDOWN_PCT})",
        ),
        CriterionResult(
            "limited_best_trade_dependence",
            max_best_trade_contribution is None or max_best_trade_contribution <= MAX_BEST_TRADE_CONTRIBUTION_PCT,
            f"max_best_trade_contribution_pct={max_best_trade_contribution} (need <= {MAX_BEST_TRADE_CONTRIBUTION_PCT} or undefined)",
        ),
        CriterionResult(
            "stable_across_folds", positive_fraction >= MIN_POSITIVE_FOLD_FRACTION,
            f"positive_fold_fraction={positive_fraction:.2f} (need >= {MIN_POSITIVE_FOLD_FRACTION})",
        ),
    ]
    failed = [c.detail for c in criteria if not c.passed]
    status = ScorecardStatus.RESEARCH_SURVIVOR if not failed else ScorecardStatus.REJECTED

    return ScorecardEntry(
        candidate_id=result.candidate_id, family=result.family, params=result.params,
        status=status, criteria=criteria, reasons=failed,
        total_trade_count=total_trades, folds_with_a_trade=folds_with_a_trade, fold_count=len(evaluated),
        median_fold_return_pct=median_fold_return, aggregate_realized_pnl_quote=aggregate_realized,
        worst_fold_return_pct=worst_fold_return, worst_fold_max_drawdown_pct=worst_fold_dd,
        positive_fold_fraction=positive_fraction, max_best_trade_contribution_pct=max_best_trade_contribution,
        mean_buy_and_hold_return_pct=mean_bh,
        caveat=RESEARCH_SURVIVOR_CAVEAT if status == ScorecardStatus.RESEARCH_SURVIVOR else REJECTED_CAVEAT,
    )


@dataclass(frozen=True, slots=True)
class Scorecard:
    entries: list[ScorecardEntry] = field(default_factory=list)
    multiple_testing_warning: str = ""


def build_scorecard(results: list[CandidateWalkForwardResult]) -> Scorecard:
    entries = [score_candidate(r) for r in results]
    return Scorecard(entries=entries, multiple_testing_warning=multiple_testing_warning(len(results)))
