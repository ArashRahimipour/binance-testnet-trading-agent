"""Resolving uncertain order state after a timeout, before ever retrying.

On a timeout (or any ambiguous network failure) after submitting an order,
the caller must NOT blindly resubmit. It must first ask Binance whether the
order actually exists, and only retry (reusing the same client order id)
if Binance confirms it does not.
"""

from __future__ import annotations

from enum import Enum

from trading_agent.execution.testnet_adapter import (
    BinanceApiError,
    OrderResult,
    TestnetBrokerAdapter,
)

# https://github.com/binance/binance-spot-api-docs - "Order does not exist" error code.
ORDER_DOES_NOT_EXIST_CODE = -2013


class ReconciledStatus(str, Enum):
    CONFIRMED_TERMINAL = "confirmed_terminal"  # FILLED, CANCELED, REJECTED, EXPIRED
    CONFIRMED_OPEN = "confirmed_open"  # NEW or PARTIALLY_FILLED
    NOT_FOUND = "not_found"  # Binance has never heard of this client order id
    UNKNOWN = "unknown"  # any other error - do NOT assume anything


_TERMINAL_STATUSES = {"FILLED", "CANCELED", "REJECTED", "EXPIRED"}
_OPEN_STATUSES = {"NEW", "PARTIALLY_FILLED"}


def reconcile_order(adapter: TestnetBrokerAdapter, symbol: str, client_order_id: str) -> tuple[ReconciledStatus, OrderResult | None]:
    try:
        order = adapter.get_order(symbol, client_order_id)
    except BinanceApiError as exc:
        if exc.code == ORDER_DOES_NOT_EXIST_CODE:
            return ReconciledStatus.NOT_FOUND, None
        return ReconciledStatus.UNKNOWN, None

    if order.status in _TERMINAL_STATUSES:
        return ReconciledStatus.CONFIRMED_TERMINAL, order
    if order.status in _OPEN_STATUSES:
        return ReconciledStatus.CONFIRMED_OPEN, order
    return ReconciledStatus.UNKNOWN, order


def safe_to_retry(status: ReconciledStatus) -> bool:
    """Only ever retry when we have positive confirmation the order never happened."""
    return status is ReconciledStatus.NOT_FOUND
