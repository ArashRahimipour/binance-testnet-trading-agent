"""Regression tests for the fetch-pipeline half-open `[start, end)`
boundary defect discovered from real database metadata: a BTCUSDT/1h
candle was stored with `open_time_ms == RESEARCH_CUTOFF_MS`
(1747353600000) after a `fetch-data --end 2025-05-16` run, because
Binance's own `/api/v3/klines` `endTime` parameter is INCLUSIVE and the
fetch pipeline forwarded a caller's exclusive `end_time_ms` straight
through as that inclusive boundary, with no filter of its own.

This file proves the fix holds at every layer named in the incident
follow-up: pagination requests, page accumulation, narrow gap recovery,
and CLI/documentation wording - entirely with synthetic fixtures and
local mocks. No real Binance host is contacted, no real database is
touched, `research-round3` is never invoked, and no market
price/signal/result is inspected anywhere in this file - only
`open_time_ms` boundary arithmetic.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import responses

from tests.fixtures.klines import make_kline_row
from trading_agent.cli.main import fetch_data
from trading_agent.data.gap_detection import GapRecord
from trading_agent.data.historical_fetch import (
    _attempt_narrow_recovery,
    _below_exclusive_end,
    _exclusive_upper_bound_for_request,
    fetch_historical_range,
)
from trading_agent.data.market_data_public import TESTNET_HOST, BinancePublicMarketDataClient
from trading_agent.data.models import Candle, interval_to_ms
from trading_agent.research.cutoff import RESEARCH_CUTOFF_MS, assert_pre_cutoff, split_at_cutoff

INTERVAL = "1h"
STEP = interval_to_ms(INTERVAL)
START = 1_700_000_000_000


def _rows(start: int, count: int) -> dict[int, list]:
    return {start + i * STEP: make_kline_row(start + i * STEP, INTERVAL) for i in range(count)}


def _lenient_exchange_callback(rows_by_open_time: dict[int, list]):
    """A deliberately WORST-CASE mock exchange: ignores the request's own
    `endTime` entirely (ships every row from `startTime` onward, capped
    only by `limit`) - strictly more permissive than even Binance's own
    real (already-inclusive-of-`endTime`) behavior. Used to prove the
    client-side filter is what actually enforces the boundary, never an
    assumption about how any particular exchange response is shaped.
    """
    sorted_times = sorted(rows_by_open_time)

    def _callback(request):
        query = parse_qs(urlparse(request.url).query)
        start = int(query["startTime"][0]) if "startTime" in query else sorted_times[0]
        limit = int(query.get("limit", ["1000"])[0])
        matching = [t for t in sorted_times if t >= start][:limit]
        body = [rows_by_open_time[t] for t in matching]
        return (200, {"Content-Type": "application/json"}, json.dumps(body))

    return _callback


class _StubClient:
    """A test double for `_attempt_narrow_recovery`'s gap-recovery request
    that, like `_lenient_exchange_callback` above, always returns every
    candle it was constructed with regardless of the `start_time_ms`/
    `end_time_ms` it was actually called with - isolates the assertion to
    what OUR OWN code does with the response, not what it asked for."""

    def __init__(self, candles: list[Candle]) -> None:
        self._candles = candles

    def get_klines(self, symbol, interval, start_time_ms=None, end_time_ms=None, limit=1000):
        return list(self._candles)


def _candle(open_time_ms: int) -> Candle:
    from decimal import Decimal

    return Candle(
        symbol="BTCUSDT", interval=INTERVAL, open_time_ms=open_time_ms, close_time_ms=open_time_ms + STEP - 1,
        open=Decimal(100), high=Decimal(101), low=Decimal(99), close=Decimal(100), volume=Decimal(1),
    )


# --- 1 & 2. A candle exactly at `end` is never stored; a candle one
# interval before `end` IS stored. ---


@responses.activate
def test_a_candle_exactly_at_end_is_never_returned_even_when_the_exchange_offers_it():
    # 11 candles, START..START+10*STEP. Request end=START+5*STEP - candles
    # 0..4 are legitimately in range; the mock exchange (worst-case,
    # ignores endTime) would happily also hand back candle 5 (exactly AT
    # end) and everything after it if our own filtering did not stop it.
    rows = _rows(START, 11)
    responses.add_callback(
        responses.GET, f"{TESTNET_HOST}/api/v3/klines", callback=_lenient_exchange_callback(rows)
    )
    client = BinancePublicMarketDataClient(TESTNET_HOST)
    end_time_ms = START + 5 * STEP
    result = fetch_historical_range(
        client, "BTCUSDT", INTERVAL, START, end_time_ms,
        page_limit=1000, reference_time_ms=START + 100 * STEP, sleep_fn=lambda s: None,
    )
    stored_open_times = [c.open_time_ms for c in result.candles]
    assert end_time_ms not in stored_open_times
    assert all(t < end_time_ms for t in stored_open_times)


@responses.activate
def test_a_candle_one_interval_before_end_is_stored():
    rows = _rows(START, 11)
    responses.add_callback(
        responses.GET, f"{TESTNET_HOST}/api/v3/klines", callback=_lenient_exchange_callback(rows)
    )
    client = BinancePublicMarketDataClient(TESTNET_HOST)
    end_time_ms = START + 5 * STEP
    result = fetch_historical_range(
        client, "BTCUSDT", INTERVAL, START, end_time_ms,
        page_limit=1000, reference_time_ms=START + 100 * STEP, sleep_fn=lambda s: None,
    )
    stored_open_times = {c.open_time_ms for c in result.candles}
    assert (end_time_ms - STEP) in stored_open_times
    assert stored_open_times == {START + i * STEP for i in range(5)}  # exactly candles 0..4, nothing more/less


# --- 3. Pagination cannot reintroduce the boundary candle. ---


@responses.activate
def test_pagination_with_a_small_page_limit_cannot_reintroduce_the_boundary_candle():
    # 12 candles; a page_limit of 2 forces 6+ round trips. The lenient
    # mock ignores endTime and would keep serving candles straight through
    # and past the boundary on every single page if per-page filtering
    # (not just a final cleanup pass) were not in effect.
    rows = _rows(START, 12)
    responses.add_callback(
        responses.GET, f"{TESTNET_HOST}/api/v3/klines", callback=_lenient_exchange_callback(rows)
    )
    client = BinancePublicMarketDataClient(TESTNET_HOST)
    end_time_ms = START + 7 * STEP
    result = fetch_historical_range(
        client, "BTCUSDT", INTERVAL, START, end_time_ms,
        page_limit=2, reference_time_ms=START + 100 * STEP, sleep_fn=lambda s: None,
    )
    stored_open_times = sorted(c.open_time_ms for c in result.candles)
    assert stored_open_times == [START + i * STEP for i in range(7)]
    assert all(t < end_time_ms for t in stored_open_times)


@responses.activate
def test_a_single_short_response_at_the_boundary_terminates_pagination_without_storing_it():
    # Exactly the pagination shape near a range's tail: the final page
    # legitimately contains fewer than page_limit rows AND its last row
    # sits exactly at the requested end - both must be handled together
    # (the short-page-break heuristic must not accidentally keep the
    # boundary row it used to decide the page was short).
    rows = _rows(START, 6)  # opens: START..START+5*STEP
    responses.add_callback(
        responses.GET, f"{TESTNET_HOST}/api/v3/klines", callback=_lenient_exchange_callback(rows)
    )
    client = BinancePublicMarketDataClient(TESTNET_HOST)
    end_time_ms = START + 5 * STEP  # the mock's last row (index 5) sits exactly here
    result = fetch_historical_range(
        client, "BTCUSDT", INTERVAL, START, end_time_ms,
        page_limit=1000, reference_time_ms=START + 100 * STEP, sleep_fn=lambda s: None,
    )
    assert [c.open_time_ms for c in result.candles] == [START + i * STEP for i in range(5)]


# --- 4. Gap recovery cannot fetch/store beyond end. ---


def test_narrow_gap_recovery_cannot_store_a_candle_at_or_after_the_requested_end():
    # Deliberately adversarial: a gap whose own `next_open_time_ms` (a
    # supposedly known-good candle) sits BEYOND the caller's overall
    # requested end - the recovery client (a stub that returns every
    # candle regardless of what it was asked for, same worst-case
    # posture as the exchange callback above) offers three candles
    # spanning across that boundary. `end_time_ms` must be enough on its
    # own to keep the boundary-violating ones out even though the gap's
    # own internal bound would have let them through.
    gap = GapRecord(
        expected_open_time_ms=START,
        previous_open_time_ms=START - STEP,
        next_open_time_ms=START + 3 * STEP,
        missing_intervals=2,
    )
    stub = _StubClient([_candle(START), _candle(START + STEP), _candle(START + 2 * STEP)])
    end_time_ms = START + STEP  # only the very first recovered candle is legitimately in range

    recovered = _attempt_narrow_recovery(
        stub, "BTCUSDT", INTERVAL, gap, max_retries=1, sleep_fn=lambda s: None, end_time_ms=end_time_ms
    )

    assert [c.open_time_ms for c in recovered] == [START]
    assert all(c.open_time_ms < end_time_ms for c in recovered)


def test_narrow_gap_recovery_without_an_end_bound_is_unaffected_by_the_new_parameter():
    # The `--limit` (non-ranged) fetch path calls confirm_gaps with no
    # end_time_ms at all - the new parameter must be fully opt-in and
    # never change behavior when omitted (default None).
    gap = GapRecord(
        expected_open_time_ms=START, previous_open_time_ms=START - STEP,
        next_open_time_ms=START + 2 * STEP, missing_intervals=1,
    )
    stub = _StubClient([_candle(START)])
    recovered = _attempt_narrow_recovery(stub, "BTCUSDT", INTERVAL, gap, max_retries=1, sleep_fn=lambda s: None)
    assert [c.open_time_ms for c in recovered] == [START]


def test_below_exclusive_end_helper_is_a_noop_when_end_is_none():
    candles = [_candle(START), _candle(START + STEP)]
    assert _below_exclusive_end(candles, None) == candles


def test_exclusive_upper_bound_for_request_subtracts_exactly_one_millisecond():
    assert _exclusive_upper_bound_for_request(START) == START - 1


# --- 5. research-round3 (via the shared cutoff machinery) rejects/
# excludes every candle at or after RESEARCH_CUTOFF_MS - tied directly to
# THIS incident's own reported values. `research-round3` itself is never
# invoked here (per the audit's own instruction); this proves the cutoff
# guarantee it relies on, and that the fixed fetch pipeline structurally
# cannot even produce the offending candle in the first place. ---


def test_the_exact_reported_offending_candle_would_be_excluded_by_split_at_cutoff():
    # The real incident's own reported value: a BTCUSDT/1h candle stored
    # at open_time_ms == RESEARCH_CUTOFF_MS == 1747353600000.
    assert RESEARCH_CUTOFF_MS == 1747353600000
    last_legitimate = _candle(RESEARCH_CUTOFF_MS - STEP)
    offending = _candle(RESEARCH_CUTOFF_MS)
    pre_cutoff, consumed = split_at_cutoff([last_legitimate, offending])
    assert pre_cutoff == [last_legitimate]
    assert consumed == [offending]
    assert_pre_cutoff(pre_cutoff)  # must not raise


@responses.activate
def test_fetching_with_end_equal_to_the_research_cutoff_cannot_produce_the_offending_candle():
    # Reproduces the exact incident scenario end-to-end through the FIXED
    # pipeline: a `fetch_historical_range` call with `end_time_ms =
    # RESEARCH_CUTOFF_MS` (exactly what `fetch-data --end 2025-05-16`
    # computes) against a worst-case exchange that would happily hand
    # back the cutoff candle and beyond. The result must be immediately
    # safe to hand to `assert_pre_cutoff` with zero filtering needed.
    rows = _rows(RESEARCH_CUTOFF_MS - 3 * STEP, 6)  # 3 legitimate + cutoff candle + 2 beyond it
    responses.add_callback(
        responses.GET, f"{TESTNET_HOST}/api/v3/klines", callback=_lenient_exchange_callback(rows)
    )
    client = BinancePublicMarketDataClient(TESTNET_HOST)
    result = fetch_historical_range(
        client, "BTCUSDT", INTERVAL, RESEARCH_CUTOFF_MS - 3 * STEP, RESEARCH_CUTOFF_MS,
        page_limit=1000, reference_time_ms=RESEARCH_CUTOFF_MS + 100 * STEP, sleep_fn=lambda s: None,
    )
    assert [c.open_time_ms for c in result.candles] == [RESEARCH_CUTOFF_MS - i * STEP for i in (3, 2, 1)]
    assert_pre_cutoff(result.candles)  # must not raise - nothing at/after cutoff ever reached this result


# --- 6. Documentation and CLI wording match actual behavior. ---


def test_end_option_help_text_states_the_boundary_is_exclusive():
    end_option = next(p for p in fetch_data.params if p.name == "end_str")
    assert "exclusive" in end_option.help.lower()
    assert "never fetched" in end_option.help.lower() or "never stored" in end_option.help.lower()


def test_fetch_data_command_docstring_documents_the_half_open_range_and_the_incident():
    doc = (fetch_data.callback.__doc__ or "").lower()
    assert "half-open" in doc
    assert "exclusive" in doc
    assert "2025-05-16" in doc or "research cutoff" in doc or "requested --end" in doc or "candle exactly at" in doc


def test_get_klines_docstring_documents_binance_inclusive_endtime_semantics():
    import inspect

    from trading_agent.data.market_data_public import BinancePublicMarketDataClient as _Client

    doc = inspect.getdoc(_Client.get_klines) or ""
    assert "inclusive" in doc.lower()
    assert "endTime" in doc


def test_historical_fetch_module_docstring_documents_the_incident_and_the_fix():
    import trading_agent.data.historical_fetch as historical_fetch_module

    doc = (historical_fetch_module.__doc__ or "").lower()
    assert "inclusive" in doc
    assert "half-open" in doc
    assert "research_cutoff_ms" in doc or "1747353600000" in doc or "cutoff" in doc
