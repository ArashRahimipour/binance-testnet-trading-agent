import pytest
import responses

from tests.fixtures.klines import make_kline_series
from trading_agent.data.market_data_public import (
    PRODUCTION_MARKET_DATA_HOST,
    TESTNET_HOST,
    BinancePublicMarketDataClient,
    DisallowedHostError,
)


def test_rejects_arbitrary_host():
    with pytest.raises(DisallowedHostError):
        BinancePublicMarketDataClient("https://evil.example.com")


def test_rejects_production_trading_host_variants():
    for host in ["https://api1.binance.com", "https://api-gcp.binance.com", "http://api.binance.com"]:
        with pytest.raises(DisallowedHostError):
            BinancePublicMarketDataClient(host)


def test_allows_production_market_data_host():
    client = BinancePublicMarketDataClient(PRODUCTION_MARKET_DATA_HOST)
    assert client.base_url == PRODUCTION_MARKET_DATA_HOST


def test_allows_testnet_host():
    client = BinancePublicMarketDataClient(TESTNET_HOST)
    assert client.base_url == TESTNET_HOST


def test_client_has_no_order_methods():
    client = BinancePublicMarketDataClient(TESTNET_HOST)
    forbidden = {"place_order", "new_order", "cancel_order", "create_order"}
    assert not (forbidden & set(dir(client)))


@responses.activate
def test_get_server_time():
    responses.add(
        responses.GET,
        f"{TESTNET_HOST}/api/v3/time",
        json={"serverTime": 1700000000000},
        status=200,
    )
    client = BinancePublicMarketDataClient(TESTNET_HOST)
    assert client.get_server_time_ms() == 1700000000000


@responses.activate
def test_get_klines_parses_rows():
    rows = make_kline_series(1700000000000, "4h", 3)
    responses.add(
        responses.GET,
        f"{TESTNET_HOST}/api/v3/klines",
        json=rows,
        status=200,
    )
    client = BinancePublicMarketDataClient(TESTNET_HOST)
    candles = client.get_klines("BTCUSDT", "4h")
    assert len(candles) == 3
    assert candles[0].open_time_ms == 1700000000000
