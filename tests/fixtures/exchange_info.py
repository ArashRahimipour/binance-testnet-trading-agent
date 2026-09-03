"""A representative /api/v3/exchangeInfo response for BTCUSDT, trimmed to
just the fields the parser reads."""

from __future__ import annotations


def make_exchange_info(
    symbol: str = "BTCUSDT",
    tick_size: str = "0.01000000",
    step_size: str = "0.00001000",
    min_qty: str = "0.00001000",
    max_qty: str = "9000.00000000",
    min_notional: str = "5.00000000",
) -> dict:
    return {
        "symbols": [
            {
                "symbol": symbol,
                "filters": [
                    {
                        "filterType": "PRICE_FILTER",
                        "minPrice": "0.01000000",
                        "maxPrice": "1000000.00000000",
                        "tickSize": tick_size,
                    },
                    {
                        "filterType": "LOT_SIZE",
                        "minQty": min_qty,
                        "maxQty": max_qty,
                        "stepSize": step_size,
                    },
                    {
                        "filterType": "NOTIONAL",
                        "minNotional": min_notional,
                        "applyMinToMarket": True,
                        "maxNotional": "9000000.00000000",
                        "applyMaxToMarket": False,
                        "avgPriceMins": 5,
                    },
                ],
            }
        ]
    }
