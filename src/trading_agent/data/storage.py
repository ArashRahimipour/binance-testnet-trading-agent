"""SQLite persistence for candles.

Prices are stored as TEXT (Decimal's exact string form) rather than REAL, so
no floating-point rounding is introduced between ingestion and later reads.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Self

from trading_agent.data.models import Candle

_SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    symbol TEXT NOT NULL,
    interval TEXT NOT NULL,
    open_time_ms INTEGER NOT NULL,
    close_time_ms INTEGER NOT NULL,
    open TEXT NOT NULL,
    high TEXT NOT NULL,
    low TEXT NOT NULL,
    close TEXT NOT NULL,
    volume TEXT NOT NULL,
    PRIMARY KEY (symbol, interval, open_time_ms)
);
"""


class CandleStore:
    def __init__(self, db_path: str | Path) -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path))
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def upsert_candles(self, candles: list[Candle]) -> None:
        rows = [
            (
                c.symbol,
                c.interval,
                c.open_time_ms,
                c.close_time_ms,
                str(c.open),
                str(c.high),
                str(c.low),
                str(c.close),
                str(c.volume),
            )
            for c in candles
        ]
        self._conn.executemany(
            """
            INSERT INTO candles
                (symbol, interval, open_time_ms, close_time_ms, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, interval, open_time_ms) DO UPDATE SET
                close_time_ms=excluded.close_time_ms,
                open=excluded.open,
                high=excluded.high,
                low=excluded.low,
                close=excluded.close,
                volume=excluded.volume
            """,
            rows,
        )
        self._conn.commit()

    def get_candles(
        self,
        symbol: str,
        interval: str,
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
    ) -> list[Candle]:
        query = "SELECT open_time_ms, close_time_ms, open, high, low, close, volume FROM candles WHERE symbol = ? AND interval = ?"
        params: list[object] = [symbol, interval]
        if start_time_ms is not None:
            query += " AND open_time_ms >= ?"
            params.append(start_time_ms)
        if end_time_ms is not None:
            query += " AND open_time_ms <= ?"
            params.append(end_time_ms)
        query += " ORDER BY open_time_ms ASC"

        cursor = self._conn.execute(query, params)
        return [
            Candle(
                symbol=symbol,
                interval=interval,
                open_time_ms=row[0],
                close_time_ms=row[1],
                open=Decimal(row[2]),
                high=Decimal(row[3]),
                low=Decimal(row[4]),
                close=Decimal(row[5]),
                volume=Decimal(row[6]),
            )
            for row in cursor.fetchall()
        ]

    def latest_close_time_ms(self, symbol: str, interval: str) -> int | None:
        cursor = self._conn.execute(
            "SELECT MAX(close_time_ms) FROM candles WHERE symbol = ? AND interval = ?",
            (symbol, interval),
        )
        result = cursor.fetchone()[0]
        return int(result) if result is not None else None
