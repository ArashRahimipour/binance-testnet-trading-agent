"""The only class in this codebase that can place, query, or cancel orders.

`BASE_URL` is a hard-coded class constant equal to the Binance Spot Testnet
host. It is not a constructor parameter, not read from config, and not
overridable by an environment variable - there is no code path, anywhere,
that can point this class at any production trading host. See
tests/integration/test_no_production_endpoints.py for a source-level proof
of this property.

Signing follows the documented HMAC-SHA256 scheme: every private endpoint
requires `timestamp`, an optional `recvWindow`, and a `signature` computed
over the exact query string being sent, using the API secret as the HMAC
key. The API key is sent via the `X-MBX-APIKEY` header, never as a query
parameter, and is never logged (see logging_setup.SecretRedactionFilter).

`timestamp` is derived from the local clock plus a bounded offset learned
via `sync_time()`, never from the raw local clock alone - see the module
docstring note on clock drift below `ClockDriftError`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

import requests

from trading_agent.execution.binance_signing import (
    DEFAULT_MAX_CLOCK_DRIFT_MS,
    TESTNET_HOST,
    BinanceApiError,
    ClockDriftError,
    auth_headers,
    compute_clock_offset,
    safe_json,
    sign_params,
)

__all__ = [
    "DEFAULT_MAX_CLOCK_DRIFT_MS",
    "TESTNET_HOST",
    "BinanceApiError",
    "ClockDriftError",
    "Fill",
    "OrderResult",
    "TestnetBrokerAdapter",
]


@dataclass(frozen=True, slots=True)
class Fill:
    price: Decimal
    qty: Decimal
    commission: Decimal
    commission_asset: str


@dataclass(frozen=True, slots=True)
class OrderResult:
    order_id: int
    client_order_id: str
    status: str
    executed_qty: Decimal
    cumulative_quote_qty: Decimal
    transact_time_ms: int
    fills: list[Fill] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


class TestnetBrokerAdapter:
    """Places and queries orders on the Binance Spot Testnet ONLY.

    There is intentionally no `base_url` (or similar) constructor
    parameter. If a future adapter for a different venue is needed, it
    must be a distinct class implementing the same interface, not a
    parameterized version of this one.
    """

    __test__ = False  # not a pytest test class despite the name prefix

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
        """Compute and store `server_time - local_time`, failing closed on excessive drift.

        Callers should pass a `server_time_ms` fetched immediately before
        (or after) this call, from the same exchange clock that will
        validate the signed requests - `live_runner.py` reuses the server
        time it already fetched from the public market-data host, since
        Testnet and its public endpoints share one clock.
        """
        local_time_ms = int(time.time() * 1000)
        offset_ms = compute_clock_offset(server_time_ms, local_time_ms, self._max_clock_drift_ms)
        self._clock_offset_ms = offset_ms
        return offset_ms

    def _headers(self) -> dict:
        return auth_headers(self._api_key)

    def _sign(self, params: dict) -> dict:
        return sign_params(self._api_secret, params, self._clock_offset_ms, self._recv_window_ms)

    def _request(self, method: Literal["GET", "POST", "DELETE"], path: str, params: dict) -> dict:
        signed_params = self._sign(params)
        url = f"{self.BASE_URL}{path}"
        response = self._session.request(
            method, url, params=signed_params, headers=self._headers(), timeout=self._timeout
        )
        if not response.ok:
            body = self._safe_json(response)
            raise BinanceApiError(response.status_code, body.get("code"), body.get("msg", response.text))
        return response.json()

    @staticmethod
    def _safe_json(response: requests.Response) -> dict:
        return safe_json(response)

    def place_market_order(
        self, symbol: str, side: Literal["BUY", "SELL"], quantity: Decimal, client_order_id: str
    ) -> OrderResult:
        raw = self._request(
            "POST",
            "/api/v3/order",
            {
                "symbol": symbol,
                "side": side,
                "type": "MARKET",
                "quantity": str(quantity),
                "newClientOrderId": client_order_id,
            },
        )
        return self._parse_order(raw)

    def get_order(self, symbol: str, client_order_id: str) -> OrderResult:
        raw = self._request(
            "GET",
            "/api/v3/order",
            {"symbol": symbol, "origClientOrderId": client_order_id},
        )
        return self._parse_order(raw)

    def get_open_orders(self, symbol: str) -> list[OrderResult]:
        raw = self._request("GET", "/api/v3/openOrders", {"symbol": symbol})
        return [self._parse_order(order) for order in raw]

    def get_account_balances(self) -> dict[str, tuple[Decimal, Decimal]]:
        """Returns {asset: (free, locked)} - both matter for reconciliation (Findings 2/5)."""
        raw = self._request("GET", "/api/v3/account", {})
        return {b["asset"]: (Decimal(b["free"]), Decimal(b["locked"])) for b in raw.get("balances", [])}

    @staticmethod
    def _parse_order(raw: dict) -> OrderResult:
        fills = [
            Fill(
                price=Decimal(str(f["price"])),
                qty=Decimal(str(f["qty"])),
                commission=Decimal(str(f["commission"])),
                commission_asset=f["commissionAsset"],
            )
            for f in raw.get("fills", [])
        ]
        return OrderResult(
            order_id=int(raw["orderId"]),
            client_order_id=raw.get("clientOrderId", raw.get("origClientOrderId", "")),
            status=raw["status"],
            executed_qty=Decimal(str(raw.get("executedQty", "0"))),
            cumulative_quote_qty=Decimal(str(raw.get("cummulativeQuoteQty", "0"))),
            transact_time_ms=int(raw.get("transactTime", raw.get("time", 0))),
            fills=fills,
            raw=raw,
        )
