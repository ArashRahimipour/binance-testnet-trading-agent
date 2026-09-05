"""Proofs for research/scorecard.py (rule-based, deterministic robustness
scoring) and research/freeze.py (freezing a RESEARCH_SURVIVOR + rejecting
reuse of previously observed data as its "next" test).
"""

from decimal import Decimal
from pathlib import Path

import pytest

from trading_agent.data.models import Candle, interval_to_ms
from trading_agent.metrics.diagnostics import RunDiagnostics
from trading_agent.metrics.extended_report import (
    AccountingIdentity,
    BootstrapConfidenceInterval,
    ExtendedDiagnosticsReport,
    HoldingPeriodStats,
    PnlBreakdown,
    RollingWindowDiagnostics,
    TimeBasedPerformance,
    TradeDistribution,
    WindowExplanation,
)
from trading_agent.metrics.performance import PerformanceReport
from trading_agent.research.freeze import (
    FrozenCandidateForwardTestViolation,
    freeze_candidate,
    load_frozen_candidate,
    save_frozen_candidate,
    validate_future_paper_test,
)
from trading_agent.research.scorecard import (
    MATERIALLY_NEGATIVE_FOLD_RETURN_PCT,
    MAX_ACCEPTABLE_DRAWDOWN_PCT,
    MAX_BEST_TRADE_CONTRIBUTION_PCT,
    MIN_FOLDS_WITH_A_TRADE,
    MIN_POSITIVE_FOLD_FRACTION,
    MIN_TOTAL_TRADES_FOR_EVIDENCE,
    ScorecardStatus,
    build_scorecard,
    score_candidate,
)
from trading_agent.research.walk_forward import CandidateWalkForwardResult, FoldResult, FoldSpec

INTERVAL = "4h"
STEP = interval_to_ms(INTERVAL)


def _minimal_performance(trade_count: int, total_return_pct: float, max_drawdown_pct: float) -> PerformanceReport:
    return PerformanceReport(
        trade_count=trade_count, total_return_pct=total_return_pct, annualized_return_pct=None,
        max_drawdown_pct=max_drawdown_pct, volatility_pct=None, sharpe_ratio=None, sortino_ratio=None,
        win_rate=None, profit_factor=None, avg_win_quote=None, avg_loss_quote=None, exposure_pct=0.0,
        turnover=0.0, buy_and_hold_return_pct=None, low_trade_count_warning=False,
        starting_equity=Decimal(50), ending_equity=Decimal(50) * Decimal(str(1 + total_return_pct / 100)),
    )


def _minimal_diagnostics() -> RunDiagnostics:
    return RunDiagnostics(
        buy_signals_generated=0, exit_signals_generated=0, unexecuted_signals=0, executed_entries=0,
        executed_strategy_exits=0, executed_stop_loss_exits=0, rejected_entries_by_reason={},
        first_executed_trade_time_ms=None, last_executed_trade_time_ms=None, max_drawdown_pct=0.0,
        max_drawdown_time_ms=None, shutdown_activations={}, starting_equity=Decimal(50),
        ending_equity=Decimal(50), ending_cash_quote=Decimal(50), ending_base_quantity=Decimal(0),
        ends_with_open_position=False,
    )


def _minimal_extended(realized_pnl: Decimal, best_trade_contribution_pct: float | None) -> ExtendedDiagnosticsReport:
    trade_distribution = (
        TradeDistribution(
            trade_count=1, median_trade_return_pct=None, average_winner_quote=None, average_loser_quote=None,
            largest_winner_quote=None, largest_loser_quote=None, best_trade_pnl_quote=realized_pnl,
            best_trade_contribution_pct=best_trade_contribution_pct, best_trade_contribution_note="",
            total_pnl_excluding_best_trade_quote=Decimal(0), return_pct_excluding_best_trade=0.0,
            max_consecutive_wins=0, max_consecutive_losses=0,
            holding_period=HoldingPeriodStats(min_hours=None, median_hours=None, mean_hours=None, max_hours=None),
        )
        if best_trade_contribution_pct is not None
        else None
    )
    return ExtendedDiagnosticsReport(
        accounting=AccountingIdentity(
            ending_cash_quote=Decimal(50), ending_base_quantity=Decimal(0), final_mark_price=Decimal(1),
            computed_ending_equity=Decimal(50), reported_ending_equity=Decimal(50), difference_quote=Decimal(0),
            identity_holds=True,
        ),
        pnl_breakdown=PnlBreakdown(
            realized_closed_trade_pnl_quote=realized_pnl, unrealized_open_position_pnl_quote=None,
            total_marked_to_market_pnl_quote=realized_pnl, entry_fees_total_quote=Decimal(0),
            exit_fees_total_quote=Decimal(0), fees_are_estimated=True, fees_note="", slippage_cost_total_quote=Decimal(0),
            open_position_note=None,
        ),
        explanation=WindowExplanation(None, "", None),
        time_based=TimeBasedPerformance(
            cagr_pct=None, cagr_note="", monthly_returns=[], positive_months_pct=None,
            longest_underwater_days=None, longest_underwater_start_ms=None, longest_underwater_end_ms=None,
            exposure_pct=0.0, exposure_adjusted_return_pct=None, exposure_adjusted_return_note="",
            calmar_ratio=None, calmar_note="",
        ),
        trade_distribution=trade_distribution,
        bootstrap=BootstrapConfidenceInterval("", 0, 0, 0, 0.95, None, None, None),
        rolling_windows=RollingWindowDiagnostics(10, []),
    )


def _fold(trade_count: int, return_pct: float, max_dd: float, realized_pnl: Decimal, best_trade_pct: float | None = None) -> FoldResult:
    return FoldResult(
        fold=FoldSpec(0, 0, None, 0, None, None, 10, skipped_reason=None),
        performance=_minimal_performance(trade_count, return_pct, max_dd),
        diagnostics=_minimal_diagnostics(),
        extended=_minimal_extended(realized_pnl, best_trade_pct),
        open_position=None, ends_with_open_position=False, unresolved_pending_signal=None,
    )


def _result(candidate_id: str, folds: list[FoldResult]) -> CandidateWalkForwardResult:
    return CandidateWalkForwardResult(candidate_id=candidate_id, family="test_family", params={"x": 1}, folds=folds, warnings=[])


# --- INSUFFICIENT_EVIDENCE. ---


def test_scorecard_insufficient_evidence_when_too_few_trades():
    folds = [_fold(trade_count=1, return_pct=5.0, max_dd=1.0, realized_pnl=Decimal(1)) for _ in range(3)]
    entry = score_candidate(_result("c1", folds))
    assert entry.status == ScorecardStatus.INSUFFICIENT_EVIDENCE
    assert entry.total_trade_count == 3 < MIN_TOTAL_TRADES_FOR_EVIDENCE


def test_scorecard_insufficient_evidence_when_too_few_folds_traded():
    # Enough total trades, but concentrated in fewer folds than required.
    folds = [_fold(trade_count=MIN_TOTAL_TRADES_FOR_EVIDENCE, return_pct=5.0, max_dd=1.0, realized_pnl=Decimal(10))]
    folds += [_fold(trade_count=0, return_pct=0.0, max_dd=0.0, realized_pnl=Decimal(0)) for _ in range(4)]
    entry = score_candidate(_result("c2", folds))
    assert entry.folds_with_a_trade == 1 < MIN_FOLDS_WITH_A_TRADE
    assert entry.status == ScorecardStatus.INSUFFICIENT_EVIDENCE


# --- REJECTED - each criterion individually. ---


def _passing_folds(n: int = 5) -> list[FoldResult]:
    trades_per_fold = MIN_TOTAL_TRADES_FOR_EVIDENCE // n + 1
    return [
        _fold(trade_count=trades_per_fold, return_pct=5.0, max_dd=5.0, realized_pnl=Decimal(10), best_trade_pct=20.0)
        for _ in range(n)
    ]


def test_scorecard_rejects_non_positive_median_fold_return():
    folds = _passing_folds()
    folds[0] = _fold(trade_count=folds[0].performance.trade_count, return_pct=-5.0, max_dd=5.0, realized_pnl=Decimal(-1), best_trade_pct=20.0)
    folds[1] = _fold(trade_count=folds[1].performance.trade_count, return_pct=-5.0, max_dd=5.0, realized_pnl=Decimal(-1), best_trade_pct=20.0)
    folds[2] = _fold(trade_count=folds[2].performance.trade_count, return_pct=-5.0, max_dd=5.0, realized_pnl=Decimal(-1), best_trade_pct=20.0)
    entry = score_candidate(_result("c3", folds))
    assert entry.status == ScorecardStatus.REJECTED
    assert any("positive_median_fold_return" not in c.name or not c.passed for c in entry.criteria if c.name == "positive_median_fold_return")


def test_scorecard_rejects_non_positive_aggregate_realized_pnl():
    folds = _passing_folds()
    folds = [
        _fold(trade_count=f.performance.trade_count, return_pct=5.0, max_dd=5.0, realized_pnl=Decimal(-10), best_trade_pct=20.0)
        for f in folds
    ]
    entry = score_candidate(_result("c4", folds))
    assert entry.status == ScorecardStatus.REJECTED
    failed_names = {c.name for c in entry.criteria if not c.passed}
    assert "positive_aggregate_realized_pnl" in failed_names


def test_scorecard_rejects_a_materially_negative_fold_even_with_positive_aggregate():
    folds = _passing_folds()
    bad_return = MATERIALLY_NEGATIVE_FOLD_RETURN_PCT - 1.0
    folds[0] = _fold(trade_count=folds[0].performance.trade_count, return_pct=bad_return, max_dd=5.0, realized_pnl=Decimal(-1), best_trade_pct=20.0)
    # keep aggregate positive and other folds strongly positive
    for i in range(1, len(folds)):
        folds[i] = _fold(trade_count=folds[i].performance.trade_count, return_pct=50.0, max_dd=5.0, realized_pnl=Decimal(50), best_trade_pct=20.0)
    entry = score_candidate(_result("c5", folds))
    assert entry.aggregate_realized_pnl_quote > 0
    assert entry.status == ScorecardStatus.REJECTED
    failed_names = {c.name for c in entry.criteria if not c.passed}
    assert "no_materially_negative_fold" in failed_names


def test_scorecard_rejects_excessive_max_drawdown():
    folds = _passing_folds()
    folds[0] = _fold(
        trade_count=folds[0].performance.trade_count, return_pct=5.0,
        max_dd=MAX_ACCEPTABLE_DRAWDOWN_PCT + 1.0, realized_pnl=Decimal(10), best_trade_pct=20.0,
    )
    entry = score_candidate(_result("c6", folds))
    assert entry.status == ScorecardStatus.REJECTED
    failed_names = {c.name for c in entry.criteria if not c.passed}
    assert "acceptable_max_drawdown" in failed_names


def test_scorecard_rejects_excessive_best_trade_dependence():
    folds = _passing_folds()
    folds[0] = _fold(
        trade_count=folds[0].performance.trade_count, return_pct=5.0, max_dd=5.0,
        realized_pnl=Decimal(10), best_trade_pct=MAX_BEST_TRADE_CONTRIBUTION_PCT + 1.0,
    )
    entry = score_candidate(_result("c7", folds))
    assert entry.status == ScorecardStatus.REJECTED
    failed_names = {c.name for c in entry.criteria if not c.passed}
    assert "limited_best_trade_dependence" in failed_names


def test_scorecard_rejects_insufficient_stability_across_folds():
    n = 5
    trades_per_fold = MIN_TOTAL_TRADES_FOR_EVIDENCE // n + 1
    folds = []
    # Only 1/5 = 20% positive, well under MIN_POSITIVE_FOLD_FRACTION, but
    # the median and aggregate are still arranged positive to isolate
    # this one criterion.
    returns = [100.0, -1.0, -1.0, -1.0, -1.0]
    for r in returns:
        folds.append(_fold(trade_count=trades_per_fold, return_pct=r, max_dd=5.0, realized_pnl=Decimal(str(r)), best_trade_pct=20.0))
    entry = score_candidate(_result("c8", folds))
    assert entry.positive_fold_fraction is not None and entry.positive_fold_fraction < MIN_POSITIVE_FOLD_FRACTION
    assert entry.status == ScorecardStatus.REJECTED
    failed_names = {c.name for c in entry.criteria if not c.passed}
    assert "stable_across_folds" in failed_names


# --- RESEARCH_SURVIVOR. ---


def test_scorecard_research_survivor_when_every_criterion_passes():
    entry = score_candidate(_result("c9", _passing_folds()))
    assert entry.status == ScorecardStatus.RESEARCH_SURVIVOR
    assert all(c.passed for c in entry.criteria)
    assert entry.reasons == []
    assert "NOT a claim of profitability" in entry.caveat


def test_scorecard_never_uses_forbidden_language():
    for status_entry_folds in (_passing_folds(),):
        entry = score_candidate(_result("c10", status_entry_folds))
        assert "profitable" not in entry.caveat.lower()
        assert "approved for live" not in entry.caveat.lower()


# --- Multiple-testing warning. ---


def test_build_scorecard_includes_multiple_testing_warning_with_correct_count():
    results = [_result(f"c{i}", _passing_folds()) for i in range(4)]
    scorecard = build_scorecard(results)
    assert len(scorecard.entries) == 4
    assert "4 candidates" in scorecard.multiple_testing_warning
    assert "selection" in scorecard.multiple_testing_warning.lower() or "bias" in scorecard.multiple_testing_warning.lower()


def test_scorecard_is_deterministic():
    folds = _passing_folds()
    entry1 = score_candidate(_result("c11", folds))
    entry2 = score_candidate(_result("c11", folds))
    assert entry1 == entry2


# --- Freeze. ---


def test_freeze_candidate_requires_research_survivor_status():
    rejected_entry = score_candidate(_result("c12", [_fold(0, 0.0, 0.0, Decimal(0))]))
    assert rejected_entry.status == ScorecardStatus.INSUFFICIENT_EVIDENCE
    with pytest.raises(ValueError):
        freeze_candidate(rejected_entry, frozen_at_ms=1000, freeze_boundary_ms=2000)


def test_freeze_candidate_records_the_survivor_and_boundary():
    entry = score_candidate(_result("c13", _passing_folds()))
    record = freeze_candidate(entry, frozen_at_ms=1_000_000, freeze_boundary_ms=2_000_000)
    assert record.candidate_id == "c13"
    assert record.freeze_boundary_ms == 2_000_000
    assert record.scorecard_status == "RESEARCH_SURVIVOR"


def test_freeze_candidate_save_and_load_round_trip(tmp_path: Path):
    entry = score_candidate(_result("c14", _passing_folds()))
    record = freeze_candidate(entry, frozen_at_ms=1_000_000, freeze_boundary_ms=2_000_000)
    path = save_frozen_candidate(record, tmp_path / "frozen")
    loaded = load_frozen_candidate(path)
    assert loaded == record


def test_validate_future_paper_test_rejects_previously_observed_candles():
    entry = score_candidate(_result("c15", _passing_folds()))
    record = freeze_candidate(entry, frozen_at_ms=1_000_000, freeze_boundary_ms=2_000_000)
    old_candle = Candle(
        symbol="BTCUSDT", interval=INTERVAL, open_time_ms=1_999_999, close_time_ms=1_999_999 + STEP - 1,
        open=Decimal(100), high=Decimal(100), low=Decimal(100), close=Decimal(100), volume=Decimal(1),
    )
    with pytest.raises(FrozenCandidateForwardTestViolation):
        validate_future_paper_test(record, [old_candle])


def test_validate_future_paper_test_accepts_genuinely_new_candles():
    entry = score_candidate(_result("c16", _passing_folds()))
    record = freeze_candidate(entry, frozen_at_ms=1_000_000, freeze_boundary_ms=2_000_000)
    new_candle = Candle(
        symbol="BTCUSDT", interval=INTERVAL, open_time_ms=2_000_000, close_time_ms=2_000_000 + STEP - 1,
        open=Decimal(100), high=Decimal(100), low=Decimal(100), close=Decimal(100), volume=Decimal(1),
    )
    validate_future_paper_test(record, [new_candle])  # must not raise
