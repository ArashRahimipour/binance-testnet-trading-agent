import time
from decimal import Decimal

import pytest
import responses

from trading_agent.execution.binance_signing import TESTNET_HOST, BinanceApiError, ClockDriftError
from trading_agent.execution.testnet_readonly import ReadOnlyTestnetClient


def _client() -> ReadOnlyTestnetClient:
    return ReadOnlyTestnetClient(api_key="fake-key", api_secret="fake-secret")


def test_base_url_is_the_testnet_host():
    assert ReadOnlyTestnetClient.BASE_URL == TESTNET_HOST == "https://testnet.binance.vision"


def test_has_no_order_placing_method_at_all():
    # Structural guarantee: the capability simply does not exist on this
    # class, not merely "unused" - see the module's own docstring.
    assert not hasattr(ReadOnlyTestnetClient, "place_market_order")
    assert not any("place" in name.lower() for name in dir(ReadOnlyTestnetClient))
    assert not any("cancel" in name.lower() for name in dir(ReadOnlyTestnetClient))


def test_sync_time_computes_offset_and_raises_on_excessive_drift():
    client = _client()
    local_ms = int(time.time() * 1000)
    offset = client.sync_time(server_time_ms=local_ms + 500)
    assert offset == pytest.approx(500, abs=50)

    with pytest.raises(ClockDriftError):
        client.sync_time(server_time_ms=local_ms + 50_000)


@responses.activate
def test_get_account_balances_issues_a_single_get_request():
    responses.add(
        responses.GET,
        f"{TESTNET_HOST}/api/v3/account",
        json={"balances": [
            {"asset": "USDT", "free": "50", "locked": "0"},
            {"asset": "BTC", "free": "0.001", "locked": "0.0005"},
        ]},
        status=200,
    )
    client = _client()
    balances = client.get_account_balances()
    assert balances["USDT"] == (Decimal(50), Decimal(0))
    assert balances["BTC"] == (Decimal("0.001"), Decimal("0.0005"))
    assert len(responses.calls) == 1
    assert responses.calls[0].request.method == "GET"


@responses.activate
def test_get_open_orders_issues_a_single_get_request():
    responses.add(
        responses.GET,
        f"{TESTNET_HOST}/api/v3/openOrders",
        json=[
            {
                "symbol": "BTCUSDT", "orderId": 1, "clientOrderId": "ta-1",
                "price": "50000.00", "origQty": "0.001", "status": "NEW", "side": "SELL",
            }
        ],
        status=200,
    )
    client = _client()
    orders = client.get_open_orders("BTCUSDT")
    assert len(orders) == 1
    assert orders[0].order_id == 1
    assert orders[0].side == "SELL"
    assert orders[0].status == "NEW"
    assert len(responses.calls) == 1
    assert responses.calls[0].request.method == "GET"


@responses.activate
def test_invalid_credentials_raise_binance_api_error_without_leaking_secret():
    responses.add(
        responses.GET,
        f"{TESTNET_HOST}/api/v3/account",
        json={"code": -2015, "msg": "Invalid API-key, IP, or permissions for action."},
        status=401,
    )
    client = ReadOnlyTestnetClient(api_key="fake-key", api_secret="super-secret-value")
    with pytest.raises(BinanceApiError) as exc_info:
        client.get_account_balances()
    assert "super-secret-value" not in str(exc_info.value)
    assert exc_info.value.code == -2015


@responses.activate
def test_no_request_is_ever_a_post_put_patch_or_delete():
    responses.add(responses.GET, f"{TESTNET_HOST}/api/v3/account", json={"balances": []}, status=200)
    responses.add(responses.GET, f"{TESTNET_HOST}/api/v3/openOrders", json=[], status=200)
    client = _client()
    client.get_account_balances()
    client.get_open_orders("BTCUSDT")
    assert len(responses.calls) == 2
    assert all(call.request.method == "GET" for call in responses.calls)
