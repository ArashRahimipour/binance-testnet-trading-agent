"""Paginated historical candle acquisition for date ranges spanning more
than one page (Binance caps a single /api/v3/klines request at 1000
candles - a multi-year download needs many such requests).

Handles: cursor-based paging, bounded retries with exponential backoff,
429/Retry-After rate-limit responses, de-duplication by open time, and
full-series validation before returning anything to the caller.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import requests

from trading_agent.data.market_data_public import BinancePublicMarketDataClient
from trading_agent.data.models import Candle, interval_to_ms
from trading_agent.data.validation import validate_candle_sequence

PAGE_LIMIT = 1000
DEFAULT_MAX_RETRIES = 5
DEFAULT_BACKOFF_SECONDS = 2.0


class HistoricalFetchError(Exception):
    """Raised when a page could not be fetched after exhausting retries."""


def fetch_historical_range(
    client: BinancePublicMarketDataClient,
    symbol: str,
    interval: str,
    start_time_ms: int,
    end_time_ms: int,
    max_retries: int = DEFAULT_MAX_RETRIES,
    page_limit: int = PAGE_LIMIT,
    sleep_fn: Callable[[float], None] = time.sleep,
    reference_time_ms: int | None = None,
) -> list[Candle]:
    """Fetch every completed candle in [start_time_ms, end_time_ms), paging
    as needed, validating the assembled series before returning it.

    `sleep_fn` is injectable so tests never actually sleep.
    """
    if reference_time_ms is None:
        reference_time_ms = client.get_server_time_ms()

    all_candles: list[Candle] = []
    cursor = start_time_ms
    step_ms = interval_to_ms(interval)

    while cursor < end_time_ms:
        page = _fetch_page_with_retries(
            client, symbol, interval, cursor, end_time_ms, page_limit, max_retries, sleep_fn
        )
        if not page:
            break
        all_candles.extend(page)
        cursor = page[-1].open_time_ms + step_ms
        if len(page) < page_limit:
            break  # short page - reached the end of what the exchange has

    completed = [c for c in all_candles if c.close_time_ms < reference_time_ms]
    deduped = _dedup_by_open_time(completed)
    if deduped:
        validate_candle_sequence(deduped, interval)
    return deduped


def _fetch_page_with_retries(
    client: BinancePublicMarketDataClient,
    symbol: str,
    interval: str,
    start_time_ms: int,
    end_time_ms: int,
    page_limit: int,
    max_retries: int,
    sleep_fn: Callable[[float], None],
) -> list[Candle]:
    last_exception: Exception | None = None
    for attempt in range(max_retries):
        try:
            return client.get_klines(
                symbol=symbol, interval=interval,
                start_time_ms=start_time_ms, end_time_ms=end_time_ms, limit=page_limit,
            )
        except requests.exceptions.HTTPError as exc:
            last_exception = exc
            response = exc.response
            if response is not None and response.status_code == 429:
                retry_after = float(response.headers.get("Retry-After", DEFAULT_BACKOFF_SECONDS))
                sleep_fn(retry_after)
            else:
                sleep_fn(DEFAULT_BACKOFF_SECONDS * (2**attempt))
        except requests.exceptions.RequestException as exc:
            last_exception = exc
            sleep_fn(DEFAULT_BACKOFF_SECONDS * (2**attempt))

    raise HistoricalFetchError(
        f"failed to fetch page starting at {start_time_ms} after {max_retries} attempts: {last_exception}"
    )


def _dedup_by_open_time(candles: list[Candle]) -> list[Candle]:
    seen: dict[int, Candle] = {}
    for candle in candles:
        seen[candle.open_time_ms] = candle
    return sorted(seen.values(), key=lambda c: c.open_time_ms)
