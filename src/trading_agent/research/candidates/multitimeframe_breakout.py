"""Candidate family E (ROUND 3): a multi-timeframe breakout - weekly
regime context, D1/B1's own 4h breakout+regime setup, and a 1h
confirmation/fill layer.

This is a RESULT-INFORMED round-3 hypothesis, examined after round 1's
nine candidates (all REJECTED/INSUFFICIENT_EVIDENCE) and round 2's
`breakout_regime_D1_round2` (OFFICIAL REJECTED verdict, already observed
and PRESERVED UNCHANGED - see `research/candidate_registry_round3.py`).

STRUCTURE (fixed, declared before any Round 3 result is ever inspected):

1. WEEKLY CONTEXT (regime gate): BUY is prohibited unless the last
   COMPLETED weekly candle's close is above a 40-period weekly EMA, AND
   that EMA is itself higher than its own value 4 completed weekly
   candles earlier (a rising long-term weekly average) - the exact same
   "price above a rising long-term average" shape as `research/
   candidates/breakout_regime_gate.py`'s own 4h EMA200 gate, just applied
   one timeframe higher and with declared weekly parameters
   (`WEEKLY_EMA_PERIOD`, `WEEKLY_EMA_SLOPE_LOOKBACK_CANDLES`).
2. 4H SETUP: `VolatilityNormalizedBreakoutStrategy`'s own causal Donchian
   breakout (channel_period=20, atr_period=14, breakout_atr_multiple=0.25)
   PLUS a completed 4h close above a rising 4h EMA-200 - IDENTICAL
   parameters and arithmetic to `research/candidates/breakout_regime_gate.py`
   (D1/B1's own logic), computed here via the SAME shared, already-causal
   `indicators.moving_averages.ema` / `indicators.volatility.atr` /
   `indicators.volatility.rolling_max` primitives (never a reimplemented
   formula) - see `_scan_setup_events` and
   `test_research_candidates_multitimeframe_breakout.py`'s dedicated
   equivalence proof against `BreakoutWithBullishRegimeGateStrategy`
   itself. A confirmed 4h setup ARMS an entry opportunity for exactly the
   next `CONFIRMATION_WINDOW_1H_CANDLES` (4) completed 1h candles, then
   EXPIRES - it can never be renewed without a fresh, independently
   causal 4h setup.
3. 1H CONFIRMATION: within an armed window, the FIRST completed 1h candle
   whose close is both (a) above the triggering 4h setup's own breakout
   level and (b) above its own open, produces the ONE entry this setup
   will ever produce (deterministically identified as the first such
   candle within the window - exactly the same "one entry per cycle,
   identified as a pure function of price history" pattern `research/
   candidates/trend_regime.py` already establishes, generalized here to a
   4-candle window instead of a single confirmation candle). Any LATER
   candle within the same window, even if it also satisfies the raw
   condition, sees the setup already consumed and reports HOLD - it never
   emits a second BUY for the same setup.

CAUSALITY: `generate_signal` is a pure function of `(candles, current_
position)`, exactly like every other candidate in this codebase - no
persisted mutable state affects its returned Signal. Weekly and 4h
candles are never fetched or stored separately; they are DERIVED, on
every call, purely by aggregating the SAME already-causal, already
gap-validated 1h `candles` this call receives (see `_aggregate_completed_
buckets`) - a higher-timeframe bucket can only ever exist in that
aggregation once EVERY ONE of its constituent 1h candles is already
present in `candles`, which is exactly what "only completed weekly/4h
candles, never leaking a partial one" means mechanically. `_scan_setup_
events`/the weekly filter therefore structurally cannot see a
higher-timeframe candle whose own close lies after the current 1h decision
timestamp - there is no separate code path to gate here, unlike D1's own
single-timeframe gate.

FAIL CLOSED ON GAPS/MISALIGNMENT: `_aggregate_completed_buckets` only
ever includes a bucket that has EXACTLY its expected number of hourly
candles, each spaced by precisely one hour, starting exactly on its own
grid boundary - a real gap or a misaligned run of candles simply produces
no bucket for that stretch (never a fabricated, partially-covered one).
In the actual evaluation pipeline this is largely already guaranteed
upstream (`data/gap_detection.py::partition_into_segments` never lets a
gap appear WITHIN the `candles` this strategy ever sees - a gap always
starts a new, independently warmed-up segment/block, exactly like every
other candidate), but the check here is unconditional and defensive: it
holds even if a caller feeds this module hand-built, deliberately gapped
or misaligned data directly (see the dedicated aggregation tests).

RISK POLICY: unchanged - this candidate participates in the SAME fixed
1:2 (net, cost-adjusted) planned reward/risk policy every other round-1/
round-2 candidate does (`backtest/risk_reward.py`, wired in identically
via `use_fixed_risk_reward_policy=True`). This module never computes a
stop, target, quantity, fee, or slippage value of any kind - it only ever
returns a BUY or HOLD `Signal`; `backtest/engine.py` (unmodified) decides
everything about sizing, protection, and next-open execution exactly as
it does for every other candidate.

`weekly_filter_rejections`/`four_h_setups_detected`/`setups_armed`/
`setups_expired`/`one_h_confirmations`/`entries` are READ-ONLY,
decision-irrelevant instance counters, exactly the same established
pattern as `research/candidates/breakout_regime_gate.py`'s own gate
counters - provided purely for `research/round3_report.py`'s diagnostics,
never consulted by `generate_signal`'s own decision.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from trading_agent.data.models import Candle
from trading_agent.indicators.moving_averages import ema
from trading_agent.indicators.volatility import atr, rolling_max
from trading_agent.strategy.base import InsufficientDataError, PositionSide, Signal, SignalType

FAMILY = "multitimeframe_breakout_round3"

_HOUR_MS = 60 * 60 * 1000
_FOUR_H_HOURS = 4
_WEEK_HOURS = 7 * 24
_FOUR_H_MS = _FOUR_H_HOURS * _HOUR_MS
_WEEK_MS = _WEEK_HOURS * _HOUR_MS
#: 1970-01-01T00:00:00Z (epoch) was a Thursday, 3 days after the preceding
#: Monday - used to align weekly buckets to Monday 00:00 UTC (matching the
#: conventional weekly-candle boundary), never to an arbitrary epoch-mod grid.
_EPOCH_MONDAY_OFFSET_MS = 3 * 24 * _HOUR_MS

#: Declared, fixed BEFORE this candidate was ever scored - never tuned
#: after seeing an E1 result.
WEEKLY_EMA_PERIOD = 40
WEEKLY_EMA_SLOPE_LOOKBACK_CANDLES = 4

#: IDENTICAL to research/candidates/breakout_regime_gate.py's own D1/B1
#: parameters - see module docstring.
FOUR_H_CHANNEL_PERIOD = 20
FOUR_H_ATR_PERIOD = 14
FOUR_H_BREAKOUT_ATR_MULTIPLE = 0.25
FOUR_H_EMA_REGIME_PERIOD = 200
FOUR_H_EMA_SLOPE_LOOKBACK_CANDLES = 20

CONFIRMATION_WINDOW_1H_CANDLES = 4

#: One full extra bucket of alignment margin on each timeframe, so an
#: arbitrary (non-grid-aligned) start-of-history never silently steals a
#: full period's worth of required warm-up.
_FOUR_H_ALIGNMENT_MARGIN_HOURS = _FOUR_H_HOURS
_WEEKLY_ALIGNMENT_MARGIN_HOURS = _WEEK_HOURS


def _four_h_bucket_start_ms(open_time_ms: int) -> int:
    return open_time_ms - (open_time_ms % _FOUR_H_MS)


def _weekly_bucket_start_ms(open_time_ms: int) -> int:
    shifted = open_time_ms + _EPOCH_MONDAY_OFFSET_MS
    return shifted - (shifted % _WEEK_MS) - _EPOCH_MONDAY_OFFSET_MS


@dataclass(frozen=True, slots=True)
class _CompletedBucket:
    candle: Candle
    #: Index, into the ORIGINAL 1h candle list this bucket was aggregated
    #: from, of this bucket's own last (closing) hourly candle.
    last_hour_index: int


def _aggregate_completed_buckets(candles: list[Candle], hours_per_bucket: int, bucket_start_fn) -> list[_CompletedBucket]:
    """Aggregate `candles` (assumed already strictly 1h-spaced and
    ordered - never re-validated here, see module docstring) into
    COMPLETED higher-timeframe buckets on a fixed, deterministic UTC grid.

    A bucket is included ONLY if it has EXACTLY `hours_per_bucket`
    constituent candles, the first one starts exactly on the bucket's own
    grid boundary, and every consecutive pair within it is spaced by
    precisely one hour - fail closed on anything else (a real gap, a
    misaligned or short run at either end of `candles`) by simply
    excluding that stretch, never fabricating a partial bucket.
    """
    buckets: list[_CompletedBucket] = []
    n = len(candles)
    i = 0
    while i < n:
        start = bucket_start_fn(candles[i].open_time_ms)
        j = i
        while j < n and bucket_start_fn(candles[j].open_time_ms) == start:
            j += 1
        group = candles[i:j]
        is_complete = (
            len(group) == hours_per_bucket
            and group[0].open_time_ms == start
            and all(group[k + 1].open_time_ms - group[k].open_time_ms == _HOUR_MS for k in range(len(group) - 1))
        )
        if is_complete:
            aggregated = Candle(
                symbol=group[0].symbol,
                interval=f"{hours_per_bucket}h_aggregated",
                open_time_ms=group[0].open_time_ms,
                close_time_ms=group[-1].close_time_ms,
                open=group[0].open,
                high=max(c.high for c in group),
                low=min(c.low for c in group),
                close=group[-1].close,
                volume=sum((c.volume for c in group), Decimal(0)),
            )
            buckets.append(_CompletedBucket(aggregated, j - 1))
        i = j
    return buckets


def _weekly_filter_bullish(candles: list[Candle]) -> bool:
    """True only when the LAST completed weekly candle's close is above a
    rising `WEEKLY_EMA_PERIOD`-period weekly EMA - see module docstring
    point 1. False (never an exception) when there is not yet enough
    completed weekly history to evaluate this at all - `generate_signal`'s
    own `min_required_candles` is sized so this should not normally occur,
    but a caller-supplied shorter series must still fail closed to
    "prohibited", never silently pass."""
    weekly_buckets = _aggregate_completed_buckets(candles, _WEEK_HOURS, _weekly_bucket_start_ms)
    min_weekly = WEEKLY_EMA_PERIOD + WEEKLY_EMA_SLOPE_LOOKBACK_CANDLES
    if len(weekly_buckets) < min_weekly:
        return False
    closes = [float(b.candle.close) for b in weekly_buckets]
    ema_values = ema(closes, WEEKLY_EMA_PERIOD)
    last_idx = len(weekly_buckets) - 1
    ema_curr = ema_values[last_idx]
    ema_prior = ema_values[last_idx - WEEKLY_EMA_SLOPE_LOOKBACK_CANDLES]
    return closes[last_idx] > ema_curr > ema_prior


@dataclass(frozen=True, slots=True)
class _SetupEvent:
    #: 1h index (into the `candles` a given `_scan_setup_events` call was
    #: given) of the 4h setup's own confirming (last) hourly candle.
    setup_hour_index: int
    breakout_level: float
    window_start_index: int
    window_end_index: int
    #: The FIRST 1h index within the window satisfying the raw 1h
    #: confirmation condition, causally knowable as of `len(candles) - 1`
    #: - None if none has been observed (yet, or ever, once the window has
    #: fully elapsed).
    confirmation_index: int | None


def _scan_setup_events(candles: list[Candle]) -> list[_SetupEvent]:
    """Find every 4h setup (module docstring point 2) confirmed so far,
    each with its own armed window and (if any, and if causally knowable
    yet) first confirming 1h candle (module docstring point 3). Pure
    function of `candles` alone - recomputed in full on every call, never
    persisting anything that could affect a returned `Signal`.
    """
    four_h_buckets = _aggregate_completed_buckets(candles, _FOUR_H_HOURS, _four_h_bucket_start_ms)
    min_four_h_idx = max(
        FOUR_H_CHANNEL_PERIOD, FOUR_H_ATR_PERIOD - 1, FOUR_H_EMA_REGIME_PERIOD + FOUR_H_EMA_SLOPE_LOOKBACK_CANDLES - 1
    )
    if len(four_h_buckets) <= min_four_h_idx:
        return []

    four_h_candles = [b.candle for b in four_h_buckets]
    closes = [float(c.close) for c in four_h_candles]
    highs = [float(c.high) for c in four_h_candles]
    ema200 = ema(closes, FOUR_H_EMA_REGIME_PERIOD)
    atr14 = atr(four_h_candles, FOUR_H_ATR_PERIOD)
    # rolling_max is an INCLUSIVE window ending at i; shifting back by one
    # index gives exactly "the channel_period candles STRICTLY BEFORE i",
    # identical to VolatilityNormalizedBreakoutStrategy's own convention.
    donchian_high_inclusive = rolling_max(highs, FOUR_H_CHANNEL_PERIOD)

    last_visible_1h_idx = len(candles) - 1
    events: list[_SetupEvent] = []
    for i in range(min_four_h_idx, len(four_h_buckets)):
        donchian_high_i = donchian_high_inclusive[i - 1]
        if math.isnan(donchian_high_i):  # not enough preceding history yet
            continue
        close_i = closes[i]
        atr_i = atr14[i]
        breakout_distance = close_i - donchian_high_i
        confirmed_breakout = breakout_distance > 0 and (atr_i <= 0 or breakout_distance >= FOUR_H_BREAKOUT_ATR_MULTIPLE * atr_i)
        if not confirmed_breakout:
            continue
        ema_curr = ema200[i]
        ema_prior = ema200[i - FOUR_H_EMA_SLOPE_LOOKBACK_CANDLES]
        if not (close_i > ema_curr > ema_prior):
            continue

        setup_hour_index = four_h_buckets[i].last_hour_index
        window_start_index = setup_hour_index + 1
        window_end_index = setup_hour_index + CONFIRMATION_WINDOW_1H_CANDLES
        confirmation_index = None
        for h in range(window_start_index, min(window_end_index, last_visible_1h_idx) + 1):
            hour_candle = candles[h]
            if float(hour_candle.close) > donchian_high_i and hour_candle.close > hour_candle.open:
                confirmation_index = h
                break
        events.append(_SetupEvent(setup_hour_index, donchian_high_i, window_start_index, window_end_index, confirmation_index))
    return events


def _weekly_floor_hours() -> int:
    return (WEEKLY_EMA_PERIOD + WEEKLY_EMA_SLOPE_LOOKBACK_CANDLES) * _WEEK_HOURS + _WEEKLY_ALIGNMENT_MARGIN_HOURS


def _four_h_floor_hours() -> int:
    min_four_h_candles = max(
        FOUR_H_CHANNEL_PERIOD + 1, FOUR_H_ATR_PERIOD, FOUR_H_EMA_REGIME_PERIOD + FOUR_H_EMA_SLOPE_LOOKBACK_CANDLES
    )
    return min_four_h_candles * _FOUR_H_HOURS + _FOUR_H_ALIGNMENT_MARGIN_HOURS


class MultiTimeframeBreakoutStrategy:
    def __init__(self) -> None:
        self.min_required_candles = max(_weekly_floor_hours(), _four_h_floor_hours()) + CONFIRMATION_WINDOW_1H_CANDLES
        #: Read-only telemetry only - see module docstring. Never consulted
        #: by generate_signal's own decision.
        self.weekly_filter_rejections = 0
        self.four_h_setups_detected = 0
        self.setups_armed = 0
        self.setups_expired = 0
        self.one_h_confirmations = 0
        self.entries = 0

    def generate_signal(self, candles: list[Candle], current_position: PositionSide) -> Signal:
        if len(candles) < self.min_required_candles:
            raise InsufficientDataError(
                f"need at least {self.min_required_candles} completed 1h candles, got {len(candles)}"
            )

        current_idx = len(candles) - 1
        last_candle = candles[-1]
        inputs: dict = {
            "symbol": last_candle.symbol,
            "close": float(last_candle.close),
            "current_position": current_position.value,
        }

        if not _weekly_filter_bullish(candles):
            self.weekly_filter_rejections += 1
            return Signal(SignalType.HOLD, "HOLD_WEEKLY_FILTER_BLOCKED", last_candle.close_time_ms, inputs)

        events = _scan_setup_events(candles)
        for event in events:
            if event.setup_hour_index == current_idx:
                self.four_h_setups_detected += 1
                self.setups_armed += 1
            if event.confirmation_index == current_idx:
                self.one_h_confirmations += 1
            if event.window_end_index == current_idx and event.confirmation_index is None:
                self.setups_expired += 1

        if current_position == PositionSide.LONG:
            return Signal(SignalType.HOLD, "HOLD_ALREADY_LONG", last_candle.close_time_ms, inputs)

        active_events = [e for e in events if e.window_start_index <= current_idx <= e.window_end_index]
        if not active_events:
            return Signal(SignalType.HOLD, "HOLD_NO_ARMED_SETUP", last_candle.close_time_ms, inputs)

        governing = max(active_events, key=lambda e: e.setup_hour_index)
        inputs["governing_setup_hour_index"] = governing.setup_hour_index
        inputs["governing_breakout_level"] = governing.breakout_level

        if governing.confirmation_index is None:
            return Signal(SignalType.HOLD, "HOLD_AWAITING_1H_CONFIRMATION", last_candle.close_time_ms, inputs)
        if governing.confirmation_index < current_idx:
            return Signal(SignalType.HOLD, "HOLD_SETUP_ALREADY_CONSUMED", last_candle.close_time_ms, inputs)

        self.entries += 1
        return Signal(SignalType.BUY, "MULTITIMEFRAME_1H_CONFIRMED_ENTRY", last_candle.close_time_ms, inputs)
