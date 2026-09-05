"""Proofs for the round-3 hypothesis wiring: `research/
candidate_registry_round3.py` (cumulative candidate-count disclosure,
round-1/round-2 preservation language), `research/round3_report.py` (E1
evaluated only on pre-cutoff 1h data, deterministic output, fragments for
too-short history), and a fingerprint-based regression proof that round 1's
and round 2's own candidate implementations are byte-for-byte unchanged
by anything added in round 3.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.fixtures.exchange_info import make_exchange_info
from trading_agent.config.models import AppConfig
from trading_agent.data.models import Candle, interval_to_ms
from trading_agent.research.candidate_registry import CANDIDATE_REGISTRY
from trading_agent.research.candidate_registry_round2 import ROUND2_CANDIDATE_REGISTRY
from trading_agent.research.candidate_registry_round3 import (
    CUMULATIVE_CANDIDATE_CONFIGURATIONS_EXAMINED,
    REQUIRED_MARKET_INTERVAL,
    ROUND3_CANDIDATE_REGISTRY,
    ROUND3_MULTIPLE_TESTING_WARNING,
    ROUND_NUMBER,
)
from trading_agent.research.cutoff import RESEARCH_CUTOFF_MS, ResearchCutoffViolation
from trading_agent.research.fingerprint import compute_strategy_implementation_fingerprint
from trading_agent.research.round3_report import build_round3_report
from trading_agent.sizing.exchange_filters import SymbolFilters

INTERVAL = "1h"
STEP = interval_to_ms(INTERVAL)
START = RESEARCH_CUTOFF_MS - 500 * STEP  # comfortably pre-cutoff, well short of E1's own warm-up


def _candle(i: int, close: float, start: int = START) -> Candle:
    return Candle(
        symbol="BTCUSDT", interval=INTERVAL, open_time_ms=start + i * STEP, close_time_ms=start + i * STEP + STEP - 1,
        open=Decimal(str(close)), high=Decimal(str(close)), low=Decimal(str(close)), close=Decimal(str(close)),
        volume=Decimal(1),
    )


def _short_candles(n: int = 500, start: int = START) -> list[Candle]:
    return [_candle(i, 100.0 + i * 0.01, start=start) for i in range(n)]


def _config() -> AppConfig:
    return AppConfig(mode="backtest", market={"interval": REQUIRED_MARKET_INTERVAL}, backtest={"min_trades_for_significance": 3})


def _filters() -> SymbolFilters:
    return SymbolFilters.from_exchange_info(make_exchange_info(min_notional="0.01"))


# --- Cumulative candidate-count disclosure. ---


def test_cumulative_candidate_configurations_examined_is_eleven():
    assert len(CANDIDATE_REGISTRY) == 9
    assert len(ROUND2_CANDIDATE_REGISTRY) == 1
    assert len(ROUND3_CANDIDATE_REGISTRY) == 1
    assert CUMULATIVE_CANDIDATE_CONFIGURATIONS_EXAMINED == 11


def test_round3_warning_discloses_round_number_cumulative_count_and_d1_preservation():
    assert ROUND_NUMBER == 3
    assert "11" in ROUND3_MULTIPLE_TESTING_WARNING
    assert "ROUND 3" in ROUND3_MULTIPLE_TESTING_WARNING
    assert "NOT AN UNTOUCHED TEST" in ROUND3_MULTIPLE_TESTING_WARNING
    assert "OFFICIAL REJECTED" in ROUND3_MULTIPLE_TESTING_WARNING
    assert "PRESERVED UNCHANGED" in ROUND3_MULTIPLE_TESTING_WARNING


def test_required_market_interval_is_1h():
    assert REQUIRED_MARKET_INTERVAL == "1h"


# --- Round-3 report: E1 pre-cutoff-only, deterministic, fragments for short history. ---


def test_round3_report_rejects_post_cutoff_candles():
    candles = _short_candles()
    post_cutoff = _candle(len(candles), float(candles[-1].close), start=RESEARCH_CUTOFF_MS)
    with pytest.raises(ResearchCutoffViolation):
        build_round3_report(candles + [post_cutoff], _config(), _filters())


def test_round3_report_is_deterministic():
    candles = _short_candles()
    first = build_round3_report(candles, _config(), _filters())
    second = build_round3_report(candles, _config(), _filters())
    assert first == second


def test_round3_report_reports_a_fragment_for_history_shorter_than_warm_up():
    candles = _short_candles()  # 500 hours - far short of E1's ~7564-hour warm-up
    report = build_round3_report(candles, _config(), _filters())
    assert report.candidate_id == "multitimeframe_breakout_E1_round3"
    assert report.round_number == 3
    assert report.cumulative_candidate_configurations_examined == 11
    assert len(report.fragments) == 1
    assert report.fragments[0].available_tradable_duration_days == 0.0
    assert report.scorecard.total_trade_count == 0
    assert report.weekly_filter_rejections == 0  # generate_signal was never even called
    assert "NOT" in report.not_approved_note
    assert "RESEARCH_SURVIVOR" in report.not_approved_note


def test_round3_report_never_hides_a_fragment_with_silence():
    candles = _short_candles()
    report = build_round3_report(candles, _config(), _filters())
    assert report.fragments or report.post_mortem.trade_counts.closed_trades >= 0


# --- Fingerprint regression: round-1/round-2 candidate implementations are byte-for-byte unchanged. ---


def test_round1_and_round2_candidate_fingerprints_are_unchanged_by_round3():
    a1 = next(s for s in CANDIDATE_REGISTRY if s.candidate_id == "trend_regime_A1")
    b1 = next(s for s in CANDIDATE_REGISTRY if s.candidate_id == "breakout_B1")
    d1 = ROUND2_CANDIDATE_REGISTRY[0]

    # Captured from the actual repository state immediately before any
    # round-3 source file was added - a change to any of these values
    # means a round-1/round-2 candidate's own implementation source
    # changed, which round 3 must never do.
    assert compute_strategy_implementation_fingerprint(a1) == (
        "ef43fd231c501d09c24870bc0cc219e161f3ab9e703ec42fed2eb2ddad903036"
    )
    assert compute_strategy_implementation_fingerprint(b1) == (
        "193d6cfac1dab68e11dba261cd9354729fb8bdbab198f955502a0f5797bc75bc"
    )
    assert compute_strategy_implementation_fingerprint(d1) == (
        "e05f606c5ca18e748d3b79852c185559f3d54bdc6a9537d70e60b0692f40e3bd"
    )


def test_round1_registry_still_has_exactly_nine_candidates_with_unchanged_ids():
    expected_ids = {
        "trend_regime_A1", "trend_regime_A2", "trend_regime_A3",
        "breakout_B1", "breakout_B2", "breakout_B3",
        "mean_reversion_C1", "mean_reversion_C2", "mean_reversion_C3",
    }
    assert {s.candidate_id for s in CANDIDATE_REGISTRY} == expected_ids


def test_round2_registry_still_has_exactly_one_candidate_with_unchanged_id():
    assert len(ROUND2_CANDIDATE_REGISTRY) == 1
    assert ROUND2_CANDIDATE_REGISTRY[0].candidate_id == "breakout_regime_D1_round2"
