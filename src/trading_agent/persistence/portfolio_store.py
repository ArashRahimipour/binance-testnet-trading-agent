"""SQLite persistence for portfolio state.

There is a single portfolio row per (symbol). Absence of a saved state is
treated as "uninitialized", not "start from zero" - callers must seed it
explicitly (from config for backtests, or from a reconciled exchange
balance for testnet runs, in a later phase). This keeps the fail-closed
principle: the agent never guesses a starting balance.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Self

from trading_agent.portfolio.state import PortfolioState
from trading_agent.strategy.base import PositionSide

_SCHEMA = """
CREATE TABLE IF NOT EXISTS portfolio_state (
    symbol TEXT PRIMARY KEY,
    quote_balance TEXT NOT NULL,
    base_balance TEXT NOT NULL,
    position_side TEXT NOT NULL,
    avg_entry_price TEXT,
    realized_pnl_quote TEXT NOT NULL,
    open_position_entry_fee TEXT NOT NULL DEFAULT '0',
    pnl_is_estimated INTEGER NOT NULL DEFAULT 0,
    updated_at_ms INTEGER NOT NULL
);
"""

_MIGRATIONS = (
    "ALTER TABLE portfolio_state ADD COLUMN open_position_entry_fee TEXT NOT NULL DEFAULT '0'",
    "ALTER TABLE portfolio_state ADD COLUMN pnl_is_estimated INTEGER NOT NULL DEFAULT 0",
)


class PortfolioStore:
    def __init__(self, db_path: str | Path) -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path))
        self._conn.execute(_SCHEMA)
        self._conn.commit()
        self._apply_migrations()

    def _apply_migrations(self) -> None:
        existing_columns = {row[1] for row in self._conn.execute("PRAGMA table_info(portfolio_state)")}
        for migration in _MIGRATIONS:
            column_name = migration.split("ADD COLUMN")[1].split()[0]
            if column_name not in existing_columns:
                self._conn.execute(migration)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def save(self, symbol: str, state: PortfolioState, updated_at_ms: int) -> None:
        self._conn.execute(
            """
            INSERT INTO portfolio_state
                (symbol, quote_balance, base_balance, position_side, avg_entry_price,
                 realized_pnl_quote, open_position_entry_fee, pnl_is_estimated, updated_at_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                quote_balance=excluded.quote_balance,
                base_balance=excluded.base_balance,
                position_side=excluded.position_side,
                avg_entry_price=excluded.avg_entry_price,
                realized_pnl_quote=excluded.realized_pnl_quote,
                open_position_entry_fee=excluded.open_position_entry_fee,
                pnl_is_estimated=excluded.pnl_is_estimated,
                updated_at_ms=excluded.updated_at_ms
            """,
            (
                symbol,
                str(state.quote_balance),
                str(state.base_balance),
                state.position_side.value,
                str(state.avg_entry_price) if state.avg_entry_price is not None else None,
                str(state.realized_pnl_quote),
                str(state.open_position_entry_fee),
                int(state.pnl_is_estimated),
                updated_at_ms,
            ),
        )
        self._conn.commit()

    def load(self, symbol: str) -> PortfolioState | None:
        cursor = self._conn.execute(
            """
            SELECT quote_balance, base_balance, position_side, avg_entry_price, realized_pnl_quote,
                   open_position_entry_fee, pnl_is_estimated
            FROM portfolio_state WHERE symbol = ?
            """,
            (symbol,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        (
            quote_balance,
            base_balance,
            position_side,
            avg_entry_price,
            realized_pnl_quote,
            open_position_entry_fee,
            pnl_is_estimated,
        ) = row
        return PortfolioState(
            quote_balance=Decimal(quote_balance),
            base_balance=Decimal(base_balance),
            position_side=PositionSide(position_side),
            avg_entry_price=Decimal(avg_entry_price) if avg_entry_price is not None else None,
            realized_pnl_quote=Decimal(realized_pnl_quote),
            open_position_entry_fee=Decimal(open_position_entry_fee),
            pnl_is_estimated=bool(pnl_is_estimated),
        )
