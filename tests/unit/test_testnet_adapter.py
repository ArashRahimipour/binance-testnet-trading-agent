import hashlib
import hmac
import inspect
import urllib.parse
from decimal import Decimal

import pytest
import responses

from trading_agent.execution.testnet_adapter import (
    TESTNET_HOST,
    BinanceApiError,
    TestnetBrokerAdapter,
)


def test_base_url_is_hardcoded_to_testnet():
    assert TestnetBrokerAdapter.BASE_URL == TESTNET_HOST == "https://testnet.binance.vision"


def test_constructor_has_no_base_url_parameter():
    signature = inspect.signature(TestnetBrokerAdapter.__init__)
    assert "base_url" not in signature.parameters
    assert "host" not in signature.parameters
    assert "endpoint" not in signature.parameters


def test_api_key_sent_only_as_header_never_as_query_param():
    adapter = TestnetBrokerAdapter(api_key="fake-key", api_secret="fake-secret")
    signed = adapter._sign({"symbol": "BTCUSDT"})
    assert "apiKey" not in signed
    assert adapter._headers() == {"X-MBX-APIKEY": "fake-key"}


def test_signature_matches_independent_hmac_computation():
    adapter = TestnetBrokerAdapter(api_key="fake-key", api_secret="fake-secret")
    signed = adapter._sign({"symbol": "BTCUSDT", "timestamp": 1700000000000, "recvWindow": 5000})
    signature = signed.pop("signature")
    expected_query = urllib.parse.urlencode(signed)
    expected_signature = hmac.new(
        b"fake-secret", expected_query.encode(), hashlib.sha256
    ).hexdigest()
    assert signature == expected_signature


@responses.activate
def test_place_market_order_sends_client_order_id():
    responses.add(
        responses.POST,
        f"{TESTNET_HOST}/api/v3/order",
        json={
            "orderId": 1,
            "clientOrderId": "ta-abc123",
            "status": "FILLED",
            "executedQty": "0.001",
            "cummulativeQuoteQty": "50.0",
            "transactTime": 1700000000000,
        },
        status=200,
    )
    adapter = TestnetBrokerAdapter(api_key="k", api_secret="s")
    result = adapter.place_market_order("BTCUSDT", "BUY", Decimal("0.001"), "ta-abc123")
    assert result.status == "FILLED"
    assert result.client_order_id == "ta-abc123"
    sent_params = urllib.parse.parse_qs(urllib.parse.urlparse(responses.calls[0].request.url).query)
    assert sent_params["newClientOrderId"] == ["ta-abc123"]


@responses.activate
def test_get_account_balances_parses_free_balances():
    responses.add(
        responses.GET,
        f"{TESTNET_HOST}/api/v3/account",
        json={"balances": [{"asset": "USDT", "free": "50.00000000", "locked": "0"}]},
        status=200,
    )
    adapter = TestnetBrokerAdapter(api_key="k", api_secret="s")
    balances = adapter.get_account_balances()
    assert balances["USDT"] == Decimal("50.00000000")


@responses.activate
def test_api_error_raises_with_code():
    responses.add(
        responses.GET,
        f"{TESTNET_HOST}/api/v3/order",
        json={"code": -2013, "msg": "Order does not exist."},
        status=400,
    )
    adapter = TestnetBrokerAdapter(api_key="k", api_secret="s")
    with pytest.raises(BinanceApiError) as exc_info:
        adapter.get_order("BTCUSDT", "ta-missing")
    assert exc_info.value.code == -2013
