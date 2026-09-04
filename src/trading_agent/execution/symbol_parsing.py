"""Splitting a Binance symbol (e.g. "BTCUSDT") into base/quote assets.

Pure string logic, no I/O, no order capability - shared by
`execution/live_runner.py` and `execution/testnet_health.py` so the
health-check module never needs to import anything from
`live_runner.py` (which does hold a reference to `place_market_order`).
"""

from __future__ import annotations

_KNOWN_QUOTE_ASSETS = ("USDT", "USDC", "BUSD", "BTC", "ETH")


def quote_asset(symbol: str) -> str:
    for suffix in _KNOWN_QUOTE_ASSETS:
        if symbol.endswith(suffix):
            return suffix
    raise ValueError(f"cannot determine quote asset for symbol {symbol!r}")


def base_asset(symbol: str) -> str:
    return symbol[: -len(quote_asset(symbol))]
