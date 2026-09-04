import hashlib
import hmac
import inspect
import time
import urllib.parse
from decimal import Decimal

import pytest
import responses

from trading_agent.execution.testnet_adapter import (
    TESTNET_HOST,
    BinanceApiError,
    ClockDriftError,
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
def test_get_account_balances_parses_free_and_locked():
    responses.add(
        responses.GET,
        f"{TESTNET_HOST}/api/v3/account",
        json={"balances": [{"asset": "USDT", "free": "50.00000000", "locked": "5.00000000"}]},
        status=200,
    )
    adapter = TestnetBrokerAdapter(api_key="k", api_secret="s")
    balances = adapter.get_account_balances()
    free, locked = balances["USDT"]
    assert free == Decimal("50.00000000")
    assert locked == Decimal("5.00000000")


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


@responses.activate
def test_timestamp_outside_recv_window_error_propagates():
    responses.add(
        responses.GET,
        f"{TESTNET_HOST}/api/v3/order",
        json={"code": -1021, "msg": "Timestamp for this request is outside of the recvWindow."},
        status=400,
    )
    adapter = TestnetBrokerAdapter(api_key="k", api_secret="s")
    with pytest.raises(BinanceApiError) as exc_info:
        adapter.get_order("BTCUSDT", "ta-x")
    assert exc_info.value.code == -1021


def test_sync_time_computes_positive_offset_when_server_ahead():
    adapter = TestnetBrokerAdapter(api_key="k", api_secret="s")
    local_ms = int(time.time() * 1000)
    offset = adapter.sync_time(server_time_ms=local_ms + 500)
    assert offset > 0
    assert offset == pytest.approx(500, abs=50)


def test_sync_time_computes_negative_offset_when_server_behind():
    adapter = TestnetBrokerAdapter(api_key="k", api_secret="s")
    local_ms = int(time.time() * 1000)
    offset = adapter.sync_time(server_time_ms=local_ms - 500)
    assert offset < 0
    assert offset == pytest.approx(-500, abs=50)


def test_sync_time_raises_on_excessive_drift():
    adapter = TestnetBrokerAdapter(api_key="k", api_secret="s", max_clock_drift_ms=1000)
    local_ms = int(time.time() * 1000)
    with pytest.raises(ClockDriftError):
        adapter.sync_time(server_time_ms=local_ms + 50_000)


def test_sync_time_within_tolerance_is_applied_to_signing():
    adapter = TestnetBrokerAdapter(api_key="k", api_secret="s", max_clock_drift_ms=1000)
    local_ms = int(time.time() * 1000)
    adapter.sync_time(server_time_ms=local_ms + 300)
    signed = adapter._sign({"symbol": "BTCUSDT"})
    # signed timestamp should reflect local time + the learned offset, not raw local time.
    assert signed["timestamp"] == pytest.approx(local_ms + 300, abs=100)


def test_unsynced_adapter_defaults_to_zero_offset():
    adapter = TestnetBrokerAdapter(api_key="k", api_secret="s")
    local_ms = int(time.time() * 1000)
    signed = adapter._sign({"symbol": "BTCUSDT"})
    assert signed["timestamp"] == pytest.approx(local_ms, abs=100)
