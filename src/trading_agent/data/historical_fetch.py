"""Paginated historical candle acquisition for date ranges spanning more
than one page (Binance caps a single /api/v3/klines request at 1000
candles - a multi-year download needs many such requests).

Handles: cursor-based paging, bounded retries with exponential backoff,
429/Retry-After rate-limit responses, de-duplication by open time, and
gap detection/confirmation before returning anything to the caller.

Gap handling is deliberately NOT the same as live/Testnet's fail-closed
`validate_candle_sequence` (see data/validation.py, still used unchanged
by execution/live_runner.py). A multi-year download can legitimately hit
a real, permanent gap in the exchange's own historical record - raising
and discarding the entire download because of one missing candle is
worse than the alternative: detect every gap, make one focused, narrow-
range retry to rule out a pagination artifact or a transient API hiccup
(never assume a gap is genuine before trying this), and if it is still
missing, CONFIRM it, record it, and preserve every valid candle around it
- never fabricate or interpolate a replacement value. See
`data/gap_detection.py` for the segmentation this feeds into for
backtesting.

HALF-OPEN [start_time_ms, end_time_ms) BOUNDARY (post-incident correction):
Binance's own `/api/v3/klines` `endTime` parameter is INCLUSIVE - a candle
opening exactly at `endTime` CAN be returned by the exchange. A prior
version of this module passed the caller's exclusive `end_time_ms`
straight through as that inclusive `endTime`, which let exactly one
boundary candle (opening at `end_time_ms` itself) survive into the stored
result - concretely, a `fetch-data --end 2025-05-16` run stored a
BTCUSDT/1h candle at `open_time_ms == RESEARCH_CUTOFF_MS`, one instant
INSIDE the immutable, permanently-consumed research-cutoff period (see
`research/cutoff.py`). Every function below that accepts an `end_time_ms`
now enforces it as a genuine exclusive upper bound at TWO independent
layers: (1) the HTTP request itself asks Binance for `end_time_ms - 1`
(`_exclusive_upper_bound_for_request`), so the exchange is never even
asked for the boundary candle, and (2) every returned candle list is
still filtered to `open_time_ms < end_time_ms` regardless (see
`_below_exclusive_end`) - so even if Binance's inclusive-`endTime`
behavior ever changed, or a future caller forgot layer (1), no candle at
or after a requested `end_time_ms` can ever reach validation or storage.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

import requests

from trading_agent.data.exceptions import EmptyDataError
from trading_agent.data.gap_detection import GapRecord, partition_into_segments
from trading_agent.data.market_data_public import BinancePublicMarketDataClient
from trading_agent.data.models import Candle, interval_to_ms

PAGE_LIMIT = 1000
DEFAULT_MAX_RETRIES = 5
DEFAULT_BACKOFF_SECONDS = 2.0


class HistoricalFetchError(Exception):
    """Raised when a page could not be fetched after exhausting retries."""


@dataclass(frozen=True, slots=True)
class HistoricalFetchResult:
    candles: list[Candle]
    confirmed_gaps: list[GapRecord] = field(default_factory=list)


def _exclusive_upper_bound_for_request(end_time_ms: int) -> int:
    """Binance's kline `endTime` is INCLUSIVE - subtract 1ms so the HTTP
    request itself asks for a genuinely exclusive upper bound, rather than
    relying solely on post-hoc filtering to enforce `end_time_ms`."""
    return end_time_ms - 1


def _below_exclusive_end(candles: list[Candle], end_time_ms: int | None) -> list[Candle]:
    """Defense-in-depth filter: drop any candle at or after `end_time_ms`,
    regardless of what the exchange returned. A no-op when `end_time_ms`
    is None (no upper bound was ever requested)."""
    if end_time_ms is None:
        return candles
    return [c for c in candles if c.open_time_ms < end_time_ms]


def _assert_no_candle_at_or_after(candles: list[Candle], end_time_ms: int) -> None:
    """Final fail-closed gate, run immediately before a range fetch's
    result is handed back to the caller for storage: proves the exclusive
    end boundary held all the way through paging, dedup, and gap
    recovery. Should never fire given the filtering above - if it ever
    does, that is a regression in this module, not a reason to silently
    drop the offending candle and continue."""
    offending = [c.open_time_ms for c in candles if c.open_time_ms >= end_time_ms]
    if offending:
        raise HistoricalFetchError(
            f"{len(offending)} candle(s) at or after the requested exclusive end "
            f"({end_time_ms}ms) survived filtering - refusing to return them for storage: "
            f"{offending}"
        )


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
) -> HistoricalFetchResult:
    """Fetch every completed candle in [start_time_ms, end_time_ms), paging
    as needed, then detect and attempt to recover any gap before returning.

    `end_time_ms` is a genuine exclusive upper bound end-to-end (see this
    module's docstring) - no candle with `open_time_ms >= end_time_ms` can
    ever be returned, regardless of Binance's own inclusive `endTime`
    semantics.

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

    # Defense-in-depth (page accumulation layer): even though each page is
    # already filtered in `_fetch_page_with_retries`, re-assert the
    # boundary over the full accumulated list before it ever reaches
    # completeness filtering, dedup, or gap detection/recovery.
    all_candles = _below_exclusive_end(all_candles, end_time_ms)

    completed = [c for c in all_candles if c.close_time_ms < reference_time_ms]
    deduped = _dedup_by_open_time(completed)
    if not deduped:
        return HistoricalFetchResult(candles=[], confirmed_gaps=[])
    result = confirm_gaps(client, symbol, interval, deduped, max_retries, sleep_fn, end_time_ms=end_time_ms)
    _assert_no_candle_at_or_after(result.candles, end_time_ms)
    return result


def confirm_gaps(
    client: BinancePublicMarketDataClient,
    symbol: str,
    interval: str,
    candles: list[Candle],
    max_retries: int = DEFAULT_MAX_RETRIES,
    sleep_fn: Callable[[float], None] = time.sleep,
    end_time_ms: int | None = None,
) -> HistoricalFetchResult:
    """Detect every gap in an already-assembled, deduped candle list, make
    one focused narrow-range retry per gap (to rule out a pagination
    artifact or a transient API response before ever concluding the
    exchange itself is missing the data), and return the final candle
    list plus whichever gaps are still missing after that retry.

    Duplicates and out-of-order candles are never tolerated - they raise
    immediately (see `gap_detection.partition_into_segments`), exactly as
    live/Testnet validation does.

    `end_time_ms`, when given, is the same exclusive upper bound the
    caller's overall fetch was bounded by (see `fetch_historical_range`) -
    every recovered candle is filtered against it too, so gap recovery
    can never reintroduce a candle at or after it. `None` (the default,
    used by the `--limit`/non-ranged fetch path, which has no such bound)
    disables this filter entirely.
    """
    if not candles:
        raise EmptyDataError("no candles to check for gaps")

    segmentation = partition_into_segments(candles, interval)
    if not segmentation.gaps:
        return HistoricalFetchResult(candles=candles, confirmed_gaps=[])

    recovered_candles = list(candles)
    for gap in segmentation.gaps:
        recovered_candles.extend(
            _attempt_narrow_recovery(client, symbol, interval, gap, max_retries, sleep_fn, end_time_ms=end_time_ms)
        )

    final_candles = _dedup_by_open_time(recovered_candles)
    final_segmentation = partition_into_segments(final_candles, interval)
    return HistoricalFetchResult(candles=final_candles, confirmed_gaps=final_segmentation.gaps)


def _attempt_narrow_recovery(
    client: BinancePublicMarketDataClient,
    symbol: str,
    interval: str,
    gap: GapRecord,
    max_retries: int,
    sleep_fn: Callable[[float], None],
    end_time_ms: int | None = None,
) -> list[Candle]:
    """A focused, narrow re-query for exactly the suspected missing
    interval(s). Never assume a gap is real on the exchange side before
    trying this: a gap can result from this project's own pagination
    cursor math landing awkwardly at a page boundary, or from a transient
    API response that happened to omit a candle that a fresh request
    returns normally. Only if the candle is still absent after exhausting
    retries does the caller treat it as confirmed.

    `gap.next_open_time_ms` is already the recovery window's own exclusive
    upper bound (a known-good candle immediately after the gap); `end_time_ms`,
    when given, is additionally enforced so a caller's overall range boundary
    can never be exceeded even if a gap's own bookkeeping were ever wrong.
    """
    last_result: list[Candle] = []
    for attempt in range(max_retries):
        try:
            page = client.get_klines(
                symbol=symbol,
                interval=interval,
                start_time_ms=gap.expected_open_time_ms,
                end_time_ms=_exclusive_upper_bound_for_request(gap.next_open_time_ms),
                limit=gap.missing_intervals + 2,
            )
        except requests.exceptions.RequestException:
            sleep_fn(DEFAULT_BACKOFF_SECONDS * (2**attempt))
            continue

        last_result = [
            c for c in page
            if gap.expected_open_time_ms <= c.open_time_ms < gap.next_open_time_ms
        ]
        last_result = _below_exclusive_end(last_result, end_time_ms)
        if last_result:
            return last_result
        sleep_fn(DEFAULT_BACKOFF_SECONDS * (2**attempt))

    return last_result


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
            page = client.get_klines(
                symbol=symbol, interval=interval,
                start_time_ms=start_time_ms,
                end_time_ms=_exclusive_upper_bound_for_request(end_time_ms),
                limit=page_limit,
            )
            # Defense-in-depth (pagination request + page accumulation
            # layers): never trust the exchange's own inclusive `endTime`
            # handling alone, even though we already requested one
            # millisecond short of it above.
            return _below_exclusive_end(page, end_time_ms)
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
