"""Proofs for research/post_mortem.py: the read-only candidate post-mortem
report built purely from already-computed `CandidateBlockedChronologicalResult`
data (no new simulation, no database access). Covers every aggregation
requirement: counts, PnL stats, exit-reason breakdown (including
gap-through-stop), realized R-multiple distribution, planned-vs-realized
R/R, cost totals, best-trade exclusion analysis, PnL concentration,
chronological stability (per-block, per-year, streaks, underwater, half
split), the risk/reward policy diagnostics rollup, the breakout-specific
exclusion check, the fixed evidence-only diagnosis rule, zero-trade
candidates, negative aggregate PnL, an unresolved open position at a
block's end, gap losses, and deterministic (repeat-call-identical) output.

These tests build `BlockResult`/`CandidateBlockedChronologicalResult`
objects directly (never via a real backtest run) so every edge case is
exact and explicit - `test_research_blocked_chronological_evaluation.py`
and `test_backtest_engine_risk_reward.py` already prove the underlying
engine wiring (that `BlockResult.trades` and `RiskRewardDiagnostics`'s
per-entry quote lists are populated correctly from a real run); one
end-to-end integration test at the bottom of this file additionally proves
the whole pipeline (real engine -> real evaluation -> this report) composes
without error and is deterministic.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from trading_agent.backtest.risk_reward import RiskRewardDiagnostics
from trading_agent.data.models import interval_to_ms
from trading_agent.metrics.performance import (
    EXIT_REASON_STOP_LOSS,
    EXIT_REASON_STRATEGY,
    EXIT_REASON_TAKE_PROFIT,
    PerformanceReport,
    Trade,
)
from trading_agent.research.blocked_chronological_evaluation import (
    BlockResult,
    BlockSpec,
    CandidateBlockedChronologicalResult,
    run_candidate_blocked_chronological_evaluation,
)
from trading_agent.research.candidate_registry import CANDIDATE_REGISTRY
from trading_agent.research.candidates.volatility_breakout import FAMILY as BREAKOUT_FAMILY
from trading_agent.research.post_mortem import (
    EQUITY_ACCOUNTING_NOTE,
    EvidenceDiagnosis,
    build_candidate_post_mortem,
    build_post_mortem_report,
)

INTERVAL = "4h"
STEP = interval_to_ms(INTERVAL)
START = 1_700_000_000_000
STARTING_EQUITY = Decimal(50)


def _trade(
    i: int,
    pnl: str,
    exit_reason: str = EXIT_REASON_STRATEGY,
    entry_price: str = "100",
    exit_price: str = "100",
    qty: str = "1",
    fees: str = "0",
) -> Trade:
    entry_ms = START + i * STEP
    exit_ms = entry_ms + STEP
    return Trade(
        entry_time_ms=entry_ms,
        exit_time_ms=exit_ms,
        entry_price=Decimal(entry_price),
        exit_price=Decimal(exit_price),
        quantity=Decimal(qty),
        fees_paid=Decimal(fees),
        pnl_quote=Decimal(pnl),
        exit_reason=exit_reason,
        entry_reference_price=Decimal(entry_price),
        exit_reference_price=Decimal(exit_price),
    )


def _perf(starting_equity: Decimal = STARTING_EQUITY, trade_count: int = 0) -> PerformanceReport:
    return PerformanceReport(
        trade_count=trade_count, total_return_pct=0.0, annualized_return_pct=None, max_drawdown_pct=0.0,
        volatility_pct=None, sharpe_ratio=None, sortino_ratio=None, win_rate=None, profit_factor=None,
        avg_win_quote=None, avg_loss_quote=None, exposure_pct=0.0, turnover=0.0, buy_and_hold_return_pct=None,
        low_trade_count_warning=True, starting_equity=starting_equity, ending_equity=starting_equity,
    )


def _rr_diag(
    trades: list[Trade],
    planned_risk_values: list[Decimal] | None = None,
    planned_reward_values: list[Decimal] | None = None,
    net_rr_values: list[float] | None = None,
    gross_rr_values: list[float] | None = None,
    entries_approved: int | None = None,
    rejected_net_rr: int = 0,
    rejected_exchange_filter: int = 0,
    rejected_post_fill: int = 0,
) -> RiskRewardDiagnostics:
    n = len(trades)
    planned_risk_values = planned_risk_values if planned_risk_values is not None else [Decimal("0.5")] * n
    planned_reward_values = planned_reward_values if planned_reward_values is not None else [Decimal("1.0")] * n
    net_rr_values = net_rr_values if net_rr_values is not None else [2.0] * n
    gross_rr_values = gross_rr_values if gross_rr_values is not None else [2.0] * n
    stop_exits = sum(1 for t in trades if t.exit_reason == EXIT_REASON_STOP_LOSS)
    take_profit_exits = sum(1 for t in trades if t.exit_reason == EXIT_REASON_TAKE_PROFIT)
    gap_count = sum(
        1 for t, r in zip(trades, planned_risk_values, strict=False) if t.exit_reason == EXIT_REASON_STOP_LOSS and -t.pnl_quote > r
    )
    return RiskRewardDiagnostics(
        entries_approved=entries_approved if entries_approved is not None else n,
        entries_rejected_net_rr_below_minimum=rejected_net_rr,
        entries_rejected_exchange_filter_within_risk_budget=rejected_exchange_filter,
        entries_rejected_post_fill_revalidation=rejected_post_fill,
        stop_loss_exits=stop_exits,
        take_profit_exits=take_profit_exits,
        gap_losses_exceeding_planned_risk=gap_count,
        planned_risk_quote_total=sum(planned_risk_values, Decimal(0)),
        planned_reward_quote_total=sum(planned_reward_values, Decimal(0)),
        planned_risk_pct_values=tuple(float(v / STARTING_EQUITY) for v in planned_risk_values),
        planned_reward_pct_values=tuple(float(v / STARTING_EQUITY) for v in planned_reward_values),
        gross_reward_to_risk_values=tuple(gross_rr_values),
        net_reward_to_risk_values=tuple(net_rr_values),
        planned_risk_quote_values=tuple(planned_risk_values),
        planned_reward_quote_values=tuple(planned_reward_values),
    )


def _block(
    seg: int,
    blk: int,
    trades: list[Trade],
    risk_reward: RiskRewardDiagnostics | None = None,
    starting_equity: Decimal = STARTING_EQUITY,
    skipped_reason: str | None = None,
) -> BlockResult:
    spec = BlockSpec(
        segment_index=seg, block_index=blk,
        warm_up_start_time_ms=None, warm_up_candle_count=0,
        window_start_time_ms=trades[0].entry_time_ms if trades else None,
        window_end_time_ms=trades[-1].exit_time_ms if trades else None,
        candle_count=len(trades) + 1, skipped_reason=skipped_reason,
    )
    if skipped_reason is not None:
        return BlockResult(
            block=spec, performance=None, diagnostics=None, extended=None, open_position=None,
            ends_with_open_position=False, unresolved_pending_signal=None,
        )
    return BlockResult(
        block=spec, performance=_perf(starting_equity, len(trades)), diagnostics=None, extended=None,
        open_position=None, ends_with_open_position=False, unresolved_pending_signal=None,
        risk_reward=risk_reward if risk_reward is not None else _rr_diag(trades),
        trades=tuple(trades),
    )


def _result(candidate_id: str, family: str, blocks: list[BlockResult]) -> CandidateBlockedChronologicalResult:
    return CandidateBlockedChronologicalResult(candidate_id=candidate_id, family=family, params={}, blocks=blocks)


# --- Trade counts and PnL stats. ---


def test_trade_counts_and_pnl_stats_basic():
    trades = [_trade(0, "1.0"), _trade(1, "2.0"), _trade(2, "-1.0"), _trade(3, "0")]
    result = _result("c1", "trend_regime", [_block(0, 0, trades)])
    pm = build_candidate_post_mortem(result)

    assert pm.trade_counts.closed_trades == 4
    assert pm.trade_counts.wins == 2
    assert pm.trade_counts.losses == 1
    assert pm.trade_counts.breakeven == 1
    assert pm.trade_counts.win_rate_pct == 50.0

    assert pm.pnl_stats.average_net_pnl_quote == 0.5
    assert pm.pnl_stats.median_net_pnl_quote == 0.5
    assert pm.pnl_stats.average_winner_quote == 1.5
    assert pm.pnl_stats.average_loser_quote == -1.0
    assert pm.pnl_stats.realized_payoff_ratio == 1.5
    assert pm.pnl_stats.profit_factor == 3.0  # gross_profit=3.0, gross_loss=1.0
    assert pm.pnl_stats.expected_value_pct_of_starting_equity == 1.0  # 0.5/50*100


# --- Zero-trade candidate: insufficient evidence, no crash, all stats None. ---


def test_zero_trade_candidate_is_insufficient_evidence_with_no_crash():
    result = _result("c_empty", "trend_regime", [_block(0, 0, [], risk_reward=_rr_diag([]))])
    pm = build_candidate_post_mortem(result)

    assert pm.trade_counts.closed_trades == 0
    assert pm.trade_counts.win_rate_pct is None
    assert pm.pnl_stats.average_net_pnl_quote is None
    assert pm.pnl_stats.profit_factor is None
    assert pm.realized_r_distribution.trades_with_known_r == 0
    assert pm.diagnosis == EvidenceDiagnosis.INSUFFICIENT_EVIDENCE
    assert "total_closed_trades=0" in pm.diagnosis_reason


def test_build_post_mortem_report_handles_a_candidate_with_zero_evaluated_blocks():
    # Every block skipped (e.g. segment too small for warm-up) - still must
    # produce a well-formed, non-crashing report.
    result = _result("c_all_skipped", "trend_regime", [_block(0, 0, [], skipped_reason="segment too small")])
    pm = build_candidate_post_mortem(result)
    assert pm.trade_counts.closed_trades == 0
    assert pm.diagnosis == EvidenceDiagnosis.INSUFFICIENT_EVIDENCE
    assert pm.chronological_stability.per_block == []


# --- Negative aggregate PnL -> negative expectancy (with sufficient evidence). ---


def _many_trades(n: int, pnl_each: str) -> list[Trade]:
    return [_trade(i, pnl_each) for i in range(n)]


def test_negative_aggregate_pnl_with_sufficient_evidence_diagnoses_negative_expectancy():
    trades = _many_trades(40, "-0.1")
    blocks = [_block(0, b, trades[b * 10 : (b + 1) * 10]) for b in range(4)]
    result = _result("c_neg", "trend_regime", blocks)
    pm = build_candidate_post_mortem(result)

    assert pm.trade_counts.closed_trades == 40
    total_pnl = sum((t.pnl_quote for t in trades), Decimal(0))
    assert total_pnl < 0
    assert pm.diagnosis == EvidenceDiagnosis.NEGATIVE_EXPECTANCY


# --- Gap-through-stop detection and risk-policy compliance. ---


def test_gap_through_stop_is_detected_and_reported_separately_from_ordinary_stop_loss():
    ordinary_stop = _trade(0, "-0.5", exit_reason=EXIT_REASON_STOP_LOSS)  # loss == planned risk exactly -> not a gap
    gap_stop = _trade(1, "-2.0", exit_reason=EXIT_REASON_STOP_LOSS)  # loss far exceeds planned risk -> a gap
    winner = _trade(2, "1.0", exit_reason=EXIT_REASON_TAKE_PROFIT)
    trades = [ordinary_stop, gap_stop, winner]
    rr = _rr_diag(trades, planned_risk_values=[Decimal("0.5"), Decimal("0.5"), Decimal("0.5")])
    result = _result("c_gap", "trend_regime", [_block(0, 0, trades, risk_reward=rr)])
    pm = build_candidate_post_mortem(result)

    gap_entry = next(e for e in pm.exit_reason_breakdown if e.exit_reason == "GAP_THROUGH_STOP")
    assert gap_entry.trade_count == 1
    assert gap_entry.expectancy_quote == -2.0

    stop_entry = next(e for e in pm.exit_reason_breakdown if e.exit_reason == EXIT_REASON_STOP_LOSS)
    assert stop_entry.trade_count == 2  # both stop-loss trades, gap included

    rrd = pm.risk_reward_diagnostics
    assert rrd.stop_loss_trades_total == 2
    assert rrd.stop_loss_trades_within_planned_risk == 1
    assert rrd.stop_loss_trades_gap_exceeding_planned_risk == 1
    assert rrd.planned_1pct_risk_compliance_pct == 50.0


# --- Realized R-multiple distribution. ---


def test_realized_r_distribution_computed_from_planned_risk_quote():
    trades = [_trade(0, "1.0"), _trade(1, "-0.5"), _trade(2, "1.5")]
    rr = _rr_diag(trades, planned_risk_values=[Decimal("0.5"), Decimal("0.5"), Decimal("0.5")])
    result = _result("c_r", "trend_regime", [_block(0, 0, trades, risk_reward=rr)])
    pm = build_candidate_post_mortem(result)

    r = pm.realized_r_distribution
    assert r.trades_with_known_r == 3
    assert r.trades_excluded_unknown_r == 0
    assert r.min_r == -1.0
    assert r.max_r == 3.0
    assert r.median_r == 2.0
    assert r.mean_r == (2.0 - 1.0 + 3.0) / 3
    assert r.pct_at_least_plus_2r == pytest.approx(2 / 3 * 100)
    assert r.pct_losing_more_than_minus_1r == 0.0


# --- Best-trade / best-3 / best-5% exclusion analysis. ---


def test_exclusion_analysis_best_trade_best_3_best_5_pct():
    # 20 trades: one huge winner (10.0), the rest small losers (-0.1 each x 19 = -1.9).
    # Aggregate = 10.0 - 1.9 = 8.1 (positive), but excluding the best trade
    # flips it negative - a textbook "concentrated/fragile" fingerprint.
    trades = [_trade(0, "10.0")] + [_trade(i, "-0.1") for i in range(1, 20)]
    result = _result("c_excl", "trend_regime", [_block(0, 0, trades)])
    pm = build_candidate_post_mortem(result)

    by_label = {e.label: e for e in pm.exclusions}
    assert by_label["excluding_best_trade"].trades_excluded == 1
    assert by_label["excluding_best_trade"].net_pnl_quote == Decimal("-1.9")
    assert by_label["excluding_best_trade"].remains_positive is False

    assert by_label["excluding_best_3_trades"].trades_excluded == 3
    # best_5_pct of 20 trades = ceil(1.0) = 1
    assert by_label["excluding_best_5_pct_of_trades"].trades_excluded == 1


def test_exclusion_analysis_never_crashes_with_fewer_than_three_trades():
    trades = [_trade(0, "1.0"), _trade(1, "-0.5")]
    result = _result("c_small", "trend_regime", [_block(0, 0, trades)])
    pm = build_candidate_post_mortem(result)
    by_label = {e.label: e for e in pm.exclusions}
    assert by_label["excluding_best_3_trades"].trades_excluded == 2  # capped at trade count


# --- Concentration. ---


def test_concentration_top_n_and_trades_to_reach_profit_thresholds():
    # winners: 6, 3, 1 (sum=10); one loser: -4. Net profit = 6.
    trades = [_trade(0, "6.0"), _trade(1, "3.0"), _trade(2, "1.0"), _trade(3, "-4.0")]
    result = _result("c_conc", "trend_regime", [_block(0, 0, trades)])
    pm = build_candidate_post_mortem(result)

    conc = pm.concentration
    assert conc.top1_pct_of_positive_pnl == 60.0  # 6/10
    assert conc.top3_pct_of_positive_pnl == 100.0  # all three winners
    # 50% of net profit (6) = 3.0 -> reached by the single best trade (6 >= 3.0)
    assert conc.trades_to_reach_50_pct_of_net_profit == 1
    # 100% of net profit (6) -> ranked desc [6,3,1,-4], cumulative: 6,9,10,6 -> reached at trade 1 already
    assert conc.trades_to_reach_100_pct_of_net_profit == 1


def test_concentration_is_none_when_aggregate_net_profit_is_not_positive():
    trades = [_trade(0, "1.0"), _trade(1, "-5.0")]
    result = _result("c_neg_conc", "trend_regime", [_block(0, 0, trades)])
    pm = build_candidate_post_mortem(result)
    assert pm.concentration.trades_to_reach_50_pct_of_net_profit is None
    assert pm.concentration.trades_to_reach_100_pct_of_net_profit is None


# --- Chronological stability: per-block, per-year, streaks, underwater, half split. ---


def test_chronological_stability_per_block_and_streaks_and_half_split():
    # Block 0: two winners. Block 1: three consecutive losers (the streak).
    block0_trades = [_trade(0, "1.0"), _trade(1, "1.0")]
    block1_trades = [_trade(2, "-1.0"), _trade(3, "-1.0"), _trade(4, "-1.0")]
    blocks = [_block(0, 0, block0_trades), _block(0, 1, block1_trades)]
    result = _result("c_chrono", "trend_regime", blocks)
    pm = build_candidate_post_mortem(result)

    cs = pm.chronological_stability
    assert len(cs.per_block) == 2
    assert cs.per_block[0].trade_count == 2
    assert cs.per_block[0].positive is True
    assert cs.per_block[1].positive is False
    assert cs.longest_losing_streak_trades == 3

    # 5 trades total -> first half = first 2, second half = last 3.
    assert cs.first_half.trade_count == 2
    assert cs.second_half.trade_count == 3
    assert cs.second_half.net_pnl_quote == Decimal("-3.0")

    # Monotonically non-decreasing cumulative curve up to block0, then a
    # 3-trade drawdown with no new high by the end -> underwater period is
    # defined and positive.
    assert cs.longest_underwater_period_days is not None
    assert cs.longest_underwater_period_days > 0


def test_chronological_stability_per_calendar_year_groups_correctly():
    import datetime as dt

    year1 = int(dt.datetime(2020, 6, 1, tzinfo=dt.UTC).timestamp() * 1000)
    year2 = int(dt.datetime(2021, 6, 1, tzinfo=dt.UTC).timestamp() * 1000)
    t1 = Trade(year1, year1 + STEP, Decimal(100), Decimal(101), Decimal(1), Decimal(0), Decimal("1.0"))
    t2 = Trade(year2, year2 + STEP, Decimal(100), Decimal(99), Decimal(1), Decimal(0), Decimal("-1.0"))
    result = _result("c_year", "trend_regime", [_block(0, 0, [t1, t2])])
    pm = build_candidate_post_mortem(result)

    years = {y.year: y for y in pm.chronological_stability.per_calendar_year}
    assert years[2020].net_pnl_quote == Decimal("1.0")
    assert years[2021].net_pnl_quote == Decimal("-1.0")


def test_no_underwater_period_when_cumulative_pnl_never_dips_below_a_prior_peak():
    trades = [_trade(0, "1.0"), _trade(1, "1.0"), _trade(2, "1.0")]
    result = _result("c_monotone", "trend_regime", [_block(0, 0, trades)])
    pm = build_candidate_post_mortem(result)
    assert pm.chronological_stability.longest_underwater_period_days is None


# --- Open position at block end: trade/plan correlation must not misalign or crash. ---


def test_open_position_at_block_end_does_not_misalign_trade_plan_correlation():
    trades = [_trade(0, "1.0"), _trade(1, "-0.5")]
    # entries_approved=3: two closed trades plus one still-open position at
    # the block's end - its plan values are a THIRD, trailing element in
    # each per-entry list with no corresponding closed Trade.
    rr = _rr_diag(
        trades,
        planned_risk_values=[Decimal("0.5"), Decimal("0.5"), Decimal("0.5")],
        planned_reward_values=[Decimal("1.0"), Decimal("1.0"), Decimal("1.0")],
        net_rr_values=[2.0, 2.0, 2.0],
        gross_rr_values=[2.0, 2.0, 2.0],
        entries_approved=3,
    )
    result = _result("c_open", "trend_regime", [_block(0, 0, trades, risk_reward=rr)])
    pm = build_candidate_post_mortem(result)

    assert pm.trade_counts.total_entries_approved == 3
    assert pm.trade_counts.closed_trades == 2
    # Only the first two (matched) planned-risk values feed the R distribution.
    assert pm.realized_r_distribution.trades_with_known_r == 2
    assert pm.realized_r_distribution.max_r == 2.0  # 1.0 / 0.5
    assert pm.realized_r_distribution.min_r == -1.0  # -0.5 / 0.5


# --- Breakout-specific exclusion check. ---


def test_breakout_family_gets_the_exclusion_check_other_families_do_not():
    trades = [_trade(0, "5.0")] + [_trade(i, "-0.2") for i in range(1, 10)]
    breakout_result = _result("breakout_X", BREAKOUT_FAMILY, [_block(0, 0, trades)])
    other_result = _result("trend_X", "trend_regime", [_block(0, 0, trades)])

    breakout_pm = build_candidate_post_mortem(breakout_result)
    other_pm = build_candidate_post_mortem(other_result)

    assert breakout_pm.breakout_exclusion_check.applicable is True
    assert breakout_pm.breakout_exclusion_check.remains_positive_excluding_best_trade is False
    assert other_pm.breakout_exclusion_check.applicable is False
    assert other_pm.breakout_exclusion_check.remains_positive_excluding_best_trade is None


# --- Evidence-only diagnosis rule. ---


def test_diagnosis_broad_positive_requires_surviving_every_declared_check():
    # 40 trades, spread evenly across 5 blocks, every block positive,
    # no single trade or top-3 group dominates.
    trades = [_trade(i, "0.5") for i in range(35)] + [_trade(i, "-0.2") for i in range(35, 40)]
    blocks = [_block(0, b, trades[b * 8 : (b + 1) * 8]) for b in range(5)]
    result = _result("c_broad", "trend_regime", blocks)
    pm = build_candidate_post_mortem(result)
    assert pm.diagnosis == EvidenceDiagnosis.BROAD_POSITIVE_EXPECTANCY


def test_diagnosis_concentrated_fragile_when_positive_only_via_one_dominant_trade():
    trades = [_trade(0, "20.0")] + [_trade(i, "-0.1") for i in range(1, 40)]
    blocks = [_block(0, b, trades[b * 8 : (b + 1) * 8]) for b in range(5)]
    result = _result("c_fragile", "trend_regime", blocks)
    pm = build_candidate_post_mortem(result)
    assert pm.diagnosis == EvidenceDiagnosis.CONCENTRATED_FRAGILE_POSITIVE_EXPECTANCY


# --- Costs, equity-accounting disclosure, and never-ranks ordering. ---


def test_cost_totals_sum_fees_and_slippage_across_all_blocks():
    t1 = Trade(
        START, START + STEP, Decimal(100), Decimal(101), Decimal(1), Decimal("0.1"), Decimal("0.9"),
        entry_fee_quote=Decimal("0.05"), exit_fee_quote=Decimal("0.05"),
        entry_reference_price=Decimal("99.5"), exit_reference_price=Decimal("101.5"),
    )
    result = _result("c_cost", "trend_regime", [_block(0, 0, [t1])])
    pm = build_candidate_post_mortem(result)
    assert pm.costs.total_fees_quote == Decimal("0.1")
    # slippage = |100-99.5|*1 + |101-101.5|*1 = 1.0
    assert pm.costs.total_slippage_quote == Decimal("1.0")


def test_equity_accounting_note_always_attached():
    result = _result("c_note", "trend_regime", [_block(0, 0, [_trade(0, "1.0")])])
    pm = build_candidate_post_mortem(result)
    assert pm.equity_accounting_note == EQUITY_ACCOUNTING_NOTE


def test_build_post_mortem_report_never_reorders_candidates():
    worse = _result("worse", "trend_regime", [_block(0, 0, [_trade(0, "-5.0")])])
    better = _result("better", "trend_regime", [_block(0, 0, [_trade(0, "5.0")])])
    report = build_post_mortem_report([worse, better])
    assert [c.candidate_id for c in report.candidates] == ["worse", "better"]


# --- Determinism: identical input -> byte-identical (dataclass-equal) output. ---


def test_deterministic_output_for_identical_input():
    trades = [_trade(0, "1.0"), _trade(1, "-0.5"), _trade(2, "2.0", exit_reason=EXIT_REASON_TAKE_PROFIT)]
    rr = _rr_diag(trades)
    result = _result("c_det", "trend_regime", [_block(0, 0, trades, risk_reward=rr)])
    first = build_candidate_post_mortem(result)
    second = build_candidate_post_mortem(result)
    assert first == second


# --- End-to-end integration: real engine -> real evaluation -> this report. ---


def test_end_to_end_through_a_real_blocked_chronological_evaluation():
    from tests.fixtures.exchange_info import make_exchange_info
    from trading_agent.config.models import AppConfig
    from trading_agent.data.models import Candle
    from trading_agent.research.cutoff import RESEARCH_CUTOFF_MS
    from trading_agent.sizing.exchange_filters import SymbolFilters

    real_start = RESEARCH_CUTOFF_MS - 3000 * STEP

    def candle(i: int, close: float) -> Candle:
        return Candle(
            symbol="BTCUSDT", interval=INTERVAL, open_time_ms=real_start + i * STEP,
            close_time_ms=real_start + i * STEP + STEP - 1,
            open=Decimal(str(close)), high=Decimal(str(close * 1.01)), low=Decimal(str(close * 0.99)),
            close=Decimal(str(close)), volume=Decimal(1),
        )

    closes = []
    price = 30000.0
    direction = 1
    for _ in range(30):
        for _ in range(15):
            price += direction * 300.0
            closes.append(price)
        direction *= -1
    candles = [candle(i, c) for i, c in enumerate(closes)]

    config = AppConfig(mode="backtest")
    filters = SymbolFilters.from_exchange_info(make_exchange_info(min_notional="0.01"))
    spec = next(s for s in CANDIDATE_REGISTRY if s.candidate_id == "trend_regime_A1")

    result = run_candidate_blocked_chronological_evaluation(spec, candles, config, filters)
    first = build_candidate_post_mortem(result)
    second = build_candidate_post_mortem(result)

    assert first == second  # deterministic given the same already-computed result
    assert first.candidate_id == "trend_regime_A1"
    assert first.diagnosis in set(EvidenceDiagnosis)
    for trade_r in first.realized_r_distribution.min_r, first.realized_r_distribution.max_r:
        assert trade_r is None or isinstance(trade_r, float)
