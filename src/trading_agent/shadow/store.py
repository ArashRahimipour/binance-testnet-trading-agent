"""The durable, atomic store for everything one shadow cycle produces.

`shadow/engine.py` recomputes the ENTIRE accumulated shadow history through
`backtest/engine.py::run_segment` (unmodified) on every single cycle - a
pure, deterministic function of the immutable candle history plus the
frozen E1 strategy/config, so re-running it produces byte-for-byte the same
result up to the point already observed. This store is what turns that
"recompute everything, every time" design into something that persists
correctly: it keeps a single high-water mark
(`last_processed_close_time_ms`), and `record_cycle_atomically` inserts
only the rows (closed trades, equity points, journal entries) whose own
timestamp is strictly after that mark, in ONE transaction that also
advances the mark - so a crash between "candles fetched" and "this
transaction commits" is always safe to retry: the mark cannot advance
without the rows it covers also being committed, and every row's primary
key makes a duplicate insert of an already-persisted row a no-op rather
than an error. This is what makes shadow processing idempotent and crash
recoverable, and guarantees a completed candle is never scored twice into
the persisted record.

Two separate SQLite connections point at the SAME `data/shadow_agent.db`
file: `data/storage.py::CandleStore` (candle history) and this class
(everything else). This is safe because within one process, the two
connections' writes are always sequential and never interleaved: a shadow
cycle upserts fetched candles (committing that write) and only afterwards
opens and commits this store's own transaction - never both at once, and
never from more than one process at a time (see `shadow/lock.py`).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Self

from trading_agent.data.models import Candle
from trading_agent.metrics.diagnostics import OpenPositionInfo
from trading_agent.metrics.performance import EquityPoint, Trade

_SCHEMA = """
CREATE TABLE IF NOT EXISTS shadow_run_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_processed_close_time_ms INTEGER,
    last_run_at_ms INTEGER NOT NULL,
    total_cycles INTEGER NOT NULL DEFAULT 0,
    last_segment_length INTEGER,
    last_cycle_status TEXT NOT NULL DEFAULT '',
    last_cycle_detail TEXT NOT NULL DEFAULT '',
    open_position_entry_time_ms INTEGER,
    open_position_entry_price TEXT,
    open_position_entry_reference_price TEXT,
    open_position_quantity TEXT,
    open_position_entry_fee_quote TEXT
);

CREATE TABLE IF NOT EXISTS shadow_trades (
    exit_time_ms INTEGER PRIMARY KEY,
    entry_time_ms INTEGER NOT NULL,
    entry_price TEXT NOT NULL,
    exit_price TEXT NOT NULL,
    quantity TEXT NOT NULL,
    fees_paid TEXT NOT NULL,
    pnl_quote TEXT NOT NULL,
    exit_reason TEXT NOT NULL,
    entry_fee_quote TEXT NOT NULL,
    exit_fee_quote TEXT NOT NULL,
    entry_reference_price TEXT NOT NULL,
    exit_reference_price TEXT NOT NULL,
    planned_risk_quote TEXT,
    net_reward_to_risk REAL
);

CREATE TABLE IF NOT EXISTS shadow_equity (
    timestamp_ms INTEGER PRIMARY KEY,
    equity TEXT NOT NULL,
    in_position INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS shadow_journal_entries (
    entry_type TEXT NOT NULL,
    timestamp_ms INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (entry_type, timestamp_ms)
);

CREATE TABLE IF NOT EXISTS shadow_bootstrap_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    warmup_start_time_ms INTEGER NOT NULL,
    warmup_last_open_time_ms INTEGER NOT NULL,
    warmup_candle_count INTEGER NOT NULL,
    effective_min_required_candles INTEGER NOT NULL,
    bootstrapped_at_ms INTEGER NOT NULL,
    source TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shadow_warmup_candles (
    open_time_ms INTEGER PRIMARY KEY,
    close_time_ms INTEGER NOT NULL,
    fetched_at_ms INTEGER NOT NULL,
    source TEXT NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class ShadowTradeRecord:
    """A closed shadow trade, with the planned risk/reward it was approved
    and sized under attached (same alignment convention as `research/
    post_mortem.py::_correlate_trades_with_plans` - `None` only if it could
    not be correlated, which does not happen for a trade this store itself
    persisted)."""

    trade: Trade
    planned_risk_quote: Decimal | None
    net_reward_to_risk: float | None


@dataclass(frozen=True, slots=True)
class ShadowBootstrapState:
    """Metadata for the ONE-TIME warm-up fetch `shadow/bootstrap.py::
    run_shadow_bootstrap` performs - see that module for what each field
    means and why `effective_min_required_candles` is larger than
    `warmup_candle_count + 1` (the settling-buffer design)."""

    warmup_start_time_ms: int
    warmup_last_open_time_ms: int
    warmup_candle_count: int
    effective_min_required_candles: int
    bootstrapped_at_ms: int
    source: str


@dataclass(frozen=True, slots=True)
class ShadowRunState:
    last_processed_close_time_ms: int | None
    last_run_at_ms: int | None
    total_cycles: int
    last_segment_length: int | None
    last_cycle_status: str
    last_cycle_detail: str
    open_position: OpenPositionInfo | None


class _InjectedTestFault(Exception):
    """Raised only when a test has set `_raise_fault_at` - proves the
    atomic cycle transaction rolls back completely regardless of where the
    failure occurs. Never raised in normal operation."""


class ShadowStore:
    def __init__(self, db_path: str | Path) -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), isolation_level=None)
        self._conn.executescript(_SCHEMA)
        # Test-only fault injection seam - see tests/unit/test_shadow_store.py.
        # Never set outside tests. Valid values: "after_trades",
        # "after_equity", "after_journal", "before_commit" (all in
        # `record_cycle_atomically`), and "after_warmup_candles",
        # "before_commit" (in `record_bootstrap_atomically`).
        self._raise_fault_at: str | None = None

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def get_run_state(self) -> ShadowRunState:
        row = self._conn.execute(
            """
            SELECT last_processed_close_time_ms, last_run_at_ms, total_cycles, last_segment_length,
                   last_cycle_status, last_cycle_detail, open_position_entry_time_ms,
                   open_position_entry_price, open_position_entry_reference_price,
                   open_position_quantity, open_position_entry_fee_quote
            FROM shadow_run_state WHERE id = 1
            """
        ).fetchone()
        if row is None:
            return ShadowRunState(
                last_processed_close_time_ms=None,
                last_run_at_ms=None,
                total_cycles=0,
                last_segment_length=None,
                last_cycle_status="",
                last_cycle_detail="",
                open_position=None,
            )
        open_position = None
        if row[6] is not None:
            open_position = OpenPositionInfo(
                entry_time_ms=row[6],
                entry_price=Decimal(row[7]),
                entry_reference_price=Decimal(row[8]),
                quantity=Decimal(row[9]),
                entry_fee_quote=Decimal(row[10]),
            )
        return ShadowRunState(
            last_processed_close_time_ms=row[0],
            last_run_at_ms=row[1],
            total_cycles=row[2],
            last_segment_length=row[3],
            last_cycle_status=row[4],
            last_cycle_detail=row[5],
            open_position=open_position,
        )

    def touch_cycle(self, now_ms: int, status: str, detail: str, segment_length: int | None) -> None:
        """A lightweight state update for a cycle that made no data
        changes (e.g. no new completed candle yet) - advances only the
        observability fields (`last_run_at_ms`, `total_cycles`), never
        `last_processed_close_time_ms`."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            state = self.get_run_state()
            self._upsert_run_state(
                last_processed_close_time_ms=state.last_processed_close_time_ms,
                last_run_at_ms=now_ms,
                total_cycles=state.total_cycles + 1,
                last_segment_length=segment_length,
                last_cycle_status=status,
                last_cycle_detail=detail,
                open_position=state.open_position,
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def record_cycle_atomically(
        self,
        new_trades: list[ShadowTradeRecord],
        new_equity_points: list[EquityPoint],
        new_journal_entries: list[dict[str, Any]],
        new_last_processed_close_time_ms: int,
        open_position: OpenPositionInfo | None,
        now_ms: int,
        segment_length: int,
        status: str,
        detail: str,
    ) -> None:
        """Insert every new row this cycle produced, update the current
        open-position snapshot, and advance the high-water mark - all in
        ONE transaction. Every insert is idempotent (`INSERT OR IGNORE` /
        `ON CONFLICT ... DO NOTHING`, keyed so re-inserting an
        already-persisted row is a no-op), so retrying this exact call
        after a crash (with the same, or a superset, of `new_trades` etc.)
        leaves the database in the same state as if it had succeeded once.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            state = self.get_run_state()

            for record in new_trades:
                self._insert_trade_no_commit(record)
            self._maybe_raise_fault("after_trades")

            for point in new_equity_points:
                self._insert_equity_point_no_commit(point)
            self._maybe_raise_fault("after_equity")

            for entry in new_journal_entries:
                self._insert_journal_entry_no_commit(entry)
            self._maybe_raise_fault("after_journal")

            self._upsert_run_state(
                last_processed_close_time_ms=new_last_processed_close_time_ms,
                last_run_at_ms=now_ms,
                total_cycles=state.total_cycles + 1,
                last_segment_length=segment_length,
                last_cycle_status=status,
                last_cycle_detail=detail,
                open_position=open_position,
            )

            self._maybe_raise_fault("before_commit")
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def get_all_trades(self) -> list[ShadowTradeRecord]:
        rows = self._conn.execute(
            """
            SELECT entry_time_ms, exit_time_ms, entry_price, exit_price, quantity, fees_paid, pnl_quote,
                   exit_reason, entry_fee_quote, exit_fee_quote, entry_reference_price, exit_reference_price,
                   planned_risk_quote, net_reward_to_risk
            FROM shadow_trades ORDER BY exit_time_ms ASC
            """
        ).fetchall()
        return [self._row_to_trade_record(row) for row in rows]

    def get_equity_curve(self) -> list[EquityPoint]:
        rows = self._conn.execute(
            "SELECT timestamp_ms, equity, in_position FROM shadow_equity ORDER BY timestamp_ms ASC"
        ).fetchall()
        return [
            EquityPoint(timestamp_ms=row[0], equity=Decimal(row[1]), in_position=bool(row[2])) for row in rows
        ]

    def get_journal_entries(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT entry_type, timestamp_ms, payload_json FROM shadow_journal_entries ORDER BY timestamp_ms ASC"
        ).fetchall()
        return [
            {"entry_type": row[0], "timestamp_ms": row[1], "payload": json.loads(row[2])} for row in rows
        ]

    def get_bootstrap_state(self) -> ShadowBootstrapState | None:
        row = self._conn.execute(
            """
            SELECT warmup_start_time_ms, warmup_last_open_time_ms, warmup_candle_count,
                   effective_min_required_candles, bootstrapped_at_ms, source
            FROM shadow_bootstrap_state WHERE id = 1
            """
        ).fetchone()
        if row is None:
            return None
        return ShadowBootstrapState(
            warmup_start_time_ms=row[0],
            warmup_last_open_time_ms=row[1],
            warmup_candle_count=row[2],
            effective_min_required_candles=row[3],
            bootstrapped_at_ms=row[4],
            source=row[5],
        )

    def get_warmup_candle_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM shadow_warmup_candles").fetchone()
        return int(row[0])

    def record_bootstrap_atomically(
        self,
        warmup_candles: list[Candle],
        warmup_start_time_ms: int,
        warmup_last_open_time_ms: int,
        warmup_candle_count: int,
        effective_min_required_candles: int,
        bootstrapped_at_ms: int,
        source: str,
    ) -> None:
        """Persist every warm-up candle's provenance row plus the singleton
        bootstrap-state row, atomically. Idempotent: re-inserting an
        already-recorded provenance row is a no-op
        (`ON CONFLICT ... DO NOTHING`), and the singleton state row is only
        ever inserted once (`ON CONFLICT(id) DO NOTHING`) - a caller that
        finds `get_bootstrap_state()` already populated must never call
        this again to silently overwrite it with a different range; see
        `shadow/bootstrap.py::run_shadow_bootstrap`, which checks that
        BEFORE ever calling this.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            for candle in warmup_candles:
                self._conn.execute(
                    """
                    INSERT INTO shadow_warmup_candles (open_time_ms, close_time_ms, fetched_at_ms, source)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(open_time_ms) DO NOTHING
                    """,
                    (candle.open_time_ms, candle.close_time_ms, bootstrapped_at_ms, source),
                )
            self._maybe_raise_fault("after_warmup_candles")
            self._conn.execute(
                """
                INSERT INTO shadow_bootstrap_state
                    (id, warmup_start_time_ms, warmup_last_open_time_ms, warmup_candle_count,
                     effective_min_required_candles, bootstrapped_at_ms, source)
                VALUES (1, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (
                    warmup_start_time_ms, warmup_last_open_time_ms, warmup_candle_count,
                    effective_min_required_candles, bootstrapped_at_ms, source,
                ),
            )
            self._maybe_raise_fault("before_commit")
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def _maybe_raise_fault(self, point: str) -> None:
        if self._raise_fault_at == point:
            raise _InjectedTestFault(f"fault injected at {point!r}")

    def _insert_trade_no_commit(self, record: ShadowTradeRecord) -> None:
        t = record.trade
        self._conn.execute(
            """
            INSERT INTO shadow_trades
                (exit_time_ms, entry_time_ms, entry_price, exit_price, quantity, fees_paid, pnl_quote,
                 exit_reason, entry_fee_quote, exit_fee_quote, entry_reference_price, exit_reference_price,
                 planned_risk_quote, net_reward_to_risk)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(exit_time_ms) DO NOTHING
            """,
            (
                t.exit_time_ms, t.entry_time_ms, str(t.entry_price), str(t.exit_price), str(t.quantity),
                str(t.fees_paid), str(t.pnl_quote), t.exit_reason, str(t.entry_fee_quote), str(t.exit_fee_quote),
                str(t.entry_reference_price), str(t.exit_reference_price),
                str(record.planned_risk_quote) if record.planned_risk_quote is not None else None,
                record.net_reward_to_risk,
            ),
        )

    def _insert_equity_point_no_commit(self, point: EquityPoint) -> None:
        self._conn.execute(
            """
            INSERT INTO shadow_equity (timestamp_ms, equity, in_position) VALUES (?, ?, ?)
            ON CONFLICT(timestamp_ms) DO NOTHING
            """,
            (point.timestamp_ms, str(point.equity), int(point.in_position)),
        )

    def _insert_journal_entry_no_commit(self, entry: dict[str, Any]) -> None:
        self._conn.execute(
            """
            INSERT INTO shadow_journal_entries (entry_type, timestamp_ms, payload_json) VALUES (?, ?, ?)
            ON CONFLICT(entry_type, timestamp_ms) DO NOTHING
            """,
            (entry["entry_type"], entry["timestamp_ms"], json.dumps(entry["payload"], default=str)),
        )

    def _upsert_run_state(
        self,
        last_processed_close_time_ms: int | None,
        last_run_at_ms: int,
        total_cycles: int,
        last_segment_length: int | None,
        last_cycle_status: str,
        last_cycle_detail: str,
        open_position: OpenPositionInfo | None,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO shadow_run_state
                (id, last_processed_close_time_ms, last_run_at_ms, total_cycles, last_segment_length,
                 last_cycle_status, last_cycle_detail, open_position_entry_time_ms, open_position_entry_price,
                 open_position_entry_reference_price, open_position_quantity, open_position_entry_fee_quote)
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                last_processed_close_time_ms=excluded.last_processed_close_time_ms,
                last_run_at_ms=excluded.last_run_at_ms,
                total_cycles=excluded.total_cycles,
                last_segment_length=excluded.last_segment_length,
                last_cycle_status=excluded.last_cycle_status,
                last_cycle_detail=excluded.last_cycle_detail,
                open_position_entry_time_ms=excluded.open_position_entry_time_ms,
                open_position_entry_price=excluded.open_position_entry_price,
                open_position_entry_reference_price=excluded.open_position_entry_reference_price,
                open_position_quantity=excluded.open_position_quantity,
                open_position_entry_fee_quote=excluded.open_position_entry_fee_quote
            """,
            (
                last_processed_close_time_ms, last_run_at_ms, total_cycles, last_segment_length,
                last_cycle_status, last_cycle_detail,
                open_position.entry_time_ms if open_position is not None else None,
                str(open_position.entry_price) if open_position is not None else None,
                str(open_position.entry_reference_price) if open_position is not None else None,
                str(open_position.quantity) if open_position is not None else None,
                str(open_position.entry_fee_quote) if open_position is not None else None,
            ),
        )

    @staticmethod
    def _row_to_trade_record(row: tuple) -> ShadowTradeRecord:
        (entry_time_ms, exit_time_ms, entry_price, exit_price, quantity, fees_paid, pnl_quote, exit_reason,
         entry_fee_quote, exit_fee_quote, entry_reference_price, exit_reference_price,
         planned_risk_quote, net_reward_to_risk) = row
        trade = Trade(
            entry_time_ms=entry_time_ms,
            exit_time_ms=exit_time_ms,
            entry_price=Decimal(entry_price),
            exit_price=Decimal(exit_price),
            quantity=Decimal(quantity),
            fees_paid=Decimal(fees_paid),
            pnl_quote=Decimal(pnl_quote),
            exit_reason=exit_reason,
            entry_fee_quote=Decimal(entry_fee_quote),
            exit_fee_quote=Decimal(exit_fee_quote),
            entry_reference_price=Decimal(entry_reference_price),
            exit_reference_price=Decimal(exit_reference_price),
        )
        return ShadowTradeRecord(
            trade=trade,
            planned_risk_quote=Decimal(planned_risk_quote) if planned_risk_quote is not None else None,
            net_reward_to_risk=net_reward_to_risk,
        )
