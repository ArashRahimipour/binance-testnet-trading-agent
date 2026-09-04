"""The single authoritative, transactional store for portfolio state and
pending (in-flight) orders.

Review Finding (round 2, #1): applying a fill, updating an order's applied
cumulative execution fields, updating portfolio state, and marking
terminal resolution must not be split across independently committed
writes or databases - a crash between them silently loses or duplicates
a fill. Both tables live in ONE SQLite file behind ONE connection, and
`apply_order_result_atomically` performs all of the above in a single
transaction: it either commits every part exactly once, or (on any
failure) rolls back every part, leaving the pending order exactly as it
was before the call - safe to retry from the same state.

This does not claim exactly-once behavior across separate databases, and
the journal (`journal/journal.py`, a deliberately separate append-only
audit log) is never treated as authoritative execution state - it is
written only after this transaction has already committed.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Literal, Self

from trading_agent.execution.order_outcome import OrderApplicationResult, compute_order_application
from trading_agent.execution.testnet_adapter import OrderResult
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

CREATE TABLE IF NOT EXISTS pending_orders (
    client_order_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    requested_quantity TEXT NOT NULL,
    signal_candle_close_time_ms INTEGER NOT NULL,
    submitted_at_ms INTEGER NOT NULL,
    status TEXT NOT NULL,
    applied_executed_qty TEXT NOT NULL DEFAULT '0',
    applied_cumulative_quote_qty TEXT NOT NULL DEFAULT '0',
    applied_commission_quote TEXT NOT NULL DEFAULT '0',
    applied_commission_base TEXT NOT NULL DEFAULT '0',
    applied_commission_other_json TEXT NOT NULL DEFAULT '{}',
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
    applied_cumulative_quote_qty: Decimal
    applied_commission_quote: Decimal
    applied_commission_base: Decimal
    applied_commission_other: dict[str, Decimal]
    resolved_order_status: str | None
    resolved_at_ms: int | None


class ExecutionStateStore:
    """One SQLite connection, two tables, one atomic cross-table write path."""

    def __init__(self, db_path: str | Path) -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # autocommit for ordinary single-statement writes; the atomic method
        # below manages its own explicit transaction boundary.
        self._conn = sqlite3.connect(str(path), isolation_level=None)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        # Test-only fault injection seam - see tests/unit/test_execution_store.py.
        # Never set outside tests. Valid values: "before_write",
        # "after_portfolio_write", "before_commit".
        self._raise_fault_at: str | None = None

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @classmethod
    def open_read_only(cls, db_path: str | Path) -> Self | None:
        """Open an EXISTING execution-state database strictly read-only,
        for reporting purposes only (e.g. the `testnet-health` CLI
        command). Returns `None` if the file does not exist - it never
        creates one, and never runs the `_SCHEMA` migration (which is
        itself a write, and would otherwise create a fresh, empty database
        exactly where the caller is trying to detect whether one already
        exists).

        The connection is opened with SQLite's own `mode=ro` URI flag, so
        even a bug that tried to write through the returned instance would
        fail at the SQLite layer - not merely by the caller's discipline of
        only calling read methods (`load_portfolio`, `load_open_pending`,
        `get_pending`) on it.
        """
        path = Path(db_path)
        if not path.exists():
            return None
        instance = cls.__new__(cls)
        instance._conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
        instance._raise_fault_at = None
        return instance

    # --- Portfolio: direct (non-order-triggered) reads/writes -----------------

    def load_portfolio(self, symbol: str) -> PortfolioState | None:
        row = self._conn.execute(
            """
            SELECT quote_balance, base_balance, position_side, avg_entry_price, realized_pnl_quote,
                   open_position_entry_fee, pnl_is_estimated
            FROM portfolio_state WHERE symbol = ?
            """,
            (symbol,),
        ).fetchone()
        return self._row_to_portfolio(row) if row else None

    def save_portfolio(self, symbol: str, state: PortfolioState, updated_at_ms: int) -> None:
        """A direct write, used only to seed state that did not come from
        applying an order fill (e.g. cold-start reconciliation from the
        exchange's account balance). Any change that stems from an order
        fill must go through `apply_order_result_atomically` instead.
        """
        self._write_portfolio(symbol, state, updated_at_ms)

    # --- Pending orders ---------------------------------------------------

    def create_pending(
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
                 submitted_at_ms, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(client_order_id) DO NOTHING
            """,
            (client_order_id, symbol, side, str(requested_quantity), signal_candle_close_time_ms,
             submitted_at_ms, STATUS_SUBMITTED),
        )

    def load_open_pending(self, symbol: str) -> list[PendingOrder]:
        rows = self._conn.execute(
            f"{_PENDING_SELECT} WHERE symbol = ? AND status = ? ORDER BY submitted_at_ms ASC",
            (symbol, STATUS_SUBMITTED),
        ).fetchall()
        return [self._row_to_pending(row) for row in rows]

    def get_pending(self, client_order_id: str) -> PendingOrder | None:
        row = self._conn.execute(
            f"{_PENDING_SELECT} WHERE client_order_id = ?", (client_order_id,)
        ).fetchone()
        return self._row_to_pending(row) if row else None

    # --- The atomic operation ----------------------------------------------

    def apply_order_result_atomically(
        self,
        symbol: str,
        quote_asset: str,
        base_asset: str,
        order: OrderResult,
        fallback_fee_pct: Decimal,
        now_ms: int,
    ) -> OrderApplicationResult:
        """Atomically: re-read the pending order and portfolio rows, verify
        and apply only the new execution delta, update portfolio state,
        update the pending order's applied cumulative fields, mark terminal
        resolution when appropriate, and commit once. Rolls back entirely
        - leaving both tables exactly as they were - on any failure,
        including an inconsistent execution report.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            pending_row = self._conn.execute(
                f"{_PENDING_SELECT} WHERE client_order_id = ?", (order.client_order_id,)
            ).fetchone()
            if pending_row is None:
                raise ValueError(
                    f"no durable pending-order record for {order.client_order_id!r} - "
                    "refusing to apply a fill with nothing to reconcile against"
                )
            pending = self._row_to_pending(pending_row)

            portfolio_row = self._conn.execute(
                "SELECT quote_balance, base_balance, position_side, avg_entry_price, realized_pnl_quote, "
                "open_position_entry_fee, pnl_is_estimated FROM portfolio_state WHERE symbol = ?",
                (symbol,),
            ).fetchone()
            if portfolio_row is None:
                raise ValueError(f"no portfolio state for {symbol!r} - cannot apply a fill before it is seeded")
            portfolio = self._row_to_portfolio(portfolio_row)

            self._maybe_raise_fault("before_write")

            side: Literal["BUY", "SELL"] = "BUY" if pending.side == "BUY" else "SELL"
            result = compute_order_application(
                order, side, quote_asset, base_asset, portfolio,
                pending.applied_executed_qty, pending.applied_cumulative_quote_qty,
                pending.applied_commission_quote, pending.applied_commission_base,
                pending.applied_commission_other, fallback_fee_pct,
            )

            self._write_portfolio(symbol, result.portfolio, now_ms)

            self._maybe_raise_fault("after_portfolio_write")

            new_status = STATUS_RESOLVED if result.is_terminal else STATUS_SUBMITTED
            self._conn.execute(
                """
                UPDATE pending_orders SET
                    applied_executed_qty = ?, applied_cumulative_quote_qty = ?,
                    applied_commission_quote = ?, applied_commission_base = ?,
                    applied_commission_other_json = ?, status = ?, resolved_order_status = ?, resolved_at_ms = ?
                WHERE client_order_id = ?
                """,
                (
                    str(result.new_applied_executed_qty),
                    str(result.new_applied_cumulative_quote_qty),
                    str(result.new_applied_commission_quote),
                    str(result.new_applied_commission_base),
                    json.dumps({k: str(v) for k, v in result.new_applied_commission_other.items()}),
                    new_status,
                    order.status if result.is_terminal else None,
                    now_ms if result.is_terminal else None,
                    order.client_order_id,
                ),
            )

            self._maybe_raise_fault("before_commit")

            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return result

    def _maybe_raise_fault(self, point: str) -> None:
        if self._raise_fault_at == point:
            raise _InjectedTestFault(f"fault injected at {point!r}")

    def _write_portfolio(self, symbol: str, state: PortfolioState, updated_at_ms: int) -> None:
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

    @staticmethod
    def _row_to_portfolio(row: tuple) -> PortfolioState:
        (quote_balance, base_balance, position_side, avg_entry_price, realized_pnl_quote,
         open_position_entry_fee, pnl_is_estimated) = row
        return PortfolioState(
            quote_balance=Decimal(quote_balance),
            base_balance=Decimal(base_balance),
            position_side=PositionSide(position_side),
            avg_entry_price=Decimal(avg_entry_price) if avg_entry_price is not None else None,
            realized_pnl_quote=Decimal(realized_pnl_quote),
            open_position_entry_fee=Decimal(open_position_entry_fee),
            pnl_is_estimated=bool(pnl_is_estimated),
        )

    @staticmethod
    def _row_to_pending(row: tuple) -> PendingOrder:
        other_json = row[11]
        other = {k: Decimal(v) for k, v in json.loads(other_json).items()} if other_json else {}
        return PendingOrder(
            client_order_id=row[0],
            symbol=row[1],
            side=row[2],
            requested_quantity=Decimal(row[3]),
            signal_candle_close_time_ms=row[4],
            submitted_at_ms=row[5],
            status=row[6],
            applied_executed_qty=Decimal(row[7]),
            applied_cumulative_quote_qty=Decimal(row[8]),
            applied_commission_quote=Decimal(row[9]),
            applied_commission_base=Decimal(row[10]),
            applied_commission_other=other,
            resolved_order_status=row[12],
            resolved_at_ms=row[13],
        )


class _InjectedTestFault(Exception):
    """Raised only when a test has set `_raise_fault_at` - proves the
    atomic transaction rolls back completely regardless of where the
    failure occurs. Never raised in normal operation."""


_PENDING_SELECT = """
    SELECT client_order_id, symbol, side, requested_quantity, signal_candle_close_time_ms,
           submitted_at_ms, status, applied_executed_qty, applied_cumulative_quote_qty,
           applied_commission_quote, applied_commission_base, applied_commission_other_json,
           resolved_order_status, resolved_at_ms
    FROM pending_orders
"""
