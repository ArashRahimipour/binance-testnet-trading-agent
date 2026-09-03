import pytest
import responses

from tests.fixtures.klines import make_kline_series
from trading_agent.data.exceptions import EmptyDataError
from trading_agent.data.ingestion import fetch_completed_candles, require_non_empty
from trading_agent.data.market_data_public import TESTNET_HOST, BinancePublicMarketDataClient
from trading_agent.data.models import interval_to_ms


@responses.activate
def test_incomplete_last_candle_is_excluded():
    interval = "4h"
    step = interval_to_ms(interval)
    start = 1700000000000
    rows = make_kline_series(start, interval, 5)
    # Server time falls inside the 5th (last) candle's window -> it must be excluded.
    server_time = rows[-1][0] + step // 2

    responses.add(
        responses.GET,
        f"{TESTNET_HOST}/api/v3/klines",
        json=rows,
        status=200,
    )
    client = BinancePublicMarketDataClient(TESTNET_HOST)
    candles = fetch_completed_candles(
        client, "BTCUSDT", interval, reference_time_ms=server_time
    )
    assert len(candles) == 4
    assert all(c.close_time_ms < server_time for c in candles)


@responses.activate
def test_all_candles_completed_when_reference_after_all_close_times():
    interval = "4h"
    rows = make_kline_series(1700000000000, interval, 3)
    server_time = rows[-1][6] + 1  # 1ms after the last candle's close_time

    responses.add(responses.GET, f"{TESTNET_HOST}/api/v3/klines", json=rows, status=200)
    client = BinancePublicMarketDataClient(TESTNET_HOST)
    candles = fetch_completed_candles(client, "BTCUSDT", interval, reference_time_ms=server_time)
    assert len(candles) == 3


@responses.activate
def test_fetch_uses_server_time_when_reference_not_given():
    interval = "4h"
    rows = make_kline_series(1700000000000, interval, 2)
    responses.add(
        responses.GET,
        f"{TESTNET_HOST}/api/v3/time",
        json={"serverTime": rows[-1][6] + 1},
        status=200,
    )
    responses.add(responses.GET, f"{TESTNET_HOST}/api/v3/klines", json=rows, status=200)
    client = BinancePublicMarketDataClient(TESTNET_HOST)
    candles = fetch_completed_candles(client, "BTCUSDT", interval)
    assert len(candles) == 2


def test_require_non_empty_raises_on_empty_list():
    with pytest.raises(EmptyDataError):
        require_non_empty([])
