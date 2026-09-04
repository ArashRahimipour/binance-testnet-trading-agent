import responses

from tests.fixtures.klines import make_kline_series
from trading_agent.data.historical_fetch import fetch_historical_range
from trading_agent.data.market_data_public import TESTNET_HOST, BinancePublicMarketDataClient
from trading_agent.data.models import interval_to_ms

INTERVAL = "4h"
STEP = interval_to_ms(INTERVAL)
START = 1_700_000_000_000


def _sleeps() -> list[float]:
    return []


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
    assert len(result) == 5


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
    assert len(result) == 5
    assert result[0].open_time_ms == START
    assert result[-1].open_time_ms == START + 4 * STEP


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
    assert len(result) == 3
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
    assert len(result) == 4


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
    assert len(result) == 2
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
    open_times = [c.open_time_ms for c in result]
    assert open_times == sorted(set(open_times))  # no duplicates, still ordered
    assert len(responses.calls) == 2  # pagination stopped after the short page
