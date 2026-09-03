"""Candle data model.

Prices/volume are `Decimal` end-to-end from ingestion through validation and
storage, so no rounding error is introduced before position sizing (Phase 3)
does exchange-filter-aware rounding. Analytics code (indicators) is free to
convert to float internally since that precision loss is acceptable for a
trend signal, but the stored/raw candle never loses precision.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class Candle:
    symbol: str
    interval: str
    open_time_ms: int
    close_time_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    @staticmethod
    def from_binance_kline(symbol: str, interval: str, raw: list) -> Candle:
        """Parse one row of the Binance /api/v3/klines response array."""
        return Candle(
            symbol=symbol,
            interval=interval,
            open_time_ms=int(raw[0]),
            open=Decimal(str(raw[1])),
            high=Decimal(str(raw[2])),
            low=Decimal(str(raw[3])),
            close=Decimal(str(raw[4])),
            volume=Decimal(str(raw[5])),
            close_time_ms=int(raw[6]),
        )


INTERVAL_MS: dict[str, int] = {
    "1m": 60_000,
    "3m": 3 * 60_000,
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "30m": 30 * 60_000,
    "1h": 60 * 60_000,
    "2h": 2 * 60 * 60_000,
    "4h": 4 * 60 * 60_000,
    "6h": 6 * 60 * 60_000,
    "8h": 8 * 60 * 60_000,
    "12h": 12 * 60 * 60_000,
    "1d": 24 * 60 * 60_000,
    "3d": 3 * 24 * 60 * 60_000,
    "1w": 7 * 24 * 60 * 60_000,
}


def interval_to_ms(interval: str) -> int:
    if interval not in INTERVAL_MS:
        raise ValueError(f"unsupported or non-fixed-width interval: {interval!r}")
    return INTERVAL_MS[interval]
