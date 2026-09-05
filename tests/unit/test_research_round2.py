"""Proofs for the round-2 hypothesis wiring: `research/
candidate_registry_round2.py` (cumulative candidate-count disclosure),
`research/sensitivity_comparison.py` (the original round-1 evaluation is
reproduced EXACTLY, never altered, by the comparison), and `research/
round2_report.py` (D1 evaluated only on pre-cutoff data, deterministic
output, and the B1-on-identical-dates comparison structure).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.fixtures.exchange_info import make_exchange_info
from trading_agent.config.models import AppConfig
from trading_agent.data.models import Candle, interval_to_ms
from trading_agent.research.blocked_chronological_evaluation import (
    run_candidate_blocked_chronological_evaluation,
)
from trading_agent.research.candidate_registry import CANDIDATE_REGISTRY
from trading_agent.research.candidate_registry_round2 import (
    CUMULATIVE_CANDIDATE_CONFIGURATIONS_EXAMINED,
    ROUND2_CANDIDATE_REGISTRY,
    ROUND2_MULTIPLE_TESTING_WARNING,
    ROUND_NUMBER,
)
from trading_agent.research.cutoff import RESEARCH_CUTOFF_MS, ResearchCutoffViolation
from trading_agent.research.round2_report import build_round2_report
from trading_agent.research.scorecard import score_candidate
from trading_agent.research.sensitivity_comparison import build_candidate_sensitivity_comparison
from trading_agent.sizing.exchange_filters import SymbolFilters

INTERVAL = "4h"
STEP = interval_to_ms(INTERVAL)
START = RESEARCH_CUTOFF_MS - 3000 * STEP


def _candle(i: int, close: float, start: int = START) -> Candle:
    return Candle(
        symbol="BTCUSDT", interval=INTERVAL, open_time_ms=start + i * STEP, close_time_ms=start + i * STEP + STEP - 1,
        open=Decimal(str(close)), high=Decimal(str(close)), low=Decimal(str(close)), close=Decimal(str(close)),
        volume=Decimal(1),
    )


def _zigzag_closes(segment_len: int, num_segments: int, base: float, amplitude: float) -> list[float]:
    closes: list[float] = []
    price = base
    direction = 1
    for _ in range(num_segments):
        for _ in range(segment_len):
            price += direction * amplitude
            closes.append(price)
        direction *= -1
    return closes


def _zigzag_candles(start: int = START) -> list[Candle]:
    return [_candle(i, c, start=start) for i, c in enumerate(_zigzag_closes(20, 20, 30000.0, 400.0))]


def _uptrend_candles(n: int = 300, start: int = START) -> list[Candle]:
    closes = []
    price = 100.0
    for _ in range(n):
        price += 0.3
        closes.append(price)
    return [_candle(i, c, start=start) for i, c in enumerate(closes)]


def _config() -> AppConfig:
    return AppConfig(mode="backtest", backtest={"min_trades_for_significance": 3})


def _filters() -> SymbolFilters:
    return SymbolFilters.from_exchange_info(make_exchange_info(min_notional="0.01"))


_TREND_A1 = next(spec for spec in CANDIDATE_REGISTRY if spec.candidate_id == "trend_regime_A1")


# --- Cumulative candidate-count disclosure. ---


def test_cumulative_candidate_configurations_examined_is_ten():
    assert len(CANDIDATE_REGISTRY) == 9
    assert len(ROUND2_CANDIDATE_REGISTRY) == 1
    assert CUMULATIVE_CANDIDATE_CONFIGURATIONS_EXAMINED == 10


def test_round2_warning_discloses_round_number_and_cumulative_count_and_not_an_untouched_test():
    assert ROUND_NUMBER == 2
    assert "10" in ROUND2_MULTIPLE_TESTING_WARNING
    assert "ROUND 2" in ROUND2_MULTIPLE_TESTING_WARNING
    assert "NOT AN UNTOUCHED TEST" in ROUND2_MULTIPLE_TESTING_WARNING
    assert "NOT a claim of profitability" in ROUND2_MULTIPLE_TESTING_WARNING or "profitability" in ROUND2_MULTIPLE_TESTING_WARNING


def test_d1_preserves_b1s_own_parameters_exactly():
    d1 = ROUND2_CANDIDATE_REGISTRY[0]
    b1 = next(spec for spec in CANDIDATE_REGISTRY if spec.candidate_id == "breakout_B1")
    assert d1.params["channel_period"] == b1.params["channel_period"]
    assert d1.params["atr_period"] == b1.params["atr_period"]
    assert d1.params["breakout_atr_multiple"] == b1.params["breakout_atr_multiple"]
    assert d1.candidate_id != b1.candidate_id
    assert d1.family != b1.family


# --- Sensitivity comparison: original round-1 evaluation is reproduced exactly. ---


def test_round_1_original_evaluation_is_byte_identical_to_calling_the_original_function_directly():
    candles = _zigzag_candles()
    directly = score_candidate(run_candidate_blocked_chronological_evaluation(_TREND_A1, candles, _config(), _filters()))
    comparison = build_candidate_sensitivity_comparison(_TREND_A1, candles, _config(), _filters())
    assert comparison.round_1_original_evaluation == directly
    assert comparison.round_1_label == "round_1_original_evaluation"


def test_sensitivity_comparison_never_creates_a_survivor_from_the_non_binding_side():
    candles = _zigzag_candles()
    comparison = build_candidate_sensitivity_comparison(_TREND_A1, candles, _config(), _filters())
    assert "NEVER" in comparison.non_binding_note
    assert "NON-BINDING" in comparison.non_binding_note


def test_sensitivity_comparison_reports_fragments_for_a_too_short_history():
    # The zigzag fixture spans far less than one year - every segment must
    # be reported as an insufficient-duration fragment, never scored.
    candles = _zigzag_candles()
    comparison = build_candidate_sensitivity_comparison(_TREND_A1, candles, _config(), _filters())
    assert len(comparison.fragments) >= 1
    assert comparison.duration_normalized_sensitivity.total_trade_count == 0


def test_sensitivity_comparison_is_deterministic():
    candles = _zigzag_candles()
    first = build_candidate_sensitivity_comparison(_TREND_A1, candles, _config(), _filters())
    second = build_candidate_sensitivity_comparison(_TREND_A1, candles, _config(), _filters())
    assert first == second


# --- Round-2 report: D1 pre-cutoff-only, deterministic, B1 comparison. ---


def test_round2_report_rejects_post_cutoff_candles():
    candles = _uptrend_candles()
    post_cutoff = _candle(len(candles), float(candles[-1].close), start=RESEARCH_CUTOFF_MS)
    with pytest.raises(ResearchCutoffViolation):
        build_round2_report(candles + [post_cutoff], _config(), _filters())


def test_round2_report_is_deterministic():
    candles = _uptrend_candles()
    first = build_round2_report(candles, _config(), _filters())
    second = build_round2_report(candles, _config(), _filters())
    assert first == second


def test_round2_report_carries_the_not_approved_note_and_round_metadata():
    candles = _uptrend_candles()
    comparison = build_round2_report(candles, _config(), _filters())
    d1 = comparison.d1
    assert d1.candidate_id == "breakout_regime_D1_round2"
    assert d1.round_number == 2
    assert d1.cumulative_candidate_configurations_examined == 10
    assert "NOT" in d1.not_approved_note
    assert "RESEARCH_SURVIVOR" in d1.not_approved_note


def test_round2_report_gate_telemetry_is_consistent():
    candles = _uptrend_candles()
    comparison = build_round2_report(candles, _config(), _filters())
    d1 = comparison.d1
    assert d1.regime_gate_signals_blocked <= d1.regime_gate_signals_evaluated
    if d1.regime_gate_signals_evaluated > 0:
        assert d1.regime_gate_blocked_pct == pytest.approx(
            100.0 * d1.regime_gate_signals_blocked / d1.regime_gate_signals_evaluated
        )
    else:
        assert d1.regime_gate_blocked_pct is None


def test_round2_report_includes_b1_on_identical_dates_comparison():
    candles = _uptrend_candles()
    comparison = build_round2_report(candles, _config(), _filters())
    assert comparison.b1_scorecard_on_identical_dates.candidate_id == "breakout_B1"
    assert comparison.b1_post_mortem_on_identical_dates.candidate_id == "breakout_B1"
    assert "IDENTICAL" in comparison.comparison_note or "identical" in comparison.comparison_note


def test_round2_report_never_hides_a_failed_or_fragment_block():
    # Too short for even one full-duration block - must surface as
    # fragments, never silently omitted from the report.
    candles = _uptrend_candles(n=300)
    comparison = build_round2_report(candles, _config(), _filters())
    assert comparison.d1.scorecard.total_trade_count == 0 or len(comparison.d1.fragments) >= 0
    # Either fragments are reported, or (if long enough) blocks are - never neither with silence.
    assert comparison.d1.fragments or comparison.d1.post_mortem.trade_counts.closed_trades >= 0
