"""Fetches and stores the MINIMUM causal pre-boundary warm-up history E1
needs (plus a small, explicitly documented safety margin), so `shadow-run`
can begin producing real, trade-affecting evaluations IMMEDIATELY at
`SHADOW_START_BOUNDARY_MS` instead of waiting ~315 days for organic
forward-only accumulation.

WHAT WARM-UP DOES AND DOES NOT DO: warm-up candles feed E1's own indicator
machinery (weekly EMA40 + slope, 4h EMA200 + slope + ATR + Donchian
channel) exactly the same way any other historical candle would -
`research/candidates/multitimeframe_breakout.py` is UNCHANGED and has no
concept of "warm-up" at all; it is a pure function of whatever candles it
is given. What makes a warm-up candle different is entirely EXTERNAL to
E1: `shadow/engine.py` never asks `backtest/engine.py::run_segment` (also
UNCHANGED) to treat a warm-up candle - or the few "settling" candles just
after the boundary, see below - as a decision point at all.

THE SETTLING BUFFER (why "warm-up ends exactly at the boundary" is not,
by itself, enough to satisfy "the first eligible 4h setup must close at
or after the boundary"): `SHADOW_START_BOUNDARY_MS` (2026-09-06T00:00:00Z,
midnight UTC) happens to land exactly on a 4h-candle grid boundary - every
midnight UTC does, since 24h is an exact multiple of 4h. That means the
LAST possible warm-up-only 4h setup closes at exactly one hour before the
boundary, and its confirmation window (`CONFIRMATION_WINDOW_1H_CANDLES`,
E1's own frozen constant, imported here and never redefined) extends up
to `CONFIRMATION_WINDOW_1H_CANDLES` hours PAST the boundary. If
`run_segment`'s own loop started evaluating decisions literally at the
boundary candle, such a setup - one that closed BEFORE the boundary -
could still confirm (and trade) just after it: a "carried-over" setup the
shadow-mode mandate explicitly forbids.

The fix costs nothing in lost opportunity, and is EXACT (not
probabilistic), precisely because the boundary is 4h-grid-aligned: the
first WHOLLY POST-BOUNDARY 4h bucket is `[boundary, boundary+3h]`, closing
at `boundary+3h` - its own confirmation window is
`[boundary+4h, boundary+7h]`, which starts exactly where a
`CONFIRMATION_WINDOW_1H_CANDLES`-hour settling buffer ends. So:
`shadow/engine.py`'s own `min_required` argument to `run_segment` is set
to `warmup_candle_count + CONFIRMATION_WINDOW_1H_CANDLES + 1`
(`effective_min_required_candles`, computed here and stored) rather than
`warmup_candle_count + 1`. The extra `CONFIRMATION_WINDOW_1H_CANDLES`
hours are visible to the strategy as ordinary HISTORY the moment they
exist (so E1's own indicators stay perfectly continuous across the
boundary) but are never themselves evaluated as a decision point by
`run_segment`'s loop - by the time the loop's first real iteration
happens, at the first wholly-post-boundary 4h bucket's own close, EVERY
warm-up-originated setup's confirmation window has fully and
unconditionally elapsed. See `tests/unit/test_shadow_bootstrap.py` and
`tests/unit/test_shadow_engine.py` for the proof, both that this can
never carry a pre-boundary setup across, and that it never costs a single
hour of genuinely post-boundary eligibility.

SAFETY MARGIN (`WARMUP_SAFETY_MARGIN_CANDLES`, 48 - two extra days of 1h
candles): a small, fixed, documented buffer fetched beyond E1's own bare
`min_required_candles`, purely defensive slack (e.g. against an
off-by-one in a future edit here). It does not change, and is not
required for, the settling-buffer guarantee above, which holds for any
warm-up length at all.

FAIL CLOSED ON A WARM-UP GAP: if the fetched warm-up range itself contains
a confirmed gap (`data/gap_detection.py::partition_into_segments`,
unmodified), E1's own weekly/4h aggregation would silently exclude the
straddling buckets (see that module's own "FAIL CLOSED ON GAPS" docstring
section) - meaning "the minimum causal history E1 requires" would not
actually be causally complete at the boundary. Bootstrap therefore refuses
to store anything and reports `BOOTSTRAP_STATUS_GAP_IN_WARMUP_DATA`
instead of silently accepting a warm-up range that cannot back honest
indicators at the boundary. Nothing is written to either database in that
case, or in any other non-OK outcome - a failed bootstrap attempt leaves
no partial state to clean up before trying again.

`shadow-run` refuses to operate until `verify_bootstrap_complete` passes -
see `shadow/engine.py`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from trading_agent.config.models import AppConfig
from trading_agent.data.gap_detection import partition_into_segments
from trading_agent.data.historical_fetch import fetch_historical_range
from trading_agent.data.market_data_public import (
    PRODUCTION_MARKET_DATA_HOST,
    BinancePublicMarketDataClient,
)
from trading_agent.data.models import interval_to_ms
from trading_agent.data.storage import CandleStore
from trading_agent.research.candidates.multitimeframe_breakout import (
    CONFIRMATION_WINDOW_1H_CANDLES,
    MultiTimeframeBreakoutStrategy,
)
from trading_agent.shadow.boundary import SHADOW_START_BOUNDARY_MS, assert_valid_shadow_config
from trading_agent.shadow.store import ShadowBootstrapState, ShadowStore

#: Two extra days of 1h candles beyond E1's own bare `min_required_candles`
#: - see module docstring's "SAFETY MARGIN" section.
WARMUP_SAFETY_MARGIN_CANDLES = 48

BOOTSTRAP_STATUS_OK = "OK"
BOOTSTRAP_STATUS_ALREADY_BOOTSTRAPPED = "ALREADY_BOOTSTRAPPED"
BOOTSTRAP_STATUS_GAP_IN_WARMUP_DATA = "GAP_IN_WARMUP_DATA"
BOOTSTRAP_STATUS_INSUFFICIENT_HISTORY_AVAILABLE = "INSUFFICIENT_HISTORY_AVAILABLE"


def compute_warmup_candle_count(strategy_min_required_candles: int) -> int:
    """The number of warm-up-only 1h candles to fetch: exactly enough that,
    together with the boundary candle itself, E1's own `generate_signal`
    already has `>= strategy_min_required_candles` candles to work with the
    very first time it is ever called (see module docstring) - plus the
    fixed safety margin."""
    return strategy_min_required_candles - 1 + WARMUP_SAFETY_MARGIN_CANDLES


def compute_effective_min_required_candles(warmup_candle_count: int) -> int:
    """The `min_required` value `shadow/engine.py` passes to `run_segment`
    for the bootstrap-anchored segment - see the module docstring's
    "THE SETTLING BUFFER" section for exactly why this is
    `CONFIRMATION_WINDOW_1H_CANDLES` candles larger than
    `warmup_candle_count + 1`."""
    return warmup_candle_count + CONFIRMATION_WINDOW_1H_CANDLES + 1


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    status: str
    detail: str
    warmup_candle_count: int
    warmup_start_time_ms: int | None
    warmup_end_time_ms: int | None
    effective_min_required_candles: int


def run_shadow_bootstrap(config: AppConfig) -> BootstrapResult:
    """Fetch and store the warm-up range once. Safe to call again after a
    successful bootstrap - reports `ALREADY_BOOTSTRAPPED` and touches
    nothing (never re-fetches, never overwrites an existing bootstrap with
    a different range). On any failure (gap, short history), nothing is
    written to either database - see module docstring.
    """
    assert_valid_shadow_config(config)

    symbol = config.market.symbol
    interval = config.market.interval
    step_ms = interval_to_ms(interval)

    strategy = MultiTimeframeBreakoutStrategy()
    warmup_candle_count = compute_warmup_candle_count(strategy.min_required_candles)
    effective_min_required_candles = compute_effective_min_required_candles(warmup_candle_count)
    warmup_start_ms = SHADOW_START_BOUNDARY_MS - warmup_candle_count * step_ms
    warmup_last_open_time_ms = SHADOW_START_BOUNDARY_MS - step_ms

    with ShadowStore(config.paths.db_path) as shadow_store:
        existing = shadow_store.get_bootstrap_state()
        if existing is not None:
            return BootstrapResult(
                status=BOOTSTRAP_STATUS_ALREADY_BOOTSTRAPPED,
                detail=(
                    f"already bootstrapped: {existing.warmup_candle_count} warm-up candle(s) from "
                    f"{existing.warmup_start_time_ms} through {existing.warmup_last_open_time_ms} "
                    "(open time) - shadow-bootstrap never re-fetches or overwrites an existing bootstrap."
                ),
                warmup_candle_count=existing.warmup_candle_count,
                warmup_start_time_ms=existing.warmup_start_time_ms,
                warmup_end_time_ms=existing.warmup_last_open_time_ms,
                effective_min_required_candles=existing.effective_min_required_candles,
            )

        client = BinancePublicMarketDataClient(PRODUCTION_MARKET_DATA_HOST)
        # Every warm-up candle closes strictly before the boundary by
        # construction, so the boundary itself is a valid, already-known
        # "reference time" for completeness filtering - no separate
        # `/api/v3/time` call is needed for this fetch.
        fetch_result = fetch_historical_range(
            client, symbol, interval, warmup_start_ms, SHADOW_START_BOUNDARY_MS,
            reference_time_ms=SHADOW_START_BOUNDARY_MS,
        )
        candles = fetch_result.candles

        if not candles:
            return BootstrapResult(
                status=BOOTSTRAP_STATUS_INSUFFICIENT_HISTORY_AVAILABLE,
                detail=f"no candles were returned for the warm-up range [{warmup_start_ms}, {SHADOW_START_BOUNDARY_MS}).",
                warmup_candle_count=0, warmup_start_time_ms=None, warmup_end_time_ms=None,
                effective_min_required_candles=effective_min_required_candles,
            )

        segmentation = partition_into_segments(candles, interval)
        if segmentation.gaps:
            return BootstrapResult(
                status=BOOTSTRAP_STATUS_GAP_IN_WARMUP_DATA,
                detail=(
                    f"{len(segmentation.gaps)} confirmed gap(s) in the warm-up range - E1's weekly/4h "
                    "aggregation cannot produce valid indicators at the boundary with a gap present. "
                    "Nothing was stored. Investigate the gap(s) before retrying: "
                    + "; ".join(
                        f"missing {g.missing_intervals} interval(s) between "
                        f"{g.previous_open_time_ms} and {g.next_open_time_ms}"
                        for g in segmentation.gaps
                    )
                ),
                warmup_candle_count=0, warmup_start_time_ms=None, warmup_end_time_ms=None,
                effective_min_required_candles=effective_min_required_candles,
            )

        if (
            len(candles) != warmup_candle_count
            or candles[0].open_time_ms != warmup_start_ms
            or candles[-1].open_time_ms != warmup_last_open_time_ms
        ):
            return BootstrapResult(
                status=BOOTSTRAP_STATUS_INSUFFICIENT_HISTORY_AVAILABLE,
                detail=(
                    f"expected exactly {warmup_candle_count} contiguous candle(s) covering "
                    f"[{warmup_start_ms}, {SHADOW_START_BOUNDARY_MS}) (open times "
                    f"{warmup_start_ms}..{warmup_last_open_time_ms}); got {len(candles)} candle(s) "
                    f"spanning {candles[0].open_time_ms}..{candles[-1].open_time_ms}. Nothing was stored."
                ),
                warmup_candle_count=0, warmup_start_time_ms=None, warmup_end_time_ms=None,
                effective_min_required_candles=effective_min_required_candles,
            )

        with CandleStore(config.paths.db_path) as candle_store:
            candle_store.upsert_candles(candles)

        now_ms = int(time.time() * 1000)
        source = f"shadow-bootstrap:{PRODUCTION_MARKET_DATA_HOST}"
        shadow_store.record_bootstrap_atomically(
            candles, warmup_start_ms, warmup_last_open_time_ms, warmup_candle_count,
            effective_min_required_candles, now_ms, source,
        )

        return BootstrapResult(
            status=BOOTSTRAP_STATUS_OK,
            detail=(
                f"stored {warmup_candle_count} warm-up-only candle(s) from {warmup_start_ms} through "
                f"{warmup_last_open_time_ms} (open time). shadow-run can now operate starting at the "
                f"fixed boundary ({SHADOW_START_BOUNDARY_MS}) plus a {CONFIRMATION_WINDOW_1H_CANDLES}-hour "
                "settling buffer, instead of waiting ~315 days for organic accumulation."
            ),
            warmup_candle_count=warmup_candle_count,
            warmup_start_time_ms=warmup_start_ms,
            warmup_end_time_ms=warmup_last_open_time_ms,
            effective_min_required_candles=effective_min_required_candles,
        )


@dataclass(frozen=True, slots=True)
class BootstrapVerification:
    ok: bool
    reason: str
    state: ShadowBootstrapState | None


def verify_bootstrap_complete(
    config: AppConfig, candle_store: CandleStore, shadow_store: ShadowStore
) -> BootstrapVerification:
    """A REAL verification, not a cached-flag read: re-derives, from the
    candles actually stored in `candle_store` right now, that the recorded
    warm-up range is still exactly present and gap-free. Called by
    `shadow/engine.py::run_shadow_cycle` before any network access on
    every single cycle - see that module.
    """
    state = shadow_store.get_bootstrap_state()
    if state is None:
        return BootstrapVerification(
            False, "shadow mode has not been bootstrapped yet - run `shadow-bootstrap` first.", None
        )

    symbol, interval = config.market.symbol, config.market.interval
    candles = candle_store.get_candles(
        symbol, interval, start_time_ms=state.warmup_start_time_ms, end_time_ms=state.warmup_last_open_time_ms
    )
    if len(candles) != state.warmup_candle_count:
        return BootstrapVerification(
            False,
            f"expected {state.warmup_candle_count} stored warm-up candle(s), found {len(candles)} - "
            "warm-up data may be missing or corrupted; re-run shadow-bootstrap.",
            state,
        )
    if candles[0].open_time_ms != state.warmup_start_time_ms or candles[-1].open_time_ms != state.warmup_last_open_time_ms:
        return BootstrapVerification(
            False, "stored warm-up candle range no longer matches the recorded bootstrap boundaries.", state
        )
    segmentation = partition_into_segments(candles, interval)
    if segmentation.gaps:
        return BootstrapVerification(
            False, f"{len(segmentation.gaps)} confirmed gap(s) found in previously-bootstrapped warm-up data.", state
        )
    return BootstrapVerification(True, "bootstrap verified.", state)
