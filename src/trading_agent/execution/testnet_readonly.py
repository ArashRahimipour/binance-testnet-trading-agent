"""A Binance Spot Testnet client capable ONLY of read operations.

This class has no `place_market_order` method, no method that issues
POST/PUT/PATCH/DELETE, and no constructor parameter that could redirect
its requests to a production host. Its single internal request helper
(`_get`) is hard-wired to `requests.Session.get` - there is no method
parameter anywhere that could turn a call into a POST, so the "GET only"
guarantee holds structurally, not just by convention.

Deliberately does NOT import `execution/testnet_adapter.py` - the ONE
class in this codebase that can place, cancel, or modify an order. This
file, and everything built on it (`execution/testnet_health.py`), has
zero reference to `place_market_order` anywhere in its source or import
graph. See `tests/unit/test_testnet_health.py` for the source-level proof
and `tests/integration/test_no_production_endpoints.py` for the existing
project-wide production-host scan, which also covers this file.

Both this class and `execution/testnet_adapter.py::TestnetBrokerAdapter`
share only pure, side-effect-free primitives from
`execution/binance_signing.py` (HMAC signing, clock-offset math, the
common `BinanceApiError`/`ClockDriftError` types) - neither depends on
the other.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal

import requests

from trading_agent.execution.binance_signing import (
    DEFAULT_MAX_CLOCK_DRIFT_MS,
    TESTNET_HOST,
    BinanceApiError,
    auth_headers,
    compute_clock_offset,
    safe_json,
    sign_params,
)

__all__ = ["OpenOrderInfo", "ReadOnlyTestnetClient"]


@dataclass(frozen=True, slots=True)
class OpenOrderInfo:
    order_id: int
    client_order_id: str
    side: str
    status: str
    price: Decimal
    orig_qty: Decimal


class ReadOnlyTestnetClient:
    """Strictly read-only Testnet client: public market data plus signed
    GET endpoints only. There is no code path in this class capable of
    placing, canceling, or modifying an order."""

    BASE_URL = TESTNET_HOST

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        recv_window_ms: int = 5000,
        timeout_seconds: float = 10.0,
        max_clock_drift_ms: int = DEFAULT_MAX_CLOCK_DRIFT_MS,
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._recv_window_ms = recv_window_ms
        self._timeout = timeout_seconds
        self._max_clock_drift_ms = max_clock_drift_ms
        self._clock_offset_ms = 0
        self._session = requests.Session()

    def sync_time(self, server_time_ms: int) -> int:
        """Compute and store `server_time - local_time`, failing closed on
        excessive drift. Identical contract to `TestnetBrokerAdapter.
        sync_time` - see that method's docstring - but implemented
        independently against the shared pure helper, not by delegating
        to the order-capable class."""
        local_time_ms = int(time.time() * 1000)
        offset_ms = compute_clock_offset(server_time_ms, local_time_ms, self._max_clock_drift_ms)
        self._clock_offset_ms = offset_ms
        return offset_ms

    def _get(self, path: str, params: dict) -> dict | list:
        """The ONLY request path in this class. Always an HTTP GET -
        there is no `method` parameter to override that."""
        signed_params = sign_params(self._api_secret, params, self._clock_offset_ms, self._recv_window_ms)
        url = f"{self.BASE_URL}{path}"
        response = self._session.get(
            url, params=signed_params, headers=auth_headers(self._api_key), timeout=self._timeout
        )
        if not response.ok:
            body = safe_json(response)
            raise BinanceApiError(response.status_code, body.get("code"), body.get("msg", response.text))
        return response.json()

    def get_account_balances(self) -> dict[str, tuple[Decimal, Decimal]]:
        """Returns {asset: (free, locked)}. Signed GET /api/v3/account."""
        raw = self._get("/api/v3/account", {})
        assert isinstance(raw, dict)
        return {b["asset"]: (Decimal(b["free"]), Decimal(b["locked"])) for b in raw.get("balances", [])}

    def get_open_orders(self, symbol: str) -> list[OpenOrderInfo]:
        """Signed GET /api/v3/openOrders. Read-only: reports state, never changes it."""
        raw = self._get("/api/v3/openOrders", {"symbol": symbol})
        assert isinstance(raw, list)
        return [
            OpenOrderInfo(
                order_id=int(o["orderId"]),
                client_order_id=o.get("clientOrderId", ""),
                side=o["side"],
                status=o["status"],
                price=Decimal(str(o["price"])),
                orig_qty=Decimal(str(o["origQty"])),
            )
            for o in raw
        ]
