"""Proofs for research/frozen_baseline.py: the rejected v0.1 EMA baseline
is reproduced exactly (ema_fast/ema_slow frozen regardless of the passed-in
config's own strategy section), and reproduction is the only research
function that may touch on-or-after-cutoff data.
"""

from decimal import Decimal

from tests.fixtures.exchange_info import make_exchange_info
from trading_agent.config.models import AppConfig
from trading_agent.data.models import Candle, interval_to_ms
from trading_agent.research.cutoff import RESEARCH_CUTOFF_MS
from trading_agent.research.frozen_baseline import (
    FROZEN_BASELINE_ID,
    FROZEN_BASELINE_STRATEGY_CONFIG,
    reproduce_frozen_baseline_report,
)
from trading_agent.sizing.exchange_filters import SymbolFilters

INTERVAL = "4h"
STEP = interval_to_ms(INTERVAL)


def _candles(closes: list[float], start: int) -> list[Candle]:
    return [
        Candle(
            symbol="BTCUSDT", interval=INTERVAL, open_time_ms=start + i * STEP, close_time_ms=start + i * STEP + STEP - 1,
            open=Decimal(str(c)), high=Decimal(str(c + 5)), low=Decimal(str(c - 5)), close=Decimal(str(c)),
            volume=Decimal(1),
        )
        for i, c in enumerate(closes)
    ]


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


def _filters() -> SymbolFilters:
    return SymbolFilters.from_exchange_info(make_exchange_info(min_notional="1"))


def test_frozen_baseline_id_is_the_exact_declared_string():
    assert FROZEN_BASELINE_ID == "ema_crossover_v0_1_rejected"


def test_frozen_baseline_uses_its_own_ema_periods_regardless_of_passed_config():
    # A caller's config declares wildly different EMA periods - the
    # reproduction must ignore them entirely and use the frozen 20/50.
    candles = _candles(_zigzag_closes(20, 10, 30000.0, 400.0), start=RESEARCH_CUTOFF_MS - 300 * STEP)
    config = AppConfig(mode="backtest", strategy={"ema_fast": 2, "ema_slow": 3})
    report = reproduce_frozen_baseline_report(candles, config, _filters())
    assert FROZEN_BASELINE_STRATEGY_CONFIG.ema_fast == 20
    assert FROZEN_BASELINE_STRATEGY_CONFIG.ema_slow == 50
    # min_required for the reproduction reflects 20/50, not 2/3 - proven
    # indirectly: the equity curve length matches ema_slow=50's warm-up,
    # not ema_slow=3's.
    assert len(report.continuous.equity_curve) == len(candles) - 50


def test_frozen_baseline_reproduction_accepts_on_or_after_cutoff_candles():
    # This is the one function allowed to receive consumed-period data -
    # must not raise.
    candles = _candles(_zigzag_closes(20, 10, 30000.0, 400.0), start=RESEARCH_CUTOFF_MS)
    config = AppConfig(mode="backtest")
    report = reproduce_frozen_baseline_report(candles, config, _filters())
    assert report.baseline_id == FROZEN_BASELINE_ID


def test_frozen_baseline_verdict_is_present_and_says_rejected():
    candles = _candles(_zigzag_closes(20, 10, 30000.0, 400.0), start=RESEARCH_CUTOFF_MS - 300 * STEP)
    config = AppConfig(mode="backtest")
    report = reproduce_frozen_baseline_report(candles, config, _filters())
    assert "REJECTED" in report.verdict
