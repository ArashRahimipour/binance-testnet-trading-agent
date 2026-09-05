import responses

from tests.fixtures.klines import make_kline_row, make_kline_series
from trading_agent.data.historical_fetch import confirm_gaps, fetch_historical_range
from trading_agent.data.market_data_public import TESTNET_HOST, BinancePublicMarketDataClient
from trading_agent.data.models import Candle, interval_to_ms

INTERVAL = "4h"
STEP = interval_to_ms(INTERVAL)
START = 1_700_000_000_000

# The exact real-world gap discovered during the first multi-year download.
REAL_GAP_PREVIOUS_OPEN_TIME_MS = 1582099200000
REAL_GAP_NEXT_OPEN_TIME_MS = 1582128000000


def _sleep_recorder(recorded: list[float]):
    def _sleep(seconds: float) -> None:
        recorded.append(seconds)

    return _sleep


@responses.activate
def test_single_page_when_range_is_small():
    rows = make_kline_series(START, INTERVAL, 5)
    responses.add(responses.GET, f"{TESTNET_HOST}/api/v3/klines", json=rows, status=200)
    client = BinancePublicMarketDataClient(TESTNET_HOST)
    reference_time_ms = rows[-1][6] + 1
    result = fetch_historical_range(
        client, "BTCUSDT", INTERVAL, START, START + 5 * STEP, reference_time_ms=reference_time_ms
    )
    assert len(result.candles) == 5
    assert result.confirmed_gaps == []


@responses.activate
def test_pages_across_multiple_requests_when_first_page_is_full():
    page_limit = 3
    first_page = make_kline_series(START, INTERVAL, page_limit)
    second_page = make_kline_series(START + page_limit * STEP, INTERVAL, 2)
    responses.add(responses.GET, f"{TESTNET_HOST}/api/v3/klines", json=first_page, status=200)
    responses.add(responses.GET, f"{TESTNET_HOST}/api/v3/klines", json=second_page, status=200)
    client = BinancePublicMarketDataClient(TESTNET_HOST)
    reference_time_ms = second_page[-1][6] + 1
    result = fetch_historical_range(
        client, "BTCUSDT", INTERVAL, START, START + 5 * STEP,
        page_limit=page_limit, reference_time_ms=reference_time_ms,
    )
    assert len(result.candles) == 5
    assert result.candles[0].open_time_ms == START
    assert result.candles[-1].open_time_ms == START + 4 * STEP
    assert result.confirmed_gaps == []


@responses.activate
def test_stops_paging_on_short_page():
    page_limit = 10
    short_page = make_kline_series(START, INTERVAL, 3)  # fewer than page_limit -> no more pages
    responses.add(responses.GET, f"{TESTNET_HOST}/api/v3/klines", json=short_page, status=200)
    client = BinancePublicMarketDataClient(TESTNET_HOST)
    reference_time_ms = short_page[-1][6] + 1
    result = fetch_historical_range(
        client, "BTCUSDT", INTERVAL, START, START + 1000 * STEP,
        page_limit=page_limit, reference_time_ms=reference_time_ms,
    )
    assert len(result.candles) == 3
    assert len(responses.calls) == 1  # never asked for a second page


@responses.activate
def test_excludes_incomplete_candles_from_final_page():
    rows = make_kline_series(START, INTERVAL, 5)
    responses.add(responses.GET, f"{TESTNET_HOST}/api/v3/klines", json=rows, status=200)
    client = BinancePublicMarketDataClient(TESTNET_HOST)
    # reference time falls inside the last candle's window.
    reference_time_ms = rows[-1][0] + STEP // 2
    result = fetch_historical_range(
        client, "BTCUSDT", INTERVAL, START, START + 5 * STEP, reference_time_ms=reference_time_ms
    )
    assert len(result.candles) == 4


@responses.activate
def test_retries_on_429_using_retry_after_header():
    rows = make_kline_series(START, INTERVAL, 2)
    recorded_sleeps: list[float] = []
    responses.add(
        responses.GET, f"{TESTNET_HOST}/api/v3/klines",
        json={"code": -1003, "msg": "Too many requests"}, status=429, headers={"Retry-After": "3"},
    )
    responses.add(responses.GET, f"{TESTNET_HOST}/api/v3/klines", json=rows, status=200)
    client = BinancePublicMarketDataClient(TESTNET_HOST)
    reference_time_ms = rows[-1][6] + 1
    result = fetch_historical_range(
        client, "BTCUSDT", INTERVAL, START, START + 2 * STEP,
        reference_time_ms=reference_time_ms, sleep_fn=_sleep_recorder(recorded_sleeps),
    )
    assert len(result.candles) == 2
    assert recorded_sleeps == [3.0]


@responses.activate
def test_gives_up_after_max_retries():
    from trading_agent.data.historical_fetch import HistoricalFetchError

    for _ in range(3):
        responses.add(
            responses.GET, f"{TESTNET_HOST}/api/v3/klines",
            json={"code": -1003, "msg": "Too many requests"}, status=429,
        )
    client = BinancePublicMarketDataClient(TESTNET_HOST)
    try:
        fetch_historical_range(
            client, "BTCUSDT", INTERVAL, START, START + 2 * STEP,
            max_retries=3, reference_time_ms=START + 10 * STEP, sleep_fn=_sleep_recorder([]),
        )
        raise AssertionError("expected HistoricalFetchError")
    except HistoricalFetchError:
        pass


@responses.activate
def test_deduplicates_overlapping_pages():
    # If a page overlaps with the previous one (defensive - shouldn't
    # normally happen given cursor advancement, but must not corrupt data).
    # page2 is deliberately SHORT (< page_limit) so pagination stops here.
    page1 = make_kline_series(START, INTERVAL, 3)
    page2 = make_kline_series(START + 2 * STEP, INTERVAL, 2)  # overlaps last candle of page1
    responses.add(responses.GET, f"{TESTNET_HOST}/api/v3/klines", json=page1, status=200)
    responses.add(responses.GET, f"{TESTNET_HOST}/api/v3/klines", json=page2, status=200)
    client = BinancePublicMarketDataClient(TESTNET_HOST)
    reference_time_ms = page2[-1][6] + 1
    result = fetch_historical_range(
        client, "BTCUSDT", INTERVAL, START, START + 100 * STEP,
        page_limit=3, reference_time_ms=reference_time_ms,
    )
    open_times = [c.open_time_ms for c in result.candles]
    assert open_times == sorted(set(open_times))  # no duplicates, still ordered
    assert len(responses.calls) == 2  # pagination stopped after the short page


# --- Gap detection, narrow-range retry, and confirmation. ---


def _candle_at(open_time_ms: int, close: float = 100.0) -> Candle:
    return Candle.from_binance_kline("BTCUSDT", INTERVAL, make_kline_row(open_time_ms, INTERVAL, close))


@responses.activate
def test_the_exact_reported_gap_is_detected_and_confirmed_when_not_recoverable():
    # Reproduces the exact failure from the bug report: candles surrounding
    # a single missing 4h candle, with the narrow retry finding nothing.
    candles = [
        _candle_at(REAL_GAP_PREVIOUS_OPEN_TIME_MS),
        _candle_at(REAL_GAP_NEXT_OPEN_TIME_MS),
    ]
    responses.add(responses.GET, f"{TESTNET_HOST}/api/v3/klines", json=[], status=200)  # narrow retry finds nothing
    client = BinancePublicMarketDataClient(TESTNET_HOST)
    result = confirm_gaps(client, "BTCUSDT", INTERVAL, candles, max_retries=1, sleep_fn=lambda s: None)

    assert len(result.candles) == 2  # both surrounding candles preserved, nothing fabricated
    assert result.candles[0].open_time_ms == REAL_GAP_PREVIOUS_OPEN_TIME_MS
    assert result.candles[1].open_time_ms == REAL_GAP_NEXT_OPEN_TIME_MS
    assert len(result.confirmed_gaps) == 1
    gap = result.confirmed_gaps[0]
    assert gap.previous_open_time_ms == REAL_GAP_PREVIOUS_OPEN_TIME_MS
    assert gap.next_open_time_ms == REAL_GAP_NEXT_OPEN_TIME_MS
    assert gap.expected_open_time_ms == REAL_GAP_PREVIOUS_OPEN_TIME_MS + STEP
    assert gap.missing_intervals == 1


@responses.activate
def test_retry_successfully_recovers_a_temporarily_missing_candle():
    # Same shape as the real gap, but this time a focused retry actually
    # finds the candle - proving a transient/pagination artifact is
    # distinguished from a genuine exchange-side gap before ever confirming one.
    candles = [
        _candle_at(REAL_GAP_PREVIOUS_OPEN_TIME_MS),
        _candle_at(REAL_GAP_NEXT_OPEN_TIME_MS),
    ]
    missing_row = make_kline_row(REAL_GAP_PREVIOUS_OPEN_TIME_MS + STEP, INTERVAL)
    responses.add(responses.GET, f"{TESTNET_HOST}/api/v3/klines", json=[missing_row], status=200)
    client = BinancePublicMarketDataClient(TESTNET_HOST)
    result = confirm_gaps(client, "BTCUSDT", INTERVAL, candles, max_retries=1, sleep_fn=lambda s: None)

    assert len(result.candles) == 3
    assert result.candles[1].open_time_ms == REAL_GAP_PREVIOUS_OPEN_TIME_MS + STEP
    assert result.confirmed_gaps == []  # fully recovered - no confirmed gap remains


@responses.activate
def test_retry_recovers_only_part_of_a_multi_candle_gap_leaving_a_smaller_confirmed_gap():
    candles = [_candle_at(START), _candle_at(START + 3 * STEP)]  # 2 missing intervals
    only_first_missing_row = make_kline_row(START + STEP, INTERVAL)
    responses.add(responses.GET, f"{TESTNET_HOST}/api/v3/klines", json=[only_first_missing_row], status=200)
    client = BinancePublicMarketDataClient(TESTNET_HOST)
    result = confirm_gaps(client, "BTCUSDT", INTERVAL, candles, max_retries=1, sleep_fn=lambda s: None)

    assert len(result.candles) == 3
    assert len(result.confirmed_gaps) == 1
    gap = result.confirmed_gaps[0]
    assert gap.previous_open_time_ms == START + STEP
    assert gap.next_open_time_ms == START + 3 * STEP
    assert gap.missing_intervals == 1


@responses.activate
def test_one_missing_interval_is_reported_with_missing_intervals_one():
    candles = [_candle_at(START), _candle_at(START + 2 * STEP)]
    responses.add(responses.GET, f"{TESTNET_HOST}/api/v3/klines", json=[], status=200)
    client = BinancePublicMarketDataClient(TESTNET_HOST)
    result = confirm_gaps(client, "BTCUSDT", INTERVAL, candles, max_retries=1, sleep_fn=lambda s: None)

    assert result.confirmed_gaps[0].missing_intervals == 1


@responses.activate
def test_several_consecutive_missing_intervals_are_reported_together():
    candles = [_candle_at(START), _candle_at(START + 5 * STEP)]  # 4 missing intervals
    responses.add(responses.GET, f"{TESTNET_HOST}/api/v3/klines", json=[], status=200)
    client = BinancePublicMarketDataClient(TESTNET_HOST)
    result = confirm_gaps(client, "BTCUSDT", INTERVAL, candles, max_retries=1, sleep_fn=lambda s: None)

    assert len(result.confirmed_gaps) == 1
    assert result.confirmed_gaps[0].missing_intervals == 4


@responses.activate
def test_gap_at_a_pagination_boundary_is_detected_and_confirmed():
    # Page 1 ends right before the gap; page 2 (a fresh request) starts
    # right after it - exactly the shape that caused the real bug, since
    # each page in isolation looks internally consistent.
    page1 = make_kline_series(START, INTERVAL, 3)  # START, START+STEP, START+2*STEP
    page2 = make_kline_series(START + 4 * STEP, INTERVAL, 2)  # skips START+3*STEP entirely
    responses.add(responses.GET, f"{TESTNET_HOST}/api/v3/klines", json=page1, status=200)
    responses.add(responses.GET, f"{TESTNET_HOST}/api/v3/klines", json=page2, status=200)
    responses.add(responses.GET, f"{TESTNET_HOST}/api/v3/klines", json=[], status=200)  # narrow retry: still missing
    client = BinancePublicMarketDataClient(TESTNET_HOST)
    reference_time_ms = page2[-1][6] + 1
    result = fetch_historical_range(
        # Requested end is exclusive - one full STEP past page2's own last
        # candle (START + 5*STEP) so that legitimate candle is actually
        # in range, distinct from testing the exclusive-end boundary itself
        # (see test_data_integrity_round3_audit.py / test_fetch_end_boundary.py).
        client, "BTCUSDT", INTERVAL, START, START + 6 * STEP,
        page_limit=3, reference_time_ms=reference_time_ms,
        max_retries=1, sleep_fn=lambda s: None,
    )
    assert len(result.candles) == 5  # 3 from page1 + 2 from page2, nothing fabricated for the gap
    assert len(result.confirmed_gaps) == 1
    assert result.confirmed_gaps[0].previous_open_time_ms == START + 2 * STEP
    assert result.confirmed_gaps[0].next_open_time_ms == START + 4 * STEP


@responses.activate
def test_gap_at_pagination_boundary_recovered_by_retry_leaves_no_confirmed_gap():
    page1 = make_kline_series(START, INTERVAL, 3)
    page2 = make_kline_series(START + 4 * STEP, INTERVAL, 2)
    recovered_row = make_kline_row(START + 3 * STEP, INTERVAL)
    responses.add(responses.GET, f"{TESTNET_HOST}/api/v3/klines", json=page1, status=200)
    responses.add(responses.GET, f"{TESTNET_HOST}/api/v3/klines", json=page2, status=200)
    responses.add(responses.GET, f"{TESTNET_HOST}/api/v3/klines", json=[recovered_row], status=200)
    client = BinancePublicMarketDataClient(TESTNET_HOST)
    reference_time_ms = page2[-1][6] + 1
    result = fetch_historical_range(
        # See the sibling test above for why this is 6*STEP, not 5*STEP.
        client, "BTCUSDT", INTERVAL, START, START + 6 * STEP,
        page_limit=3, reference_time_ms=reference_time_ms,
        max_retries=1, sleep_fn=lambda s: None,
    )
    assert len(result.candles) == 6  # 3 + 2 from the pages, plus the recovered candle
    assert result.confirmed_gaps == []


def test_duplicate_candles_are_still_rejected_not_silently_absorbed():
    from trading_agent.data.exceptions import DuplicateCandleError

    candles = [_candle_at(START), _candle_at(START)]
    client = BinancePublicMarketDataClient(TESTNET_HOST)
    try:
        confirm_gaps(client, "BTCUSDT", INTERVAL, candles, max_retries=1, sleep_fn=lambda s: None)
        raise AssertionError("expected DuplicateCandleError")
    except DuplicateCandleError:
        pass


def test_out_of_order_candles_are_still_rejected():
    from trading_agent.data.exceptions import OutOfOrderCandleError

    candles = [_candle_at(START + STEP), _candle_at(START)]
    client = BinancePublicMarketDataClient(TESTNET_HOST)
    try:
        confirm_gaps(client, "BTCUSDT", INTERVAL, candles, max_retries=1, sleep_fn=lambda s: None)
        raise AssertionError("expected OutOfOrderCandleError")
    except OutOfOrderCandleError:
        pass


@responses.activate
def test_no_fabricated_candle_ever_appears_in_the_result():
    candles = [_candle_at(REAL_GAP_PREVIOUS_OPEN_TIME_MS), _candle_at(REAL_GAP_NEXT_OPEN_TIME_MS)]
    responses.add(responses.GET, f"{TESTNET_HOST}/api/v3/klines", json=[], status=200)
    client = BinancePublicMarketDataClient(TESTNET_HOST)
    result = confirm_gaps(client, "BTCUSDT", INTERVAL, candles, max_retries=1, sleep_fn=lambda s: None)

    open_times = {c.open_time_ms for c in result.candles}
    assert open_times == {REAL_GAP_PREVIOUS_OPEN_TIME_MS, REAL_GAP_NEXT_OPEN_TIME_MS}
    missing_expected_time = REAL_GAP_PREVIOUS_OPEN_TIME_MS + STEP
    assert missing_expected_time not in open_times


def test_confirm_gaps_rejects_empty_input():
    from trading_agent.data.exceptions import EmptyDataError

    client = BinancePublicMarketDataClient(TESTNET_HOST)
    try:
        confirm_gaps(client, "BTCUSDT", INTERVAL, [], max_retries=1, sleep_fn=lambda s: None)
        raise AssertionError("expected EmptyDataError")
    except EmptyDataError:
        pass
