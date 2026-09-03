"""Helpers to build synthetic Binance kline rows for tests."""

from __future__ import annotations

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
