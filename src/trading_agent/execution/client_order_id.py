"""Deterministic, idempotent client order IDs.

The same (symbol, side, signal candle close time) always produces the same
ID. That means:
  - A retry after a timeout reuses the *same* ID, so if the original
    request actually reached Binance, the retry is rejected as a duplicate
    active client order ID instead of creating a second real order.
  - Two different signals (different candles) never collide.

Binance limits `newClientOrderId` to 36 characters of a restricted
character set; "ta-" plus 24 hex characters (27 total) stays comfortably
inside that limit.
"""

from __future__ import annotations

import hashlib

MAX_LENGTH = 36


def generate_client_order_id(symbol: str, side: str, signal_candle_close_time_ms: int) -> str:
    raw = f"{symbol}|{side.upper()}|{signal_candle_close_time_ms}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"ta-{digest}"  # always 27 chars, well under MAX_LENGTH
