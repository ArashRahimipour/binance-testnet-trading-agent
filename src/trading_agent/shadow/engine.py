"""Orchestrates one forward-only shadow-mode cycle for the frozen
`multitimeframe_breakout_E1_round3` candidate.

DESIGN: `run_shadow_cycle` is deliberately a "recompute everything, every
time" design, not an incremental/hand-rolled state machine. Every cycle:

  1. Checks the shadow kill switch FIRST (before any fetch, lock, or
     processing) - if engaged, the entire cycle short-circuits. This is a
     coarser "pause everything" semantic than `execution/live_runner.py`'s
     per-order `RiskContext.kill_switch_engaged` gating (which still lets a
     cycle run and only blocks the order itself); a finer-grained gate here
     would require modifying `backtest/engine.py::run_segment`, which is
     forbidden - see `RISK_POLICY.md`-style reasoning applied to shadow mode.
  2. Acquires `shadow/lock.py::ShadowLock` (never blocks - a second
     concurrent invocation fails immediately with `ShadowLockError`).
  3. Fetches only NEW completed 1h candles via `data/ingestion.py::
     fetch_completed_candles` against the read-only public Binance client,
     floored at `max(SHADOW_START_BOUNDARY_MS, latest_stored_close_ms + 1)`
     - it is structurally impossible for this to request, and
     `shadow/boundary.py::assert_no_pre_boundary_candles` additionally
     proves, that no candle before the fixed shadow start boundary is ever
     fetched, stored, or scored.
  4. Upserts new candles into `data/storage.py::CandleStore` (unmodified),
     pointed at `data/shadow_agent.db`.
  5. Re-reads the ENTIRE stored shadow candle history from the boundary
     onward, partitions it into gap-free segments
     (`data/gap_detection.py::partition_into_segments`, unmodified), and
     evaluates ONLY the latest segment - exactly the same "segment" gap
     policy every other part of this project already uses. A gap
     restarts E1's own warm-up clock from the post-gap segment; earlier
     segments' already-persisted trades/equity are never touched or
     recomputed, they simply stop growing (see `shadow/report.py` for how
     this is disclosed).
  6. If the latest segment is shorter than E1's own
     `min_required_candles` (~315 days of 1h candles), reports
     INSUFFICIENT_DATA and does nothing further this cycle - exactly the
     same caller-side guard `backtest/engine.py::run_backtest` and
     `run_independent_holdout_evaluation` already use before ever calling
     `run_segment` (which requires `len(segment) >= min_required`).
  7. Otherwise calls `backtest/engine.py::run_segment` - UNMODIFIED,
     the same simulation core research/round3_report.py uses for E1 itself
     - over the FULL latest segment, with a fresh, throwaway
     `Journal(":memory:")` to capture this cycle's own signal/decision log
     without ever polluting a persistent journal with duplicate entries on
     each full recompute. `use_fixed_risk_reward_policy=True` matches E1's
     own already-evaluated risk policy (net R/R >= 2.0, 1% risk budget)
     exactly.
  8. Diffs the freshly computed trades/equity/journal entries against the
     stored high-water mark (`shadow/store.py::ShadowStore`) and persists
     only the NEW rows, atomically, along with the current open position
     (if any) and the advanced high-water mark - see `shadow/store.py`'s
     own module docstring for why this makes the whole cycle idempotent
     and crash recoverable, and guarantees a completed candle is never
     scored twice into the persisted record.

Nothing in this module (or anywhere else in `shadow/`) ever imports
`execution/testnet_adapter.py` or constructs a client for any host other
than the public, unauthenticated market-data endpoints
`data/market_data_public.py::ALLOWED_HOSTS` already restricts requests to -
see `tests/unit/test_shadow_engine.py`'s source-level regression lock. No
order, real or simulated-against-a-live-book, is ever submitted.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from trading_agent.backtest.engine import run_segment
from trading_agent.config.models import AppConfig, Mode
from trading_agent.data.gap_detection import partition_into_segments
from trading_agent.data.ingestion import fetch_completed_candles
from trading_agent.data.market_data_public import (
    PRODUCTION_MARKET_DATA_HOST,
    BinancePublicMarketDataClient,
)
from trading_agent.data.storage import CandleStore
from trading_agent.execution.backtest_broker import BacktestBroker
from trading_agent.journal.journal import Journal
from trading_agent.research.candidates.multitimeframe_breakout import (
    MultiTimeframeBreakoutStrategy,
)
from trading_agent.risk.engine import RiskEngine
from trading_agent.risk.kill_switch import KillSwitch
from trading_agent.shadow.boundary import (
    SHADOW_START_BOUNDARY_MS,
    assert_no_pre_boundary_candles,
    filter_from_boundary,
)
from trading_agent.shadow.lock import ShadowLock
from trading_agent.shadow.store import ShadowStore, ShadowTradeRecord
from trading_agent.sizing.exchange_filters import SymbolFilters

REQUIRED_SHADOW_INTERVAL = "1h"

SHADOW_KILL_SWITCH_FILENAME = "SHADOW_KILL_SWITCH"
SHADOW_LOCK_FILENAME = "shadow.lock"

SHADOW_STATUS_KILL_SWITCH_ENGAGED = "KILL_SWITCH_ENGAGED"
SHADOW_STATUS_NO_NEW_CANDLES = "NO_NEW_CANDLES"
SHADOW_STATUS_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
SHADOW_STATUS_OK = "OK"


class ShadowConfigError(Exception):
    """Raised when `run_shadow_cycle` is called with a config that is not
    valid for shadow mode (wrong `mode`, or a `market.interval` other than
    the fixed "1h" E1 itself requires)."""


def shadow_kill_switch_path(config: AppConfig) -> Path:
    return config.paths.data_dir / SHADOW_KILL_SWITCH_FILENAME


def shadow_lock_path(config: AppConfig) -> Path:
    return config.paths.data_dir / SHADOW_LOCK_FILENAME


@dataclass(frozen=True, slots=True)
class ShadowCycleResult:
    status: str
    detail: str
    new_candles_fetched: int
    segment_length: int | None
    min_required_candles: int
    new_trades_persisted: int
    new_equity_points_persisted: int
    new_journal_entries_persisted: int


def run_shadow_cycle(config: AppConfig) -> ShadowCycleResult:
    """Run exactly one shadow cycle. Intended to be invoked once per
    completed 1h candle (e.g. by an external scheduler) - see this
    module's own docstring. Never places an order of any kind.
    """
    if config.mode != Mode.SHADOW:
        raise ShadowConfigError("run_shadow_cycle requires config.mode == Mode.SHADOW")
    if config.market.interval != REQUIRED_SHADOW_INTERVAL:
        raise ShadowConfigError(
            f"shadow mode requires market.interval == {REQUIRED_SHADOW_INTERVAL!r}, "
            f"got {config.market.interval!r}"
        )

    min_required_candles = MultiTimeframeBreakoutStrategy().min_required_candles

    kill_switch = KillSwitch(shadow_kill_switch_path(config))
    if kill_switch.is_engaged():
        return ShadowCycleResult(
            status=SHADOW_STATUS_KILL_SWITCH_ENGAGED,
            detail=f"shadow kill switch is engaged: {kill_switch.reason()}",
            new_candles_fetched=0,
            segment_length=None,
            min_required_candles=min_required_candles,
            new_trades_persisted=0,
            new_equity_points_persisted=0,
            new_journal_entries_persisted=0,
        )

    with ShadowLock(shadow_lock_path(config)):
        return _run_shadow_cycle_locked(config, min_required_candles)


def _run_shadow_cycle_locked(config: AppConfig, min_required_candles: int) -> ShadowCycleResult:
    symbol = config.market.symbol
    interval = config.market.interval
    now_ms = int(time.time() * 1000)

    with (
        CandleStore(config.paths.db_path) as candle_store,
        ShadowStore(config.paths.db_path) as shadow_store,
    ):
        client = BinancePublicMarketDataClient(PRODUCTION_MARKET_DATA_HOST)

        latest_stored_close_ms = candle_store.latest_close_time_ms(symbol, interval)
        fetch_start_ms = SHADOW_START_BOUNDARY_MS
        if latest_stored_close_ms is not None:
            fetch_start_ms = max(SHADOW_START_BOUNDARY_MS, latest_stored_close_ms + 1)

        new_candles = fetch_completed_candles(client, symbol, interval, start_time_ms=fetch_start_ms)
        new_candles = filter_from_boundary(new_candles)
        assert_no_pre_boundary_candles(new_candles)
        if new_candles:
            candle_store.upsert_candles(new_candles)

        all_candles = candle_store.get_candles(symbol, interval, start_time_ms=SHADOW_START_BOUNDARY_MS)

        if not all_candles:
            detail = "no shadow candles available yet."
            shadow_store.touch_cycle(now_ms, SHADOW_STATUS_NO_NEW_CANDLES, detail, None)
            return ShadowCycleResult(
                SHADOW_STATUS_NO_NEW_CANDLES, detail, len(new_candles), None, min_required_candles, 0, 0, 0
            )

        run_state = shadow_store.get_run_state()
        latest_stored_close_after_fetch = all_candles[-1].close_time_ms
        if (
            run_state.last_processed_close_time_ms is not None
            and latest_stored_close_after_fetch == run_state.last_processed_close_time_ms
        ):
            detail = "no new completed candle since the last processed cycle."
            shadow_store.touch_cycle(now_ms, SHADOW_STATUS_NO_NEW_CANDLES, detail, run_state.last_segment_length)
            return ShadowCycleResult(
                SHADOW_STATUS_NO_NEW_CANDLES, detail, len(new_candles), run_state.last_segment_length,
                min_required_candles, 0, 0, 0,
            )

        segmentation = partition_into_segments(all_candles, interval)
        latest_segment = segmentation.segments[-1]

        if len(latest_segment) < min_required_candles:
            detail = (
                f"accumulating warm-up: {len(latest_segment)}/{min_required_candles} completed 1h candles "
                "in the current gap-free segment - E1 cannot generate a signal yet."
            )
            shadow_store.touch_cycle(now_ms, SHADOW_STATUS_INSUFFICIENT_DATA, detail, len(latest_segment))
            return ShadowCycleResult(
                SHADOW_STATUS_INSUFFICIENT_DATA, detail, len(new_candles), len(latest_segment),
                min_required_candles, 0, 0, 0,
            )

        filters = SymbolFilters.from_exchange_info(client.get_exchange_info(symbol))
        risk_engine = RiskEngine(config.risk)
        broker = BacktestBroker(config.fees)
        starting_equity = Decimal(str(config.backtest.starting_equity))
        journal = Journal(":memory:")
        strategy = MultiTimeframeBreakoutStrategy()

        result = run_segment(
            latest_segment, config, filters, strategy, risk_engine, broker, journal,
            min_required_candles, starting_equity, use_fixed_risk_reward_policy=True,
        )

        last_processed = run_state.last_processed_close_time_ms
        threshold = last_processed if last_processed is not None else -1

        rr = result.risk_reward
        trade_records = []
        for i, trade in enumerate(result.trades):
            planned_risk = (
                rr.planned_risk_quote_values[i]
                if rr is not None and i < len(rr.planned_risk_quote_values)
                else None
            )
            net_rr = (
                rr.net_reward_to_risk_values[i]
                if rr is not None and i < len(rr.net_reward_to_risk_values)
                else None
            )
            trade_records.append(
                ShadowTradeRecord(trade=trade, planned_risk_quote=planned_risk, net_reward_to_risk=net_rr)
            )
        new_trade_records = [r for r in trade_records if r.trade.exit_time_ms > threshold]
        new_equity_points = [p for p in result.equity_curve if p.timestamp_ms > threshold]
        new_journal_entries = [e for e in journal.all_entries() if e["timestamp_ms"] > threshold]

        new_last_processed = latest_segment[-1].close_time_ms
        detail = (
            f"processed {len(latest_segment)} candles in the current segment; "
            f"{len(new_trade_records)} new closed trade(s)."
        )
        shadow_store.record_cycle_atomically(
            new_trade_records, new_equity_points, new_journal_entries, new_last_processed,
            result.open_position, now_ms, len(latest_segment), SHADOW_STATUS_OK, detail,
        )
        return ShadowCycleResult(
            SHADOW_STATUS_OK, detail, len(new_candles), len(latest_segment), min_required_candles,
            len(new_trade_records), len(new_equity_points), len(new_journal_entries),
        )
