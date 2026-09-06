"""Helpers to build synthetic Binance kline rows for tests."""

from __future__ import annotations

import json
from collections.abc import Callable
from urllib.parse import parse_qs, urlparse

from trading_agent.data.models import interval_to_ms


def make_kline_row(open_time_ms: int, interval: str, close: float = 100.0) -> list:
    close_time_ms = open_time_ms + interval_to_ms(interval) - 1
    return [
        open_time_ms,
        f"{close:.2f}",
        f"{close + 1:.2f}",
        f"{close - 1:.2f}",
        f"{close:.2f}",
        "10.0",
        close_time_ms,
        "1000.0",
        100,
        "5.0",
        "500.0",
        "0",
    ]


def make_kline_series(start_open_time_ms: int, interval: str, count: int) -> list[list]:
    step = interval_to_ms(interval)
    return [
        make_kline_row(start_open_time_ms + i * step, interval, close=100.0 + i)
        for i in range(count)
    ]


def make_stateful_klines_callback(rows: list[list]) -> Callable:
    """A `responses.add_callback` handler that serves `/api/v3/klines`
    requests directly from a precomputed, already-sorted `rows` list -
    honoring `startTime`/`endTime` (both inclusive, exactly like the real
    exchange) and `limit`, so a caller under test (e.g.
    `data/historical_fetch.py::fetch_historical_range`) can page through a
    large synthetic range (thousands of candles) with a SINGLE registered
    mock, instead of hand-registering one `responses.add` per page.
    """

    def _callback(request: object) -> tuple[int, dict, str]:
        query = parse_qs(urlparse(request.url).query)  # type: ignore[attr-defined]
        start_time = int(query["startTime"][0]) if "startTime" in query else None
        end_time = int(query["endTime"][0]) if "endTime" in query else None
        limit = int(query["limit"][0]) if "limit" in query else 1000
        page = [
            row for row in rows
            if (start_time is None or row[0] >= start_time) and (end_time is None or row[0] <= end_time)
        ][:limit]
        return (200, {}, json.dumps(page))

    return _callback
