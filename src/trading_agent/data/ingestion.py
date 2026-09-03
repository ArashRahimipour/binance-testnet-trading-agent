"""Candle ingestion with mandatory completed-candle filtering.

The strategy (Phase 2) must never see a candle that has not fully closed -
that would be look-ahead bias against itself (acting on a signal computed
from a still-forming bar). This module is the single choke point where that
filtering happens, based on server time rather than local wall-clock time,
so local clock drift cannot make an incomplete candle look complete.
"""

from __future__ import annotations

from trading_agent.data.exceptions import EmptyDataError
from trading_agent.data.market_data_public import BinancePublicMarketDataClient
from trading_agent.data.models import Candle


def fetch_completed_candles(
    client: BinancePublicMarketDataClient,
    symbol: str,
    interval: str,
    start_time_ms: int | None = None,
    end_time_ms: int | None = None,
    limit: int = 1000,
    reference_time_ms: int | None = None,
) -> list[Candle]:
    """Fetch klines and drop any candle that has not fully closed yet.

    `reference_time_ms` defaults to the exchange's own server time (not the
    local clock). A candle is considered complete only if its close time is
    strictly before the reference time.
    """
    if reference_time_ms is None:
        reference_time_ms = client.get_server_time_ms()

    raw_candles = client.get_klines(
        symbol=symbol,
        interval=interval,
        start_time_ms=start_time_ms,
        end_time_ms=end_time_ms,
        limit=limit,
    )
    return [c for c in raw_candles if c.close_time_ms < reference_time_ms]


def require_non_empty(candles: list[Candle]) -> list[Candle]:
    if not candles:
        raise EmptyDataError("no completed candles available")
    return candles
