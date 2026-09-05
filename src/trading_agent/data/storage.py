"""SQLite persistence for candles and their confirmed historical gap manifest.

Prices are stored as TEXT (Decimal's exact string form) rather than REAL, so
no floating-point rounding is introduced between ingestion and later reads.

The gap manifest (`candle_gaps`) records every CONFIRMED historical gap
(see data/gap_detection.py, data/historical_fetch.py::confirm_gaps) -
never a live/Testnet concern, since that path always fails closed on any
gap and never reaches this store with one. Its primary key is
(symbol, interval, expected_open_time_ms), so re-recording the same
confirmed gap on a repeated download is a no-op update, not a duplicate
row - this is what makes re-running the same download idempotent.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Self

from trading_agent.data.gap_detection import GapRecord
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

CREATE TABLE IF NOT EXISTS candle_gaps (
    symbol TEXT NOT NULL,
    interval TEXT NOT NULL,
    expected_open_time_ms INTEGER NOT NULL,
    previous_open_time_ms INTEGER NOT NULL,
    next_open_time_ms INTEGER NOT NULL,
    missing_intervals INTEGER NOT NULL,
    detected_at_ms INTEGER NOT NULL,
    PRIMARY KEY (symbol, interval, expected_open_time_ms)
);
"""


class CandleStore:
    def __init__(self, db_path: str | Path) -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path))
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def upsert_candles(self, candles: list[Candle]) -> None:
        self._upsert_candles_no_commit(candles)
        self._conn.commit()

    def store_candles_and_gaps(
        self,
        candles: list[Candle],
        gaps: list[GapRecord],
        symbol: str,
        interval: str,
        detected_at_ms: int,
        stale_gap_expected_open_times: list[int] | None = None,
    ) -> None:
        """Persist a historical download's candles and its confirmed gap
        manifest together, in ONE transaction - a failure partway through
        rolls back both writes rather than leaving candles committed
        without their gap record (or the reverse). Both writes are
        idempotent (`ON CONFLICT ... DO UPDATE`, keyed so a re-detected
        candle or gap just re-asserts the same fact), so re-running the
        same download twice leaves the database in the same state as
        running it once.

        `stale_gap_expected_open_times`, when given, additionally DELETES
        any `candle_gaps` row at that `(symbol, interval, expected_open_time_ms)`
        within this SAME transaction, before `gaps` is upserted - used when
        a previously-confirmed gap has since been fully or partially
        resolved (e.g. by `data/gap_recovery.py`) and its OLD manifest row
        must not survive alongside (or instead of) a freshly recomputed,
        narrower or absent gap record. Deleting a row that no longer
        exists is a no-op, so this stays idempotent. `None` (the default)
        preserves the exact prior behavior - the normal `fetch-data`
        re-download path never needs this, since a fresh full-range
        fetch's own `confirmed_gaps` already reflects the complete,
        current gap set for everything it just covered.
        """
        try:
            self._upsert_candles_no_commit(candles)
            if stale_gap_expected_open_times:
                self._delete_gaps_no_commit(symbol, interval, stale_gap_expected_open_times)
            self._upsert_gaps_no_commit(gaps, symbol, interval, detected_at_ms)
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def get_gaps(self, symbol: str, interval: str) -> list[GapRecord]:
        cursor = self._conn.execute(
            """
            SELECT expected_open_time_ms, previous_open_time_ms, next_open_time_ms, missing_intervals
            FROM candle_gaps WHERE symbol = ? AND interval = ? ORDER BY expected_open_time_ms ASC
            """,
            (symbol, interval),
        )
        return [
            GapRecord(
                expected_open_time_ms=row[0],
                previous_open_time_ms=row[1],
                next_open_time_ms=row[2],
                missing_intervals=row[3],
            )
            for row in cursor.fetchall()
        ]

    def _upsert_candles_no_commit(self, candles: list[Candle]) -> None:
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

    def _delete_gaps_no_commit(
        self, symbol: str, interval: str, expected_open_time_mss: list[int]
    ) -> None:
        rows = [(symbol, interval, t) for t in expected_open_time_mss]
        self._conn.executemany(
            "DELETE FROM candle_gaps WHERE symbol = ? AND interval = ? AND expected_open_time_ms = ?",
            rows,
        )

    def _upsert_gaps_no_commit(
        self, gaps: list[GapRecord], symbol: str, interval: str, detected_at_ms: int
    ) -> None:
        rows = [
            (
                symbol,
                interval,
                gap.expected_open_time_ms,
                gap.previous_open_time_ms,
                gap.next_open_time_ms,
                gap.missing_intervals,
                detected_at_ms,
            )
            for gap in gaps
        ]
        self._conn.executemany(
            """
            INSERT INTO candle_gaps
                (symbol, interval, expected_open_time_ms, previous_open_time_ms,
                 next_open_time_ms, missing_intervals, detected_at_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, interval, expected_open_time_ms) DO UPDATE SET
                previous_open_time_ms=excluded.previous_open_time_ms,
                next_open_time_ms=excluded.next_open_time_ms,
                missing_intervals=excluded.missing_intervals,
                detected_at_ms=excluded.detected_at_ms
            """,
            rows,
        )

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
