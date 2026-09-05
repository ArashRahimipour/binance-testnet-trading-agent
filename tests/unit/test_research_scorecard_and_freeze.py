"""Proofs for research/scorecard.py (rule-based, deterministic robustness
scoring on the blocked chronological evaluation - see
research/blocked_chronological_evaluation.py) and research/freeze.py
(freezing a RESEARCH_SURVIVOR + rejecting reuse of previously observed
data as its "next" test).

Scoring is on REALIZED closed-trade PnL only (normalized by each block's
own starting equity) - never marked-to-market total return - so these
fixtures drive every pass/fail outcome through `realized_pnl`, never
through `PerformanceReport.total_return_pct` alone.
"""

import json
from dataclasses import asdict, replace
from decimal import Decimal
from pathlib import Path

import pytest

from tests.fixtures.exchange_info import make_exchange_info
from trading_agent.config.models import AppConfig
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
from trading_agent.research.blocked_chronological_evaluation import (
    BlockResult,
    BlockSpec,
    CandidateBlockedChronologicalResult,
)
from trading_agent.research.candidate_registry import CANDIDATE_REGISTRY
from trading_agent.research.cutoff import RESEARCH_CUTOFF_MS
from trading_agent.research.fingerprint import (
    compute_candidate_registry_fingerprint,
    compute_config_fingerprint,
    compute_exchange_filters_fingerprint,
    compute_strategy_implementation_fingerprint,
)
from trading_agent.research.freeze import (
    FrozenCandidateForwardTestViolation,
    FrozenCandidateImplementationDriftError,
    FrozenCandidateRecord,
    FrozenCandidateVersionConflict,
    assert_frozen_candidate_matches_current_implementation,
    freeze_candidate,
    load_frozen_candidate,
    new_candidate_version_migration_id,
    save_frozen_candidate,
    validate_future_paper_test,
)
from trading_agent.research.scorecard import (
    MATERIALLY_NEGATIVE_BLOCK_REALIZED_RETURN_PCT,
    MAX_ACCEPTABLE_DRAWDOWN_PCT,
    MAX_BEST_TRADE_CONTRIBUTION_PCT,
    MIN_BLOCKS_WITH_A_TRADE,
    MIN_POSITIVE_REALIZED_PNL_BLOCK_FRACTION,
    MIN_TOTAL_TRADES_FOR_EVIDENCE,
    ScorecardStatus,
    build_scorecard,
    score_candidate,
)
from trading_agent.sizing.exchange_filters import SymbolFilters

INTERVAL = "4h"
STEP = interval_to_ms(INTERVAL)
STARTING_EQUITY = Decimal(50)

_TREND_A1 = next(spec for spec in CANDIDATE_REGISTRY if spec.candidate_id == "trend_regime_A1")
_TREND_A2 = next(spec for spec in CANDIDATE_REGISTRY if spec.candidate_id == "trend_regime_A2")


def _app_config() -> AppConfig:
    return AppConfig(mode="backtest")


def _symbol_filters() -> SymbolFilters:
    return SymbolFilters.from_exchange_info(make_exchange_info())


def _minimal_performance(trade_count: int, total_return_pct: float, max_drawdown_pct: float) -> PerformanceReport:
    return PerformanceReport(
        trade_count=trade_count, total_return_pct=total_return_pct, annualized_return_pct=None,
        max_drawdown_pct=max_drawdown_pct, volatility_pct=None, sharpe_ratio=None, sortino_ratio=None,
        win_rate=None, profit_factor=None, avg_win_quote=None, avg_loss_quote=None, exposure_pct=0.0,
        turnover=0.0, buy_and_hold_return_pct=None, low_trade_count_warning=False,
        starting_equity=STARTING_EQUITY, ending_equity=STARTING_EQUITY * Decimal(str(1 + total_return_pct / 100)),
    )


def _minimal_diagnostics() -> RunDiagnostics:
    return RunDiagnostics(
        buy_signals_generated=0, exit_signals_generated=0, unexecuted_signals=0, executed_entries=0,
        executed_strategy_exits=0, executed_stop_loss_exits=0, rejected_entries_by_reason={},
        first_executed_trade_time_ms=None, last_executed_trade_time_ms=None, max_drawdown_pct=0.0,
        max_drawdown_time_ms=None, shutdown_activations={}, starting_equity=STARTING_EQUITY,
        ending_equity=STARTING_EQUITY, ending_cash_quote=STARTING_EQUITY, ending_base_quantity=Decimal(0),
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
            ending_cash_quote=STARTING_EQUITY, ending_base_quantity=Decimal(0), final_mark_price=Decimal(1),
            computed_ending_equity=STARTING_EQUITY, reported_ending_equity=STARTING_EQUITY, difference_quote=Decimal(0),
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


def _block(trade_count: int, realized_pnl: Decimal, max_dd: float, best_trade_pct: float | None = None) -> BlockResult:
    """A block whose REALIZED pnl (as a pct of the fixed $50 starting
    equity) is what scoring actually reads - marked-to-market
    `total_return_pct` is set to the same figure here since no open
    position is modeled in these fixtures."""
    realized_return_pct = float(realized_pnl / STARTING_EQUITY * 100)
    return BlockResult(
        block=BlockSpec(0, 0, None, 0, None, None, 10, skipped_reason=None),
        performance=_minimal_performance(trade_count, realized_return_pct, max_dd),
        diagnostics=_minimal_diagnostics(),
        extended=_minimal_extended(realized_pnl, best_trade_pct),
        open_position=None, ends_with_open_position=False, unresolved_pending_signal=None,
    )


def _result(candidate_id: str, blocks: list[BlockResult]) -> CandidateBlockedChronologicalResult:
    return CandidateBlockedChronologicalResult(candidate_id=candidate_id, family="test_family", params={"x": 1}, blocks=blocks, warnings=[])


# --- INSUFFICIENT_EVIDENCE. ---


def test_scorecard_insufficient_evidence_when_too_few_trades():
    blocks = [_block(trade_count=1, realized_pnl=Decimal(1), max_dd=1.0) for _ in range(3)]
    entry = score_candidate(_result("c1", blocks))
    assert entry.status == ScorecardStatus.INSUFFICIENT_EVIDENCE
    assert entry.total_trade_count == 3 < MIN_TOTAL_TRADES_FOR_EVIDENCE


def test_scorecard_insufficient_evidence_when_too_few_blocks_traded():
    # Enough total trades, but concentrated in fewer blocks than required.
    blocks = [_block(trade_count=MIN_TOTAL_TRADES_FOR_EVIDENCE, realized_pnl=Decimal(10), max_dd=1.0)]
    blocks += [_block(trade_count=0, realized_pnl=Decimal(0), max_dd=0.0) for _ in range(4)]
    entry = score_candidate(_result("c2", blocks))
    assert entry.blocks_with_a_trade == 1 < MIN_BLOCKS_WITH_A_TRADE
    assert entry.status == ScorecardStatus.INSUFFICIENT_EVIDENCE


def test_scorecard_zero_aggregate_pnl_cannot_pass():
    n = 5
    trades_per_block = MIN_TOTAL_TRADES_FOR_EVIDENCE // n + 1
    blocks = [_block(trade_count=trades_per_block, realized_pnl=Decimal(0), max_dd=1.0, best_trade_pct=0.0) for _ in range(n)]
    entry = score_candidate(_result("c_zero", blocks))
    assert entry.aggregate_realized_pnl_quote == 0
    assert entry.status != ScorecardStatus.RESEARCH_SURVIVOR


# --- REJECTED - each criterion individually. ---


def _passing_blocks(n: int = 5) -> list[BlockResult]:
    trades_per_block = MIN_TOTAL_TRADES_FOR_EVIDENCE // n + 1
    return [
        _block(trade_count=trades_per_block, realized_pnl=Decimal(10), max_dd=5.0, best_trade_pct=20.0)
        for _ in range(n)
    ]


def test_scorecard_rejects_non_positive_median_block_realized_return():
    blocks = _passing_blocks()
    blocks[0] = _block(trade_count=blocks[0].performance.trade_count, realized_pnl=Decimal("-2.5"), max_dd=5.0, best_trade_pct=20.0)
    blocks[1] = _block(trade_count=blocks[1].performance.trade_count, realized_pnl=Decimal("-2.5"), max_dd=5.0, best_trade_pct=20.0)
    blocks[2] = _block(trade_count=blocks[2].performance.trade_count, realized_pnl=Decimal("-2.5"), max_dd=5.0, best_trade_pct=20.0)
    entry = score_candidate(_result("c3", blocks))
    assert entry.status == ScorecardStatus.REJECTED
    assert entry.aggregate_realized_pnl_quote > 0  # isolates the median criterion, not the aggregate one
    failed_names = {c.name for c in entry.criteria if not c.passed}
    assert "positive_median_block_realized_return" in failed_names


def test_scorecard_rejects_non_positive_aggregate_realized_pnl():
    blocks = _passing_blocks()
    blocks = [
        _block(trade_count=b.performance.trade_count, realized_pnl=Decimal(-10), max_dd=5.0, best_trade_pct=20.0)
        for b in blocks
    ]
    entry = score_candidate(_result("c4", blocks))
    assert entry.status == ScorecardStatus.REJECTED
    failed_names = {c.name for c in entry.criteria if not c.passed}
    assert "positive_aggregate_realized_pnl" in failed_names


def test_scorecard_rejects_a_materially_negative_block_even_with_positive_aggregate():
    blocks = _passing_blocks()
    bad_return_pct = MATERIALLY_NEGATIVE_BLOCK_REALIZED_RETURN_PCT - 1.0
    bad_realized_pnl = Decimal(str(bad_return_pct / 100)) * STARTING_EQUITY
    blocks[0] = _block(trade_count=blocks[0].performance.trade_count, realized_pnl=bad_realized_pnl, max_dd=5.0, best_trade_pct=20.0)
    # keep aggregate positive and other blocks strongly positive
    for i in range(1, len(blocks)):
        blocks[i] = _block(trade_count=blocks[i].performance.trade_count, realized_pnl=Decimal(25), max_dd=5.0, best_trade_pct=20.0)
    entry = score_candidate(_result("c5", blocks))
    assert entry.aggregate_realized_pnl_quote > 0
    assert entry.status == ScorecardStatus.REJECTED
    failed_names = {c.name for c in entry.criteria if not c.passed}
    assert "no_materially_negative_block" in failed_names


def test_scorecard_rejects_excessive_max_drawdown():
    blocks = _passing_blocks()
    blocks[0] = _block(
        trade_count=blocks[0].performance.trade_count, realized_pnl=Decimal(10),
        max_dd=MAX_ACCEPTABLE_DRAWDOWN_PCT + 1.0, best_trade_pct=20.0,
    )
    entry = score_candidate(_result("c6", blocks))
    assert entry.status == ScorecardStatus.REJECTED
    failed_names = {c.name for c in entry.criteria if not c.passed}
    assert "acceptable_max_drawdown" in failed_names


def test_scorecard_rejects_excessive_best_trade_dependence():
    blocks = _passing_blocks()
    blocks[0] = _block(
        trade_count=blocks[0].performance.trade_count, realized_pnl=Decimal(10), max_dd=5.0,
        best_trade_pct=MAX_BEST_TRADE_CONTRIBUTION_PCT + 1.0,
    )
    entry = score_candidate(_result("c7", blocks))
    assert entry.status == ScorecardStatus.REJECTED
    failed_names = {c.name for c in entry.criteria if not c.passed}
    assert "limited_best_trade_dependence" in failed_names


def test_scorecard_undefined_best_trade_contribution_never_auto_passes():
    # Every block has a positive realized PnL but no defined
    # best_trade_contribution_pct at all - undefined must FAIL, never
    # silently pass.
    n = 5
    trades_per_block = MIN_TOTAL_TRADES_FOR_EVIDENCE // n + 1
    blocks = [_block(trade_count=trades_per_block, realized_pnl=Decimal(10), max_dd=5.0, best_trade_pct=None) for _ in range(n)]
    entry = score_candidate(_result("c_undefined", blocks))
    assert entry.max_best_trade_contribution_pct is None
    assert entry.status == ScorecardStatus.REJECTED
    failed_names = {c.name for c in entry.criteria if not c.passed}
    assert "limited_best_trade_dependence" in failed_names


def test_scorecard_rejects_insufficient_stability_across_blocks():
    n = 5
    trades_per_block = MIN_TOTAL_TRADES_FOR_EVIDENCE // n + 1
    blocks = []
    # Only 1/5 = 20% positive, well under MIN_POSITIVE_REALIZED_PNL_BLOCK_FRACTION,
    # but the median and aggregate are still arranged positive to isolate
    # this one criterion.
    pnls = [100.0, -1.0, -1.0, -1.0, -1.0]
    for pnl in pnls:
        blocks.append(_block(trade_count=trades_per_block, realized_pnl=Decimal(str(pnl)), max_dd=5.0, best_trade_pct=20.0))
    entry = score_candidate(_result("c8", blocks))
    assert (
        entry.positive_realized_pnl_block_fraction is not None
        and entry.positive_realized_pnl_block_fraction < MIN_POSITIVE_REALIZED_PNL_BLOCK_FRACTION
    )
    assert entry.status == ScorecardStatus.REJECTED
    failed_names = {c.name for c in entry.criteria if not c.passed}
    assert "stable_across_blocks" in failed_names


# --- Realized vs. marked-to-market: an open position cannot create a survivor. ---


def test_scorecard_open_position_unrealized_gain_cannot_create_a_survivor():
    # Every block has ZERO realized PnL but a large positive marked-to-market
    # total_return_pct (as if a big paper gain sits in a still-open
    # position) - survivor status must still be denied, since scoring
    # never reads total_return_pct.
    n = 5
    trades_per_block = MIN_TOTAL_TRADES_FOR_EVIDENCE // n + 1
    blocks = []
    for _ in range(n):
        block = _block(trade_count=trades_per_block, realized_pnl=Decimal(0), max_dd=5.0, best_trade_pct=None)
        performance = _minimal_performance(trades_per_block, total_return_pct=500.0, max_drawdown_pct=5.0)
        blocks.append(
            BlockResult(
                block=block.block, performance=performance, diagnostics=block.diagnostics, extended=block.extended,
                open_position=None, ends_with_open_position=True, unresolved_pending_signal=None,
            )
        )
    entry = score_candidate(_result("c_unrealized", blocks))
    assert entry.aggregate_realized_pnl_quote == 0
    assert entry.status != ScorecardStatus.RESEARCH_SURVIVOR


# --- RESEARCH_SURVIVOR. ---


def test_scorecard_research_survivor_when_every_criterion_passes():
    entry = score_candidate(_result("c9", _passing_blocks()))
    assert entry.status == ScorecardStatus.RESEARCH_SURVIVOR
    assert all(c.passed for c in entry.criteria)
    assert entry.reasons == []
    assert "NOT a claim of profitability" in entry.caveat


def test_scorecard_never_uses_forbidden_language():
    for status_entry_blocks in (_passing_blocks(),):
        entry = score_candidate(_result("c10", status_entry_blocks))
        assert "profitable" not in entry.caveat.lower()
        assert "approved for live" not in entry.caveat.lower()


def test_scorecard_reports_marked_to_market_and_excess_return_for_visibility_only():
    entry = score_candidate(_result("c_bench", _passing_blocks()))
    assert entry.median_block_marked_to_market_return_pct is not None
    assert "excess_return_pct" in entry.benchmark_note
    assert "NEVER a pass/fail criterion" in entry.benchmark_note


# --- Multiple-testing warning. ---


def test_build_scorecard_includes_multiple_testing_warning_with_correct_count():
    results = [_result(f"c{i}", _passing_blocks()) for i in range(4)]
    scorecard = build_scorecard(results)
    assert len(scorecard.entries) == 4
    assert "4 candidates" in scorecard.multiple_testing_warning
    assert "selection" in scorecard.multiple_testing_warning.lower() or "bias" in scorecard.multiple_testing_warning.lower()


def test_scorecard_is_deterministic():
    blocks = _passing_blocks()
    entry1 = score_candidate(_result("c11", blocks))
    entry2 = score_candidate(_result("c11", blocks))
    assert entry1 == entry2


# --- Freeze. ---


def test_freeze_candidate_requires_research_survivor_status():
    rejected_entry = score_candidate(_result(_TREND_A1.candidate_id, [_block(0, Decimal(0), 0.0)]))
    assert rejected_entry.status == ScorecardStatus.INSUFFICIENT_EVIDENCE
    with pytest.raises(ValueError):
        freeze_candidate(
            rejected_entry, frozen_at_ms=1000, freeze_boundary_ms=2000,
            candidate=_TREND_A1, config=_app_config(), filters=_symbol_filters(),
        )


def test_freeze_candidate_rejects_a_mismatched_candidate_spec():
    entry = score_candidate(_result(_TREND_A1.candidate_id, _passing_blocks()))
    with pytest.raises(ValueError):
        freeze_candidate(
            entry, frozen_at_ms=1_000_000, freeze_boundary_ms=2_000_000,
            candidate=_TREND_A2, config=_app_config(), filters=_symbol_filters(),
        )


def test_freeze_candidate_records_the_survivor_and_boundary():
    entry = score_candidate(_result(_TREND_A1.candidate_id, _passing_blocks()))
    record = freeze_candidate(
        entry, frozen_at_ms=1_000_000, freeze_boundary_ms=2_000_000,
        candidate=_TREND_A1, config=_app_config(), filters=_symbol_filters(),
    )
    assert record.candidate_id == _TREND_A1.candidate_id
    assert record.freeze_boundary_ms == 2_000_000
    assert record.scorecard_status == "RESEARCH_SURVIVOR"
    assert record.research_cutoff_ms == RESEARCH_CUTOFF_MS
    assert record.candidate_version == 1


def test_freeze_candidate_save_and_load_round_trip(tmp_path: Path):
    entry = score_candidate(_result(_TREND_A1.candidate_id, _passing_blocks()))
    record = freeze_candidate(
        entry, frozen_at_ms=1_000_000, freeze_boundary_ms=2_000_000,
        candidate=_TREND_A1, config=_app_config(), filters=_symbol_filters(),
    )
    path = save_frozen_candidate(record, tmp_path / "frozen")
    loaded = load_frozen_candidate(path)
    assert loaded == record


def test_validate_future_paper_test_rejects_previously_observed_candles():
    entry = score_candidate(_result(_TREND_A1.candidate_id, _passing_blocks()))
    record = freeze_candidate(
        entry, frozen_at_ms=1_000_000, freeze_boundary_ms=2_000_000,
        candidate=_TREND_A1, config=_app_config(), filters=_symbol_filters(),
    )
    old_candle = Candle(
        symbol="BTCUSDT", interval=INTERVAL, open_time_ms=1_999_999, close_time_ms=1_999_999 + STEP - 1,
        open=Decimal(100), high=Decimal(100), low=Decimal(100), close=Decimal(100), volume=Decimal(1),
    )
    with pytest.raises(FrozenCandidateForwardTestViolation):
        validate_future_paper_test(record, [old_candle])


def test_validate_future_paper_test_accepts_genuinely_new_candles():
    entry = score_candidate(_result(_TREND_A1.candidate_id, _passing_blocks()))
    record = freeze_candidate(
        entry, frozen_at_ms=1_000_000, freeze_boundary_ms=2_000_000,
        candidate=_TREND_A1, config=_app_config(), filters=_symbol_filters(),
    )
    new_candle = Candle(
        symbol="BTCUSDT", interval=INTERVAL, open_time_ms=2_000_000, close_time_ms=2_000_000 + STEP - 1,
        open=Decimal(100), high=Decimal(100), low=Decimal(100), close=Decimal(100), volume=Decimal(1),
    )
    validate_future_paper_test(record, [new_candle])  # must not raise


# --- Reproducibility fingerprints (pre-real-evaluation code review correction). ---


def _frozen_a1(config: AppConfig | None = None, filters: SymbolFilters | None = None) -> FrozenCandidateRecord:
    entry = score_candidate(_result(_TREND_A1.candidate_id, _passing_blocks()))
    return freeze_candidate(
        entry, frozen_at_ms=1_000_000, freeze_boundary_ms=2_000_000,
        candidate=_TREND_A1, config=config or _app_config(), filters=filters or _symbol_filters(),
    )


def test_frozen_record_carries_every_required_fingerprint_and_snapshot():
    record = _frozen_a1()
    assert record.strategy_implementation_fingerprint == compute_strategy_implementation_fingerprint(_TREND_A1)
    assert record.candidate_registry_fingerprint == compute_candidate_registry_fingerprint(_TREND_A1)
    assert record.config_fingerprint == compute_config_fingerprint(_app_config())
    assert record.exchange_filters_fingerprint == compute_exchange_filters_fingerprint(_symbol_filters())
    assert record.scorecard_result_fingerprint
    assert record.config_snapshot["interval"] == _app_config().market.interval
    assert record.symbol == "BTCUSDT"
    assert record.exchange_filters_snapshot["symbol"] == "BTCUSDT"
    # source_commit_hash is best-effort - either a non-empty string or None, never raises.
    assert record.source_commit_hash is None or isinstance(record.source_commit_hash, str)


def test_frozen_record_contains_no_secrets_or_credentials():
    record = _frozen_a1()
    serialized = _stable_str(record).lower()
    for forbidden in ("api_key", "api_secret", "testnet_api_key", "testnet_api_secret"):
        assert forbidden not in serialized
    # AppConfig structurally has no field for credentials - config_snapshot can only ever
    # contain the declared, non-secret sections it was built from.
    assert set(record.config_snapshot.keys()) == {"interval", "starting_equity", "gap_policy", "fees", "sizing", "stop_loss", "risk"}


def _stable_str(record: FrozenCandidateRecord) -> str:
    return json.dumps(asdict(record), default=str)


def test_assert_frozen_candidate_matches_current_implementation_passes_when_nothing_changed():
    record = _frozen_a1()
    assert_frozen_candidate_matches_current_implementation(record, _TREND_A1, _app_config(), _symbol_filters())


def test_assert_frozen_candidate_matches_current_implementation_fails_closed_on_config_drift():
    record = _frozen_a1()
    drifted_config = AppConfig(mode="backtest", risk={"max_drawdown_pct": 0.5})
    with pytest.raises(FrozenCandidateImplementationDriftError):
        assert_frozen_candidate_matches_current_implementation(record, _TREND_A1, drifted_config, _symbol_filters())


def test_assert_frozen_candidate_matches_current_implementation_fails_closed_on_filter_drift():
    record = _frozen_a1()
    drifted_filters = _symbol_filters_with(min_notional="999")
    with pytest.raises(FrozenCandidateImplementationDriftError):
        assert_frozen_candidate_matches_current_implementation(record, _TREND_A1, _app_config(), drifted_filters)


def _symbol_filters_with(**overrides) -> SymbolFilters:
    return SymbolFilters.from_exchange_info(make_exchange_info(**overrides))


def test_assert_frozen_candidate_matches_current_implementation_fails_closed_on_registry_drift():
    record = _frozen_a1()
    # A different candidate (different params) masquerading under a hand-edited id - simulates
    # someone having changed A1's declared params in the registry after it was frozen.
    drifted_candidate = replace(_TREND_A1, params={**_TREND_A1.params, "min_trend_strength_atr": 9.0})
    with pytest.raises(FrozenCandidateImplementationDriftError):
        assert_frozen_candidate_matches_current_implementation(record, drifted_candidate, _app_config(), _symbol_filters())


def test_assert_frozen_candidate_matches_current_implementation_fails_closed_on_strategy_source_drift():
    # Simulates the candidate's own source file (or a shared indicator module it depends on)
    # having changed since freezing, WITHOUT touching the registry entry, config, or filters -
    # isolates the strategy-implementation-fingerprint check specifically.
    record = _frozen_a1()
    drifted_record = replace(record, strategy_implementation_fingerprint="0" * 64)
    with pytest.raises(FrozenCandidateImplementationDriftError) as excinfo:
        assert_frozen_candidate_matches_current_implementation(drifted_record, _TREND_A1, _app_config(), _symbol_filters())
    assert "strategy implementation" in str(excinfo.value)


def test_assert_frozen_candidate_matches_current_implementation_rejects_wrong_candidate():
    record = _frozen_a1()
    with pytest.raises(ValueError):
        assert_frozen_candidate_matches_current_implementation(record, _TREND_A2, _app_config(), _symbol_filters())


def test_save_frozen_candidate_allows_re_saving_an_identical_record(tmp_path: Path):
    record = _frozen_a1()
    directory = tmp_path / "frozen"
    save_frozen_candidate(record, directory)
    path = save_frozen_candidate(record, directory)  # identical fingerprints - must not raise
    assert load_frozen_candidate(path) == record


def test_save_frozen_candidate_refuses_to_silently_overwrite_a_differing_record(tmp_path: Path):
    directory = tmp_path / "frozen"
    original = _frozen_a1()
    save_frozen_candidate(original, directory)

    drifted_config = AppConfig(mode="backtest", fees={"taker_fee_pct": 0.01})
    drifted = _frozen_a1(config=drifted_config)
    assert drifted.config_fingerprint != original.config_fingerprint
    with pytest.raises(FrozenCandidateVersionConflict):
        save_frozen_candidate(drifted, directory)


def test_new_candidate_version_migration_id_is_the_sanctioned_response_to_drift():
    migrated_id = new_candidate_version_migration_id(_TREND_A1.candidate_id, new_version=2)
    assert migrated_id == f"{_TREND_A1.candidate_id}__v2"
    with pytest.raises(ValueError):
        new_candidate_version_migration_id(_TREND_A1.candidate_id, new_version=1)
