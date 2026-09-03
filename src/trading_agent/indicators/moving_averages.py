"""Causal moving-average indicators.

`ema` uses `adjust=False`, which makes it a strictly causal recursive
filter: `EMA[k]` is a function only of `price[0..k]`. That is what makes it
safe against look-ahead bias - computing it over a truncated series produces
identical values for every index that exists in both the truncated and full
series (see tests/unit/test_indicators.py::test_ema_is_causal).
"""

from __future__ import annotations

import pandas as pd


def ema(values: list[float], period: int) -> list[float]:
    if period <= 0:
        raise ValueError("period must be positive")
    series = pd.Series(values, dtype="float64")
    return series.ewm(span=period, adjust=False).mean().tolist()


def sma(values: list[float], period: int) -> list[float]:
    if period <= 0:
        raise ValueError("period must be positive")
    series = pd.Series(values, dtype="float64")
    return series.rolling(window=period).mean().tolist()
