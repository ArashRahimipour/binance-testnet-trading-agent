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

    `end_time_ms`, when given, is a genuine EXCLUSIVE upper bound on
    `open_time_ms` - never Binance's own inclusive `endTime` semantics
    (see `data/market_data_public.py::get_klines`'s own docstring for the
    incident this guards against). The request itself asks the exchange
    for one millisecond short of `end_time_ms`, and the result is filtered
    again regardless, so no candle at or after `end_time_ms` can ever be
    returned.
    """
    if reference_time_ms is None:
        reference_time_ms = client.get_server_time_ms()

    raw_candles = client.get_klines(
        symbol=symbol,
        interval=interval,
        start_time_ms=start_time_ms,
        end_time_ms=(end_time_ms - 1) if end_time_ms is not None else None,
        limit=limit,
    )
    completed = [c for c in raw_candles if c.close_time_ms < reference_time_ms]
    if end_time_ms is not None:
        completed = [c for c in completed if c.open_time_ms < end_time_ms]
    return completed


def require_non_empty(candles: list[Candle]) -> list[Candle]:
    if not candles:
        raise EmptyDataError("no completed candles available")
    return candles
