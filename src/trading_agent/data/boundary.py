"""Shared half-open `[start, end)` boundary-safety helpers for every fetch
path that accepts an exclusive `end_time_ms`.

Binance's own kline `endTime` parameter is INCLUSIVE (a candle opening
exactly at `endTime` CAN be returned) - see `data/historical_fetch.py`'s
own module docstring for the real incident this guards against (a
BTCUSDT/1h candle stored exactly at the immutable research cutoff after a
`fetch-data --end 2025-05-16` run). Every caller that needs a genuinely
exclusive upper bound enforces it at two independent layers using the two
functions below: (1) the HTTP request itself asks for `end_time_ms - 1`,
and (2) the result is filtered again regardless of what the exchange
returned. `data/historical_fetch.py` and `data/gap_recovery.py` (its own
1-minute-kline fetches for gap reconstruction) both reuse these exact same
two functions, so the guarantee is defined and tested in exactly one
place.
"""

from __future__ import annotations

from trading_agent.data.models import Candle


def exclusive_upper_bound_for_request(end_time_ms: int) -> int:
    """Binance's kline `endTime` is INCLUSIVE - subtract 1ms so the HTTP
    request itself asks for a genuinely exclusive upper bound, rather than
    relying solely on post-hoc filtering to enforce `end_time_ms`."""
    return end_time_ms - 1


def below_exclusive_end(candles: list[Candle], end_time_ms: int | None) -> list[Candle]:
    """Defense-in-depth filter: drop any candle at or after `end_time_ms`,
    regardless of what the exchange returned. A no-op when `end_time_ms`
    is None (no upper bound was ever requested)."""
    if end_time_ms is None:
        return candles
    return [c for c in candles if c.open_time_ms < end_time_ms]
