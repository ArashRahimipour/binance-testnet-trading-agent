"""Read-only Binance market-data client.

This class is deliberately incapable of placing, canceling, or querying
orders - it has no such methods, no API key, and no signing logic at all.
It is restricted to a small allowlist of hosts: the production public
market-data host (used only to fetch historical candles for backtesting -
no key, no trading capability, per the approved backtest-data-source
decision) and the Spot Testnet host (used for indicative current-market
data while running in testnet mode). Order placement lives in a completely
separate, testnet-only class (see execution/testnet_adapter.py, Phase 5).
"""

from __future__ import annotations

import requests

from trading_agent.data.models import Candle

PRODUCTION_MARKET_DATA_HOST = "https://api.binance.com"
TESTNET_HOST = "https://testnet.binance.vision"

ALLOWED_HOSTS = {PRODUCTION_MARKET_DATA_HOST, TESTNET_HOST}


class DisallowedHostError(ValueError):
    pass


class BinancePublicMarketDataClient:
    def __init__(self, base_url: str, timeout_seconds: float = 10.0) -> None:
        if base_url not in ALLOWED_HOSTS:
            raise DisallowedHostError(
                f"host {base_url!r} is not permitted; allowed hosts are {sorted(ALLOWED_HOSTS)}"
            )
        self._base_url = base_url
        self._timeout = timeout_seconds
        self._session = requests.Session()

    @property
    def base_url(self) -> str:
        return self._base_url

    def get_server_time_ms(self) -> int:
        resp = self._session.get(f"{self._base_url}/api/v3/time", timeout=self._timeout)
        resp.raise_for_status()
        return int(resp.json()["serverTime"])

    def get_klines(
        self,
        symbol: str,
        interval: str,
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
        limit: int = 1000,
    ) -> list[Candle]:
        params: dict[str, str | int] = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_time_ms is not None:
            params["startTime"] = start_time_ms
        if end_time_ms is not None:
            params["endTime"] = end_time_ms
        resp = self._session.get(f"{self._base_url}/api/v3/klines", params=params, timeout=self._timeout)
        resp.raise_for_status()
        return [Candle.from_binance_kline(symbol, interval, row) for row in resp.json()]

    def get_exchange_info(self, symbol: str) -> dict:
        resp = self._session.get(
            f"{self._base_url}/api/v3/exchangeInfo", params={"symbol": symbol}, timeout=self._timeout
        )
        resp.raise_for_status()
        return resp.json()
