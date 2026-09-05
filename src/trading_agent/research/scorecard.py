"""Rule-based robustness scorecard for a candidate's blocked chronological
evaluation result (`research/blocked_chronological_evaluation.py` - NOT
walk-forward optimization; see that module's docstring).

Every threshold here is DECLARED in code, fixed the same for all candidates
in `candidate_registry.py`, BEFORE any candidate is scored - never fitted,
tuned, or chosen after seeing a result. Scoring is a PASS/FAIL test against
these fixed thresholds, never a "pick the best-performing candidate"
ranking - see `MULTIPLE_TESTING_WARNING` for why that distinction matters.
There is no ML, optimizer, or search of any kind in this module.

Survivor status is scored on REALIZED closed-trade PnL only (`extended.
pnl_breakdown.realized_closed_trade_pnl_quote`, normalized by each block's
own starting equity) - NEVER on marked-to-market total return, which can
include a large paper gain (or loss) on a position that is still open and
could reverse before ever being realized. An unfinished, still-open
position can therefore never by itself make a block - or a candidate -
pass. Marked-to-market total return is still reported separately
(`median_fold_marked_to_market_return_pct` etc.) for visibility, just never
used as a pass/fail input.

Final status is always exactly one of:
  - REJECTED - failed one or more pre-declared robustness criteria.
  - RESEARCH_SURVIVOR - passed every pre-declared robustness criterion.
    NEVER "profitable" or "approved for live trading" - see
    `RESEARCH_SURVIVOR_CAVEAT`. Must be frozen (`research/freeze.py`)
    before any further paper testing.
  - INSUFFICIENT_EVIDENCE - too few trades across all blocks to judge
    either way.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

from trading_agent.research.blocked_chronological_evaluation import (
    BlockResult,
    CandidateBlockedChronologicalResult,
)


class ScorecardStatus(str, Enum):
    REJECTED = "REJECTED"
    RESEARCH_SURVIVOR = "RESEARCH_SURVIVOR"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


# --- Declared, fixed robustness thresholds - see module docstring. ---
MIN_TOTAL_TRADES_FOR_EVIDENCE = 30
MIN_BLOCKS_WITH_A_TRADE = 4
MAX_ACCEPTABLE_DRAWDOWN_PCT = 15.0
MATERIALLY_NEGATIVE_BLOCK_REALIZED_RETURN_PCT = -10.0
MAX_BEST_TRADE_CONTRIBUTION_PCT = 50.0
MIN_POSITIVE_REALIZED_PNL_BLOCK_FRACTION = 0.6

RESEARCH_SURVIVOR_CAVEAT = (
    "RESEARCH_SURVIVOR means this candidate passed every pre-declared robustness threshold, "
    "scored on REALIZED closed-trade PnL only, on PRE-CUTOFF development data. It is NOT a claim "
    "of profitability and NOT approval for live or Testnet trading. It has not been tested on any "
    "data after the research cutoff, must be frozen before any further test (see "
    "research/freeze.py), and its only valid next test is genuinely new candles arriving after "
    "this evaluation - never previously observed data."
)

INSUFFICIENT_EVIDENCE_CAVEAT = (
    "Too few trades occurred across too few blocks to judge this candidate's robustness either "
    "way. This is NOT a pass, and it is not evidence of a working strategy - it means the "
    "declared entry conditions rarely fired on this data. Zero trades is always insufficient "
    "evidence, regardless of any other figure."
)

REJECTED_CAVEAT = (
    "REJECTED means this candidate failed at least one pre-declared robustness criterion on "
    "pre-cutoff development data. See `reasons` for exactly which one(s)."
)

BENCHMARK_NOTE = (
    "excess_return_pct (strategy total marked-to-market return minus buy-and-hold return, same "
    "block/dates) is reported for every block, along with the median across blocks, purely for "
    "visibility - it is NEVER a pass/fail criterion, and beating buy-and-hold in every bullish "
    "block is never required. Absolute profitability and drawdown control remain the primary "
    "bar: a risk-managed strategy's job is capital preservation and a smoother realized equity "
    "curve, not outrunning a passive holding during a strong rally - a candidate that loses less "
    "than buy-and-hold in a crash, or forgoes some upside in a rally while controlling drawdown, "
    "is doing its job even with a negative median excess return."
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
    blocks_with_a_trade: int
    block_count: int
    # --- Realized-PnL-based figures actually used for scoring. ---
    median_block_realized_return_pct: float | None
    worst_block_realized_return_pct: float | None
    aggregate_realized_pnl_quote: Decimal
    positive_realized_pnl_block_fraction: float | None
    max_best_trade_contribution_pct: float | None
    # --- Reported for visibility only - never used for survivor status. ---
    worst_block_max_drawdown_pct: float | None
    median_block_marked_to_market_return_pct: float | None
    median_excess_return_vs_buy_and_hold_pct: float | None
    benchmark_note: str
    caveat: str


def _realized_return_pct(block: BlockResult) -> float:
    """REALIZED closed-trade PnL as a percentage of the block's own
    starting equity - never marked-to-market, so an unfinished open
    position never contributes. Falls back to 0.0 (neither a pass nor a
    materially-negative signal) for a block with no usable data."""
    if block.extended is None or block.performance is None:
        return 0.0
    starting_equity = block.performance.starting_equity
    if starting_equity <= 0:
        return 0.0
    return float(block.extended.pnl_breakdown.realized_closed_trade_pnl_quote / starting_equity * 100)


def _excess_return_pct(block: BlockResult) -> float | None:
    """Strategy total (marked-to-market) return minus buy-and-hold return
    over the exact same block - reported for visibility only, see
    `BENCHMARK_NOTE`."""
    if block.performance is None or block.performance.buy_and_hold_return_pct is None:
        return None
    return block.performance.total_return_pct - block.performance.buy_and_hold_return_pct


def score_candidate(result: CandidateBlockedChronologicalResult) -> ScorecardEntry:
    evaluated = [b for b in result.blocks if b.performance is not None]
    total_trades = sum(b.performance.trade_count for b in evaluated if b.performance is not None)
    blocks_with_a_trade = sum(1 for b in evaluated if b.performance is not None and b.performance.trade_count > 0)

    evidence_ok = total_trades >= MIN_TOTAL_TRADES_FOR_EVIDENCE and blocks_with_a_trade >= MIN_BLOCKS_WITH_A_TRADE
    if not evidence_ok:
        detail = (
            f"total_trades={total_trades} (need >= {MIN_TOTAL_TRADES_FOR_EVIDENCE}), "
            f"blocks_with_a_trade={blocks_with_a_trade} (need >= {MIN_BLOCKS_WITH_A_TRADE})"
        )
        criteria = [CriterionResult("sufficient_evidence", False, detail)]
        return ScorecardEntry(
            candidate_id=result.candidate_id, family=result.family, params=result.params,
            status=ScorecardStatus.INSUFFICIENT_EVIDENCE, criteria=criteria, reasons=[detail],
            total_trade_count=total_trades, blocks_with_a_trade=blocks_with_a_trade, block_count=len(evaluated),
            median_block_realized_return_pct=None, worst_block_realized_return_pct=None,
            aggregate_realized_pnl_quote=Decimal(0), positive_realized_pnl_block_fraction=None,
            max_best_trade_contribution_pct=None, worst_block_max_drawdown_pct=None,
            median_block_marked_to_market_return_pct=None, median_excess_return_vs_buy_and_hold_pct=None,
            benchmark_note=BENCHMARK_NOTE, caveat=INSUFFICIENT_EVIDENCE_CAVEAT,
        )

    realized_returns = [_realized_return_pct(b) for b in evaluated]
    median_realized_return = statistics.median(realized_returns)
    worst_realized_return = min(realized_returns)
    positive_realized_fraction = sum(1 for r in realized_returns if r > 0) / len(realized_returns)
    aggregate_realized = sum(
        (b.extended.pnl_breakdown.realized_closed_trade_pnl_quote for b in evaluated if b.extended is not None),
        Decimal(0),
    )
    worst_block_dd = max(b.performance.max_drawdown_pct for b in evaluated if b.performance is not None)
    mtm_returns = [b.performance.total_return_pct for b in evaluated if b.performance is not None]
    median_mtm_return = statistics.median(mtm_returns) if mtm_returns else None
    excess_returns = [e for e in (_excess_return_pct(b) for b in evaluated) if e is not None]
    median_excess_return = statistics.median(excess_returns) if excess_returns else None

    # Best-trade dependence is only meaningful for a block that actually
    # made money overall - a losing block's "contribution ratio" (a
    # positive trade divided by a negative total) is not a dependence
    # signal, so it is excluded rather than passed through. If NO block
    # ever had a positive, well-defined contribution ratio, this is
    # undefined and therefore FAILS (never auto-passes on undefined).
    positive_block_contributions = [
        b.extended.trade_distribution.best_trade_contribution_pct
        for b in evaluated
        if b.extended is not None and b.extended.trade_distribution is not None
        and b.extended.trade_distribution.best_trade_contribution_pct is not None
        and b.extended.pnl_breakdown.realized_closed_trade_pnl_quote > 0
    ]
    max_best_trade_contribution = max(positive_block_contributions) if positive_block_contributions else None
    best_trade_dependence_passed = (
        max_best_trade_contribution is not None and max_best_trade_contribution <= MAX_BEST_TRADE_CONTRIBUTION_PCT
    )

    criteria = [
        CriterionResult(
            "positive_median_block_realized_return", median_realized_return > 0,
            f"median_block_realized_return_pct={median_realized_return:.2f} (need > 0)",
        ),
        CriterionResult(
            "positive_aggregate_realized_pnl", aggregate_realized > 0,
            f"aggregate_realized_pnl_quote={aggregate_realized} (need > 0)",
        ),
        CriterionResult(
            "no_materially_negative_block",
            worst_realized_return >= MATERIALLY_NEGATIVE_BLOCK_REALIZED_RETURN_PCT,
            f"worst_block_realized_return_pct={worst_realized_return:.2f} "
            f"(need >= {MATERIALLY_NEGATIVE_BLOCK_REALIZED_RETURN_PCT})",
        ),
        CriterionResult(
            "acceptable_max_drawdown", worst_block_dd <= MAX_ACCEPTABLE_DRAWDOWN_PCT,
            f"worst_block_max_drawdown_pct={worst_block_dd:.2f} (need <= {MAX_ACCEPTABLE_DRAWDOWN_PCT})",
        ),
        CriterionResult(
            "limited_best_trade_dependence", best_trade_dependence_passed,
            f"max_best_trade_contribution_pct={max_best_trade_contribution} "
            f"(need a defined value <= {MAX_BEST_TRADE_CONTRIBUTION_PCT} - undefined never passes)",
        ),
        CriterionResult(
            "stable_across_blocks", positive_realized_fraction >= MIN_POSITIVE_REALIZED_PNL_BLOCK_FRACTION,
            f"positive_realized_pnl_block_fraction={positive_realized_fraction:.2f} "
            f"(need >= {MIN_POSITIVE_REALIZED_PNL_BLOCK_FRACTION})",
        ),
    ]
    failed = [c.detail for c in criteria if not c.passed]
    status = ScorecardStatus.RESEARCH_SURVIVOR if not failed else ScorecardStatus.REJECTED

    return ScorecardEntry(
        candidate_id=result.candidate_id, family=result.family, params=result.params,
        status=status, criteria=criteria, reasons=failed,
        total_trade_count=total_trades, blocks_with_a_trade=blocks_with_a_trade, block_count=len(evaluated),
        median_block_realized_return_pct=median_realized_return, worst_block_realized_return_pct=worst_realized_return,
        aggregate_realized_pnl_quote=aggregate_realized, positive_realized_pnl_block_fraction=positive_realized_fraction,
        max_best_trade_contribution_pct=max_best_trade_contribution, worst_block_max_drawdown_pct=worst_block_dd,
        median_block_marked_to_market_return_pct=median_mtm_return,
        median_excess_return_vs_buy_and_hold_pct=median_excess_return,
        benchmark_note=BENCHMARK_NOTE,
        caveat=RESEARCH_SURVIVOR_CAVEAT if status == ScorecardStatus.RESEARCH_SURVIVOR else REJECTED_CAVEAT,
    )


@dataclass(frozen=True, slots=True)
class Scorecard:
    entries: list[ScorecardEntry] = field(default_factory=list)
    multiple_testing_warning: str = ""


def build_scorecard(results: list[CandidateBlockedChronologicalResult]) -> Scorecard:
    entries = [score_candidate(r) for r in results]
    return Scorecard(entries=entries, multiple_testing_warning=multiple_testing_warning(len(results)))
