"""Shared, side-effect-free signing and clock-offset primitives for
Binance Spot Testnet's private (signed) endpoints.

Deliberately contains NO order-placing capability and makes NO HTTP calls
itself: it is pure computation only (HMAC signing, clock-offset math,
constants, and the shared API-error type), safe to import from both the
order-capable `execution/testnet_adapter.py` and the strictly read-only
`execution/testnet_readonly.py` without either depending on the other -
see `testnet_readonly.py`'s module docstring for why that separation
matters (it is what lets `execution/testnet_health.py` prove it has no
reference to `place_market_order` anywhere in its import graph).
"""

from __future__ import annotations

import hashlib
import hmac
import time
import urllib.parse

import requests

TESTNET_HOST = "https://testnet.binance.vision"

# Generous relative to Binance's default recvWindow (5000ms) and max
# (60000ms), but still small enough to catch a genuinely misconfigured
# system clock before it causes a confusing -1021 error mid-trade.
DEFAULT_MAX_CLOCK_DRIFT_MS = 1000


class ClockDriftError(Exception):
    """Raised when the local clock disagrees with Binance's server time by
    more than the configured tolerance. Fail closed rather than sign
    requests with a timestamp that might fall outside recvWindow."""


class BinanceApiError(Exception):
    def __init__(self, status_code: int, code: int | None, message: str) -> None:
        super().__init__(f"Binance API error {code}: {message} (HTTP {status_code})")
        self.status_code = status_code
        self.code = code
        self.message = message


def compute_clock_offset(server_time_ms: int, local_time_ms: int, max_drift_ms: int) -> int:
    """Returns `server_time_ms - local_time_ms`, failing closed if the
    magnitude exceeds `max_drift_ms`. Pure arithmetic on integers already
    in hand - never touches a secret or a signed request."""
    offset_ms = server_time_ms - local_time_ms
    if abs(offset_ms) > max_drift_ms:
        raise ClockDriftError(
            f"local clock drift {offset_ms}ms exceeds max allowed {max_drift_ms}ms - refusing to sign requests"
        )
    return offset_ms


def sign_params(api_secret: str, params: dict, clock_offset_ms: int, recv_window_ms: int) -> dict:
    """Returns `params` plus `timestamp`/`recvWindow`/`signature`, per
    Binance's documented HMAC-SHA256 signing scheme. The signature is
    computed over the exact query string being sent, using the API secret
    as the HMAC key - the secret itself never appears in the returned
    dict, only the resulting signature digest."""
    signed = dict(params)
    signed.setdefault("timestamp", int(time.time() * 1000) + clock_offset_ms)
    signed.setdefault("recvWindow", recv_window_ms)
    query_string = urllib.parse.urlencode(signed)
    signature = hmac.new(
        api_secret.encode("utf-8"), query_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    signed["signature"] = signature
    return signed


def auth_headers(api_key: str) -> dict:
    """The API key travels only in this header, never as a query
    parameter and never logged (see logging_setup.SecretRedactionFilter)."""
    return {"X-MBX-APIKEY": api_key}


def safe_json(response: requests.Response) -> dict:
    try:
        return response.json()
    except ValueError:
        return {}
