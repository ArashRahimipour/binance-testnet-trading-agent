import responses

from trading_agent.execution.reconciliation import ReconciledStatus, reconcile_order, safe_to_retry
from trading_agent.execution.testnet_adapter import TESTNET_HOST, TestnetBrokerAdapter


def _adapter() -> TestnetBrokerAdapter:
    return TestnetBrokerAdapter(api_key="k", api_secret="s")


@responses.activate
def test_order_not_found_is_safe_to_retry():
    responses.add(
        responses.GET,
        f"{TESTNET_HOST}/api/v3/order",
        json={"code": -2013, "msg": "Order does not exist."},
        status=400,
    )
    status, order = reconcile_order(_adapter(), "BTCUSDT", "ta-missing")
    assert status == ReconciledStatus.NOT_FOUND
    assert order is None
    assert safe_to_retry(status) is True


@responses.activate
def test_confirmed_filled_is_not_retried():
    responses.add(
        responses.GET,
        f"{TESTNET_HOST}/api/v3/order",
        json={
            "orderId": 1,
            "clientOrderId": "ta-abc",
            "status": "FILLED",
            "executedQty": "0.001",
            "cummulativeQuoteQty": "50.0",
            "transactTime": 1700000000000,
        },
        status=200,
    )
    status, order = reconcile_order(_adapter(), "BTCUSDT", "ta-abc")
    assert status == ReconciledStatus.CONFIRMED_TERMINAL
    assert order is not None
    assert safe_to_retry(status) is False


@responses.activate
def test_confirmed_open_is_not_retried():
    responses.add(
        responses.GET,
        f"{TESTNET_HOST}/api/v3/order",
        json={
            "orderId": 1,
            "clientOrderId": "ta-abc",
            "status": "NEW",
            "executedQty": "0",
            "cummulativeQuoteQty": "0",
            "transactTime": 1700000000000,
        },
        status=200,
    )
    status, _ = reconcile_order(_adapter(), "BTCUSDT", "ta-abc")
    assert status == ReconciledStatus.CONFIRMED_OPEN
    assert safe_to_retry(status) is False


@responses.activate
def test_unknown_api_error_is_not_retried():
    responses.add(
        responses.GET,
        f"{TESTNET_HOST}/api/v3/order",
        json={"code": -1021, "msg": "Timestamp for this request is outside of the recvWindow."},
        status=400,
    )
    status, order = reconcile_order(_adapter(), "BTCUSDT", "ta-abc")
    assert status == ReconciledStatus.UNKNOWN
    assert order is None
    assert safe_to_retry(status) is False
