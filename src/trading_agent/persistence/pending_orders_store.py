"""Durable record of submitted-but-not-yet-fully-resolved orders.

A row is written with status='SUBMITTED' BEFORE the exchange call is made,
and is only cleared (status='RESOLVED') after its outcome has been
durably applied to portfolio state. This is what makes crash recovery
possible: whatever state the process is in when it crashes, the next run
finds this row (if the order was ever submitted) and can ask Binance what
actually happened via `client_order_id` - independent of whether the
crash happened before, during, or after the HTTP round trip, or between
applying a fill and persisting it. See ARCHITECTURE.md's "Crash recovery
and the pending-order state machine" section for the full state diagram.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Self

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_orders (
    client_order_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    requested_quantity TEXT NOT NULL,
    signal_candle_close_time_ms INTEGER NOT NULL,
    submitted_at_ms INTEGER NOT NULL,
    status TEXT NOT NULL,
    applied_executed_qty TEXT NOT NULL DEFAULT '0',
    resolved_order_status TEXT,
    resolved_at_ms INTEGER
);
"""

STATUS_SUBMITTED = "SUBMITTED"
STATUS_RESOLVED = "RESOLVED"


@dataclass(frozen=True, slots=True)
class PendingOrder:
    client_order_id: str
    symbol: str
    side: str
    requested_quantity: Decimal
    signal_candle_close_time_ms: int
    submitted_at_ms: int
    status: str
    applied_executed_qty: Decimal
    resolved_order_status: str | None
    resolved_at_ms: int | None


class PendingOrdersStore:
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

    def create(
        self,
        client_order_id: str,
        symbol: str,
        side: str,
        requested_quantity: Decimal,
        signal_candle_close_time_ms: int,
        submitted_at_ms: int,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO pending_orders
                (client_order_id, symbol, side, requested_quantity, signal_candle_close_time_ms,
                 submitted_at_ms, status, applied_executed_qty)
            VALUES (?, ?, ?, ?, ?, ?, ?, '0')
            ON CONFLICT(client_order_id) DO NOTHING
            """,
            (client_order_id, symbol, side, str(requested_quantity), signal_candle_close_time_ms,
             submitted_at_ms, STATUS_SUBMITTED),
        )
        self._conn.commit()

    def load_open(self, symbol: str) -> list[PendingOrder]:
        cursor = self._conn.execute(
            """
            SELECT client_order_id, symbol, side, requested_quantity, signal_candle_close_time_ms,
                   submitted_at_ms, status, applied_executed_qty, resolved_order_status, resolved_at_ms
            FROM pending_orders WHERE symbol = ? AND status = ?
            ORDER BY submitted_at_ms ASC
            """,
            (symbol, STATUS_SUBMITTED),
        )
        return [self._row_to_pending_order(row) for row in cursor.fetchall()]

    def get(self, client_order_id: str) -> PendingOrder | None:
        cursor = self._conn.execute(
            """
            SELECT client_order_id, symbol, side, requested_quantity, signal_candle_close_time_ms,
                   submitted_at_ms, status, applied_executed_qty, resolved_order_status, resolved_at_ms
            FROM pending_orders WHERE client_order_id = ?
            """,
            (client_order_id,),
        )
        row = cursor.fetchone()
        return self._row_to_pending_order(row) if row else None

    def update_applied_qty(self, client_order_id: str, new_applied_qty: Decimal) -> None:
        self._conn.execute(
            "UPDATE pending_orders SET applied_executed_qty = ? WHERE client_order_id = ?",
            (str(new_applied_qty), client_order_id),
        )
        self._conn.commit()

    def mark_resolved(self, client_order_id: str, resolved_order_status: str, resolved_at_ms: int) -> None:
        self._conn.execute(
            """
            UPDATE pending_orders
            SET status = ?, resolved_order_status = ?, resolved_at_ms = ?
            WHERE client_order_id = ?
            """,
            (STATUS_RESOLVED, resolved_order_status, resolved_at_ms, client_order_id),
        )
        self._conn.commit()

    @staticmethod
    def _row_to_pending_order(row: tuple) -> PendingOrder:
        return PendingOrder(
            client_order_id=row[0],
            symbol=row[1],
            side=row[2],
            requested_quantity=Decimal(row[3]),
            signal_candle_close_time_ms=row[4],
            submitted_at_ms=row[5],
            status=row[6],
            applied_executed_qty=Decimal(row[7]),
            resolved_order_status=row[8],
            resolved_at_ms=row[9],
        )
