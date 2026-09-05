"""Causal volatility/range indicators.

Every function here is strictly causal: index `k` of the output depends
only on `candles`/`values[0..k]`, never on anything after it - the same
causality property `moving_averages.py::ema` already documents and tests
prove (`tests/unit/test_indicators.py::test_ema_is_causal`). This is what
lets a research candidate call these on `candles[: i + 1]` (the existing
convention throughout this codebase) without ever leaking future data.
"""

from __future__ import annotations

import pandas as pd

from trading_agent.data.models import Candle


def true_range(candles: list[Candle]) -> list[float]:
    """Wilder's True Range: `max(high-low, |high-prev_close|, |low-prev_close|)`.

    The first candle has no previous close, so its true range is simply
    `high - low` - the only value causally available at that point.
    """
    values: list[float] = []
    prev_close: float | None = None
    for candle in candles:
        high, low = float(candle.high), float(candle.low)
        if prev_close is None:
            values.append(high - low)
        else:
            values.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        prev_close = float(candle.close)
    return values


def atr(candles: list[Candle], period: int) -> list[float]:
    """Average True Range: a simple rolling mean of `true_range` over
    `period` candles (causal - `rolling().mean()` never looks ahead)."""
    if period <= 0:
        raise ValueError("period must be positive")
    tr = pd.Series(true_range(candles), dtype="float64")
    return tr.rolling(window=period).mean().tolist()


def rolling_std(values: list[float], period: int) -> list[float]:
    if period <= 0:
        raise ValueError("period must be positive")
    series = pd.Series(values, dtype="float64")
    return series.rolling(window=period).std().tolist()


def rolling_max(values: list[float], period: int) -> list[float]:
    if period <= 0:
        raise ValueError("period must be positive")
    series = pd.Series(values, dtype="float64")
    return series.rolling(window=period).max().tolist()


def rolling_min(values: list[float], period: int) -> list[float]:
    if period <= 0:
        raise ValueError("period must be positive")
    series = pd.Series(values, dtype="float64")
    return series.rolling(window=period).min().tolist()
