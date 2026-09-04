"""Persisted risk-tracking state for live (testnet) runs.

Backtests recompute this in-memory for the whole run (see backtest/engine.py).
A live run is invoked once per completed candle (typically by an external
scheduler - see README.md), so the same daily counters, cooldown, and
peak-equity tracking must survive between separate CLI invocations.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Self

_SCHEMA = """
CREATE TABLE IF NOT EXISTS risk_state (
    symbol TEXT PRIMARY KEY,
    day_key TEXT NOT NULL,
    daily_start_equity TEXT NOT NULL,
    daily_realized_pnl_pct REAL NOT NULL,
    trades_today INTEGER NOT NULL,
    peak_equity TEXT NOT NULL,
    cooldown_bars_remaining INTEGER NOT NULL,
    consecutive_api_errors INTEGER NOT NULL
);
"""


class RiskState:
    def __init__(
        self,
        day_key: str,
        daily_start_equity: Decimal,
        daily_realized_pnl_pct: float,
        trades_today: int,
        peak_equity: Decimal,
        cooldown_bars_remaining: int,
        consecutive_api_errors: int,
    ) -> None:
        self.day_key = day_key
        self.daily_start_equity = daily_start_equity
        self.daily_realized_pnl_pct = daily_realized_pnl_pct
        self.trades_today = trades_today
        self.peak_equity = peak_equity
        self.cooldown_bars_remaining = cooldown_bars_remaining
        self.consecutive_api_errors = consecutive_api_errors

    @staticmethod
    def initial(starting_equity: Decimal, day_key: str) -> RiskState:
        return RiskState(day_key, starting_equity, 0.0, 0, starting_equity, 0, 0)


class RiskStateStore:
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

    def save(self, symbol: str, state: RiskState) -> None:
        self._conn.execute(
            """
            INSERT INTO risk_state
                (symbol, day_key, daily_start_equity, daily_realized_pnl_pct, trades_today,
                 peak_equity, cooldown_bars_remaining, consecutive_api_errors)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                day_key=excluded.day_key,
                daily_start_equity=excluded.daily_start_equity,
                daily_realized_pnl_pct=excluded.daily_realized_pnl_pct,
                trades_today=excluded.trades_today,
                peak_equity=excluded.peak_equity,
                cooldown_bars_remaining=excluded.cooldown_bars_remaining,
                consecutive_api_errors=excluded.consecutive_api_errors
            """,
            (
                symbol,
                state.day_key,
                str(state.daily_start_equity),
                state.daily_realized_pnl_pct,
                state.trades_today,
                str(state.peak_equity),
                state.cooldown_bars_remaining,
                state.consecutive_api_errors,
            ),
        )
        self._conn.commit()

    def load(self, symbol: str) -> RiskState | None:
        cursor = self._conn.execute(
            """
            SELECT day_key, daily_start_equity, daily_realized_pnl_pct, trades_today,
                   peak_equity, cooldown_bars_remaining, consecutive_api_errors
            FROM risk_state WHERE symbol = ?
            """,
            (symbol,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return RiskState(
            day_key=row[0],
            daily_start_equity=Decimal(row[1]),
            daily_realized_pnl_pct=row[2],
            trades_today=row[3],
            peak_equity=Decimal(row[4]),
            cooldown_bars_remaining=row[5],
            consecutive_api_errors=row[6],
        )
