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
  3. Verifies `shadow/bootstrap.py::verify_bootstrap_complete` BEFORE any
     network access - `shadow-run` refuses to operate (`NOT_BOOTSTRAPPED`/
     `BOOTSTRAP_INVALID`, zero Binance calls) until `shadow-bootstrap` has
     successfully stored E1's causal pre-boundary warm-up history. See
     `shadow/bootstrap.py`'s module docstring for what that warm-up range
     is and why it also includes a small post-boundary "settling buffer".
  4. Fetches only NEW completed 1h candles via `data/ingestion.py::
     fetch_completed_candles` against the read-only public Binance client,
     floored at `max(SHADOW_START_BOUNDARY_MS, latest_stored_close_ms + 1)`
     - it is structurally impossible for this REGULAR (non-bootstrap) fetch
     to request, and `shadow/boundary.py::assert_no_pre_boundary_candles`
     additionally proves, that no candle before the fixed shadow start
     boundary is ever fetched THIS way, stored, or scored. (Warm-up
     candles are fetched exactly once, by `shadow/bootstrap.py`, through a
     completely separate, explicitly pre-boundary code path - see there.)
  5. Upserts new candles into `data/storage.py::CandleStore` (unmodified),
     pointed at `data/shadow_agent.db`.
  6. Re-reads the ENTIRE stored shadow candle history from the verified
     warm-up start (not the boundary - warm-up and forward candles must be
     read together as one contiguous list), partitions it into gap-free
     segments (`data/gap_detection.py::partition_into_segments`,
     unmodified), and evaluates ONLY the latest segment - exactly the same
     "segment" gap policy every other part of this project already uses.
     A gap AFTER bootstrap starts an independent new segment with no
     warm-up context of its own (organic accumulation resumes from
     scratch, using E1's own plain `min_required_candles` - the settling-
     buffer machinery only ever applies to the ONE segment that begins
     exactly at the bootstrapped warm-up start); earlier segments'
     already-persisted trades/equity are never touched or recomputed, they
     simply stop growing (see `shadow/report.py` for how this is
     disclosed).
  7. If the latest segment is shorter than the required length for ITS
     OWN kind (the bootstrap-anchored segment's `effective_min_required_
     candles`, or a later organic segment's plain `min_required_candles`),
     reports INSUFFICIENT_DATA and does nothing further this cycle -
     exactly the same caller-side guard `backtest/engine.py::run_backtest`
     and `run_independent_holdout_evaluation` already use before ever
     calling `run_segment` (which requires `len(segment) >= min_required`).
  8. Otherwise calls `backtest/engine.py::run_segment` - UNMODIFIED,
     the same simulation core research/round3_report.py uses for E1 itself
     - over the FULL latest segment, with a fresh, throwaway
     `Journal(":memory:")` to capture this cycle's own signal/decision log
     without ever polluting a persistent journal with duplicate entries on
     each full recompute. `use_fixed_risk_reward_policy=True` matches E1's
     own already-evaluated risk policy (net R/R >= 2.0, 1% risk budget)
     exactly.
  9. Diffs the freshly computed trades/equity/journal entries against the
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
from trading_agent.config.models import AppConfig
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
from trading_agent.shadow.bootstrap import verify_bootstrap_complete
from trading_agent.shadow.boundary import (
    REQUIRED_SHADOW_INTERVAL,
    SHADOW_START_BOUNDARY_MS,
    ShadowConfigError,
    assert_no_pre_boundary_candles,
    assert_valid_shadow_config,
    filter_from_boundary,
)
from trading_agent.shadow.lock import ShadowLock
from trading_agent.shadow.store import ShadowStore, ShadowTradeRecord
from trading_agent.sizing.exchange_filters import SymbolFilters

__all__ = [
    "REQUIRED_SHADOW_INTERVAL",
    "ShadowConfigError",
    "ShadowCycleResult",
    "run_shadow_cycle",
    "shadow_kill_switch_path",
    "shadow_lock_path",
]

SHADOW_KILL_SWITCH_FILENAME = "SHADOW_KILL_SWITCH"
SHADOW_LOCK_FILENAME = "shadow.lock"

SHADOW_STATUS_KILL_SWITCH_ENGAGED = "KILL_SWITCH_ENGAGED"
SHADOW_STATUS_NOT_BOOTSTRAPPED = "NOT_BOOTSTRAPPED"
SHADOW_STATUS_BOOTSTRAP_INVALID = "BOOTSTRAP_INVALID"
SHADOW_STATUS_NO_NEW_CANDLES = "NO_NEW_CANDLES"
SHADOW_STATUS_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
SHADOW_STATUS_OK = "OK"


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
    module's own docstring. Never places an order of any kind. Refuses to
    operate (`NOT_BOOTSTRAPPED`/`BOOTSTRAP_INVALID`, zero network calls)
    until `shadow-bootstrap` has successfully run - see
    `shadow/bootstrap.py`.
    """
    assert_valid_shadow_config(config)

    # A cheap, informational default for the two early-return paths below
    # (kill switch, bootstrap not yet verified) that never reach the point
    # where the bootstrap-adjusted effective value is known.
    raw_min_required_candles = MultiTimeframeBreakoutStrategy().min_required_candles

    kill_switch = KillSwitch(shadow_kill_switch_path(config))
    if kill_switch.is_engaged():
        return ShadowCycleResult(
            status=SHADOW_STATUS_KILL_SWITCH_ENGAGED,
            detail=f"shadow kill switch is engaged: {kill_switch.reason()}",
            new_candles_fetched=0,
            segment_length=None,
            min_required_candles=raw_min_required_candles,
            new_trades_persisted=0,
            new_equity_points_persisted=0,
            new_journal_entries_persisted=0,
        )

    with ShadowLock(shadow_lock_path(config)):
        return _run_shadow_cycle_locked(config, raw_min_required_candles)


def _run_shadow_cycle_locked(config: AppConfig, raw_min_required_candles: int) -> ShadowCycleResult:
    symbol = config.market.symbol
    interval = config.market.interval
    now_ms = int(time.time() * 1000)

    with (
        CandleStore(config.paths.db_path) as candle_store,
        ShadowStore(config.paths.db_path) as shadow_store,
    ):
        # Verified BEFORE any network access - see module docstring point 3.
        verification = verify_bootstrap_complete(config, candle_store, shadow_store)
        if not verification.ok:
            status = (
                SHADOW_STATUS_NOT_BOOTSTRAPPED if verification.state is None else SHADOW_STATUS_BOOTSTRAP_INVALID
            )
            detail = f"{verification.reason} Run `shadow-bootstrap` before `shadow-run`."
            return ShadowCycleResult(
                status, detail, 0, None, raw_min_required_candles, 0, 0, 0
            )
        bootstrap_state = verification.state
        assert bootstrap_state is not None  # verification.ok implies this

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

        # Read warm-up + forward candles together as ONE contiguous list -
        # `run_segment` needs them concatenated to see warm-up history at
        # all; see `shadow/bootstrap.py`'s "THE SETTLING BUFFER" section.
        all_candles = candle_store.get_candles(symbol, interval, start_time_ms=bootstrap_state.warmup_start_time_ms)

        if not all_candles:  # pragma: no cover - bootstrap guarantees this is non-empty
            detail = "no shadow candles available yet."
            shadow_store.touch_cycle(now_ms, SHADOW_STATUS_NO_NEW_CANDLES, detail, None)
            return ShadowCycleResult(
                SHADOW_STATUS_NO_NEW_CANDLES, detail, len(new_candles), None, raw_min_required_candles, 0, 0, 0
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
                raw_min_required_candles, 0, 0, 0,
            )

        segmentation = partition_into_segments(all_candles, interval)
        latest_segment = segmentation.segments[-1]

        # The settling-buffer-adjusted requirement applies ONLY to the one
        # segment that still begins exactly at the bootstrapped warm-up
        # start; any later segment (born from a gap in forward data) has
        # no warm-up context of its own and falls back to E1's own plain
        # minimum - see module docstring point 6.
        is_bootstrap_anchored_segment = latest_segment[0].open_time_ms == bootstrap_state.warmup_start_time_ms
        required_len = (
            bootstrap_state.effective_min_required_candles if is_bootstrap_anchored_segment
            else raw_min_required_candles
        )

        if len(latest_segment) < required_len:
            detail = (
                f"accumulating warm-up/settling buffer: {len(latest_segment)}/{required_len} completed 1h "
                "candles in the current gap-free segment - E1 cannot generate an eligible decision yet."
            )
            shadow_store.touch_cycle(now_ms, SHADOW_STATUS_INSUFFICIENT_DATA, detail, len(latest_segment))
            return ShadowCycleResult(
                SHADOW_STATUS_INSUFFICIENT_DATA, detail, len(new_candles), len(latest_segment),
                required_len, 0, 0, 0,
            )

        filters = SymbolFilters.from_exchange_info(client.get_exchange_info(symbol))
        risk_engine = RiskEngine(config.risk)
        broker = BacktestBroker(config.fees)
        starting_equity = Decimal(str(config.backtest.starting_equity))
        journal = Journal(":memory:")
        strategy = MultiTimeframeBreakoutStrategy()

        result = run_segment(
            latest_segment, config, filters, strategy, risk_engine, broker, journal,
            required_len, starting_equity, use_fixed_risk_reward_policy=True,
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
            SHADOW_STATUS_OK, detail, len(new_candles), len(latest_segment), required_len,
            len(new_trade_records), len(new_equity_points), len(new_journal_entries),
        )
