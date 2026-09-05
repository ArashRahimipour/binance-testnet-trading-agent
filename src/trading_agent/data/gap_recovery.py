"""Forensic analysis and OPTIONAL, explicitly-confirmed recovery of
CONFIRMED historical 1h candle gaps, by reconstructing a missing 1h candle
from Binance's own official 1-minute klines - and ONLY when every single
one of the 60 expected 1-minute candles for that hour is present,
continuous, and validated.

This module changes NOTHING about any strategy, candidate, parameter,
scorecard, risk rule, fee, slippage, sizing, or execution logic, and it
NEVER runs a candidate evaluation (no `Strategy.generate_signal`, no
`backtest/engine.py::run_segment` call anywhere below) - the ONLY
candidate-related fact this module ever reads is
`MultiTimeframeBreakoutStrategy().min_required_candles`, a fixed,
side-effect-free constant computed from declared warm-up parameters, used
purely to COUNT how many complete Round-3 fixed-duration blocks a
recovered candle series would offer (see `count_round3_complete_blocks`) -
never to score, rank, or alter any historical verdict.

RECOVERY RULE (never relaxed): a missing 1h candle is reconstructed ONLY
when all 60 expected 1-minute candles exist, are correctly spaced (exactly
60_000ms apart), start exactly on the hour, and contain no duplicate or
out-of-order timestamp. If even one of the 60 is missing, this module
NEVER interpolates, extrapolates, or fabricates a substitute value of any
kind - the original 1h gap is left completely unchanged. A successful
reconstruction aggregates EXACTLY:
    open   = first  1m candle's open
    high   = max of all 60 1m candles' highs
    low    = min of all 60 1m candles' lows
    close  = last   1m candle's close
    volume = sum of all 60 1m candles' volumes
- Binance's own documented 1h-from-1m aggregation convention, verified in
`tests/unit/test_gap_recovery.py` against hand-computed expected values.

CROSS-CHECK AGAINST BINANCE'S OWN 1H AGGREGATION: after building a
candidate reconstruction, this module makes one additional read-only
request for Binance's OWN native 1h kline at that same open time. Ordinarily
this returns nothing (that is exactly what "a confirmed gap" already means
- `data/historical_fetch.py::_attempt_narrow_recovery` already tried and
failed at this same interval during the original download). If it
unexpectedly DOES return a candle, the two are compared: an exact OHLCV
match is fine (reported in provenance) - a MISMATCH is a validation
failure serious enough that the candle is NOT recovered at all
(`RecoveryOutcome.UNRESOLVED`), never silently stored anyway.

IMMUTABLE RESEARCH CUTOFF: no missing hour at or after
`research/cutoff.py::RESEARCH_CUTOFF_MS` is ever fetched, reconstructed,
or stored by this module - each one is classified `UNRESOLVED` with an
explicit "excluded - at/after cutoff" detail and the exchange is never
even contacted for it. `run_gap_forensics` additionally restricts its own
`existing_candles` input to strictly-pre-cutoff data via `split_at_cutoff`
before ever computing resulting segments or Round-3 block counts.

TWO-COMMAND SEPARATION: `run_gap_forensics` is purely READ-ONLY - it never
writes to any `CandleStore`. Only `apply_gap_recovery` writes, and only
ever with candles this module itself classified `FULLY_RECOVERABLE` inside
an already-computed `GapForensicReport` - the CLI (`research-gap-recover
--confirm`) is the only caller, gated on an explicit `--confirm` flag (see
`cli/main.py`). Storage is atomic (one transaction, via the existing
`CandleStore.store_candles_and_gaps`) and idempotent (re-running recovery
against an already-recovered database re-derives and re-asserts the exact
same candles and gap manifest - see `tests/unit/test_gap_recovery.py`).
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

import requests

from trading_agent.data.boundary import below_exclusive_end, exclusive_upper_bound_for_request
from trading_agent.data.exceptions import EmptyDataError
from trading_agent.data.gap_detection import GapRecord, partition_into_segments
from trading_agent.data.market_data_public import BinancePublicMarketDataClient
from trading_agent.data.models import Candle, interval_to_ms
from trading_agent.data.storage import CandleStore
from trading_agent.research.candidates.multitimeframe_breakout import MultiTimeframeBreakoutStrategy
from trading_agent.research.cutoff import RESEARCH_CUTOFF_MS, split_at_cutoff
from trading_agent.research.fixed_duration_evaluation import DEFAULT_BLOCK_DURATION_DAYS

RECOVERY_SOURCE = "binance_1m_klines_aggregation"

_ONE_HOUR_MS = interval_to_ms("1h")
_ONE_MINUTE_MS = interval_to_ms("1m")
_MINUTES_PER_HOUR = 60
_MS_PER_DAY = 24 * 60 * 60 * 1000

DEFAULT_MAX_RETRIES = 5
DEFAULT_BACKOFF_SECONDS = 2.0


class RecoveryOutcome(str, Enum):
    """Mutually exclusive classification of exactly ONE missing 1h hour -
    see this module's own docstring for the full definition of each."""

    FULLY_RECOVERABLE = "fully_recoverable"
    PARTIALLY_RECOVERABLE = "partially_recoverable"
    GENUINE_NO_DATA = "genuine_no_data_exchange_outage"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True, slots=True)
class RecoveredCandleProvenance:
    """Full forensic provenance for one reconstructed candle - required by
    the audit so a recovered candle's origin is always independently
    checkable, never a black box."""

    source: str
    retrieved_at_ms: int
    component_count: int
    first_component_open_time_ms: int
    last_component_open_time_ms: int
    validation_result: str
    content_hash: str


@dataclass(frozen=True, slots=True)
class MissingHourOutcome:
    """The forensic result for exactly one missing 1h candle (one hour
    within one confirmed gap)."""

    open_time_ms: int
    outcome: RecoveryOutcome
    found_1m_candle_count: int
    detail: str
    reconstructed_candle: Candle | None = None
    provenance: RecoveredCandleProvenance | None = None


@dataclass(frozen=True, slots=True)
class GapForensicResult:
    gap: GapRecord
    missing_hours: list[MissingHourOutcome] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class SegmentLength:
    segment_index: int
    start_time_ms: int
    end_time_ms: int
    candle_count: int


@dataclass(frozen=True, slots=True)
class GapForensicReport:
    symbol: str
    interval: str
    generated_at_ms: int
    total_gaps: int
    total_missing_hours: int
    fully_recoverable_hours: int
    partially_recoverable_hours: int
    genuine_no_data_hours: int
    unresolved_hours: int
    gap_results: list[GapForensicResult]
    resulting_segments_after_recovery: list[SegmentLength]
    round3_min_required_candles: int
    round3_complete_blocks_after_recovery: int
    round3_block_duration_days: int = DEFAULT_BLOCK_DURATION_DAYS


@dataclass(frozen=True, slots=True)
class RecoveryApplyResult:
    """What `apply_gap_recovery` actually did, for the CLI to report."""

    stored_candle_count: int
    remaining_confirmed_gaps: list[GapRecord] = field(default_factory=list)


def _content_hash(candle: Candle) -> str:
    """Deterministic SHA-256 over exactly this candle's own fields - two
    reconstructions of the same hour from the same 1-minute data always
    hash identically; any difference in any field changes the hash."""
    payload = {
        "symbol": candle.symbol,
        "interval": candle.interval,
        "open_time_ms": candle.open_time_ms,
        "close_time_ms": candle.close_time_ms,
        "open": str(candle.open),
        "high": str(candle.high),
        "low": str(candle.low),
        "close": str(candle.close),
        "volume": str(candle.volume),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _dedup_sort_1m(candles: list[Candle]) -> list[Candle]:
    seen: dict[int, Candle] = {}
    for candle in candles:
        seen[candle.open_time_ms] = candle
    return sorted(seen.values(), key=lambda c: c.open_time_ms)


def _is_continuous_60_minutes(candles: list[Candle], hour_open_time_ms: int) -> bool:
    if len(candles) != _MINUTES_PER_HOUR:
        return False
    if candles[0].open_time_ms != hour_open_time_ms:
        return False
    return all(
        candles[i + 1].open_time_ms - candles[i].open_time_ms == _ONE_MINUTE_MS
        for i in range(_MINUTES_PER_HOUR - 1)
    )


def _aggregate_1m_to_1h(symbol: str, hour_open_time_ms: int, minute_candles: list[Candle]) -> Candle:
    """Binance's own documented 1h-from-1m aggregation convention - see
    this module's docstring. `minute_candles` must already be the exact,
    validated 60-candle set (see `_is_continuous_60_minutes`)."""
    return Candle(
        symbol=symbol,
        interval="1h",
        open_time_ms=hour_open_time_ms,
        close_time_ms=hour_open_time_ms + _ONE_HOUR_MS - 1,
        open=minute_candles[0].open,
        high=max(c.high for c in minute_candles),
        low=min(c.low for c in minute_candles),
        close=minute_candles[-1].close,
        volume=sum((c.volume for c in minute_candles), Decimal(0)),
    )


def _fetch_1m_candles_for_hour(
    client: BinancePublicMarketDataClient,
    symbol: str,
    hour_open_time_ms: int,
    max_retries: int,
    sleep_fn: Callable[[float], None],
) -> list[Candle]:
    """Fetch every 1-minute candle Binance has for exactly
    `[hour_open_time_ms, hour_open_time_ms + 1h)` - a genuine exclusive
    upper bound at both the request itself and the returned result,
    reusing the same two-layer defense `data/historical_fetch.py` uses
    (see `historical_fetch_boundary.py`)."""
    end_time_ms = hour_open_time_ms + _ONE_HOUR_MS
    last_result: list[Candle] = []
    for attempt in range(max_retries):
        try:
            page = client.get_klines(
                symbol=symbol,
                interval="1m",
                start_time_ms=hour_open_time_ms,
                end_time_ms=exclusive_upper_bound_for_request(end_time_ms),
                limit=_MINUTES_PER_HOUR + 1,
            )
        except requests.exceptions.RequestException:
            sleep_fn(DEFAULT_BACKOFF_SECONDS * (2**attempt))
            continue
        last_result = _dedup_sort_1m(below_exclusive_end(page, end_time_ms))
        last_result = [c for c in last_result if c.open_time_ms >= hour_open_time_ms]
        if len(last_result) >= _MINUTES_PER_HOUR:
            return last_result
        sleep_fn(DEFAULT_BACKOFF_SECONDS * (2**attempt))
    return last_result


def _fetch_native_1h_candle(
    client: BinancePublicMarketDataClient,
    symbol: str,
    hour_open_time_ms: int,
    max_retries: int,
    sleep_fn: Callable[[float], None],
) -> Candle | None:
    """One read-only cross-check request for Binance's OWN native 1h
    kline at this exact open time - see module docstring's "CROSS-CHECK"
    section. Returns None (never raises) if the exchange has nothing
    there, which is the expected outcome for a genuinely confirmed gap."""
    end_time_ms = hour_open_time_ms + _ONE_HOUR_MS
    for attempt in range(max_retries):
        try:
            page = client.get_klines(
                symbol=symbol,
                interval="1h",
                start_time_ms=hour_open_time_ms,
                end_time_ms=exclusive_upper_bound_for_request(end_time_ms),
                limit=1,
            )
        except requests.exceptions.RequestException:
            sleep_fn(DEFAULT_BACKOFF_SECONDS * (2**attempt))
            continue
        matching = [c for c in page if c.open_time_ms == hour_open_time_ms]
        return matching[0] if matching else None
    return None


def _classify_and_reconstruct_hour(
    client: BinancePublicMarketDataClient,
    symbol: str,
    hour_open_time_ms: int,
    max_retries: int,
    sleep_fn: Callable[[float], None],
    now_ms: int,
) -> MissingHourOutcome:
    if hour_open_time_ms >= RESEARCH_CUTOFF_MS:
        return MissingHourOutcome(
            open_time_ms=hour_open_time_ms,
            outcome=RecoveryOutcome.UNRESOLVED,
            found_1m_candle_count=0,
            detail=(
                f"excluded - at or after the immutable research cutoff ({RESEARCH_CUTOFF_MS}ms); "
                "never fetched from the exchange."
            ),
        )

    minute_candles = _fetch_1m_candles_for_hour(client, symbol, hour_open_time_ms, max_retries, sleep_fn)
    count = len(minute_candles)

    if count == 0:
        return MissingHourOutcome(
            open_time_ms=hour_open_time_ms,
            outcome=RecoveryOutcome.GENUINE_NO_DATA,
            found_1m_candle_count=0,
            detail="Binance has zero 1-minute candles for this hour - a genuine exchange-side data absence.",
        )

    if not _is_continuous_60_minutes(minute_candles, hour_open_time_ms):
        if count < _MINUTES_PER_HOUR:
            return MissingHourOutcome(
                open_time_ms=hour_open_time_ms,
                outcome=RecoveryOutcome.PARTIALLY_RECOVERABLE,
                found_1m_candle_count=count,
                detail=(
                    f"only {count}/{_MINUTES_PER_HOUR} 1-minute candles available - reconstruction requires "
                    "ALL 60; the 1h gap is preserved unchanged."
                ),
            )
        return MissingHourOutcome(
            open_time_ms=hour_open_time_ms,
            outcome=RecoveryOutcome.UNRESOLVED,
            found_1m_candle_count=count,
            detail=(
                f"{count} 1-minute candles found but not a clean, contiguous, hour-aligned 60-candle "
                "run (misalignment/gap among the minute candles themselves) - refusing to reconstruct."
            ),
        )

    reconstructed = _aggregate_1m_to_1h(symbol, hour_open_time_ms, minute_candles)

    native = _fetch_native_1h_candle(client, symbol, hour_open_time_ms, max_retries, sleep_fn)
    if native is not None and (
        native.open != reconstructed.open
        or native.high != reconstructed.high
        or native.low != reconstructed.low
        or native.close != reconstructed.close
        or native.volume != reconstructed.volume
    ):
        return MissingHourOutcome(
            open_time_ms=hour_open_time_ms,
            outcome=RecoveryOutcome.UNRESOLVED,
            found_1m_candle_count=count,
            detail=(
                "the 1m-aggregated reconstruction does NOT match Binance's own native 1h kline for this "
                "hour (unexpectedly available) - refusing to store a candle that fails this cross-check."
            ),
        )

    validation_result = (
        "VALID_MATCHES_NATIVE_1H" if native is not None else "VALID_NATIVE_1H_UNAVAILABLE_AS_EXPECTED_FOR_A_CONFIRMED_GAP"
    )
    provenance = RecoveredCandleProvenance(
        source=RECOVERY_SOURCE,
        retrieved_at_ms=now_ms,
        component_count=count,
        first_component_open_time_ms=minute_candles[0].open_time_ms,
        last_component_open_time_ms=minute_candles[-1].open_time_ms,
        validation_result=validation_result,
        content_hash=_content_hash(reconstructed),
    )
    return MissingHourOutcome(
        open_time_ms=hour_open_time_ms,
        outcome=RecoveryOutcome.FULLY_RECOVERABLE,
        found_1m_candle_count=count,
        detail="all 60 1-minute candles present, continuous, and validated - reconstructed.",
        reconstructed_candle=reconstructed,
        provenance=provenance,
    )


def _missing_hours_for_gap(gap: GapRecord) -> list[int]:
    return [gap.expected_open_time_ms + i * _ONE_HOUR_MS for i in range(gap.missing_intervals)]


def count_round3_complete_blocks(
    candles: list[Candle],
    interval: str,
    anchor_warm_up_candles_required: int,
    block_duration_days: int = DEFAULT_BLOCK_DURATION_DAYS,
) -> int:
    """Pure candle-counting arithmetic ONLY - never runs a candidate, a
    signal, or the backtest engine. Reuses the exact same
    `total_tradable_span_ms // block_duration_ms` per-segment formula
    `research/fixed_duration_evaluation.py::build_fixed_duration_schedule`
    uses to decide how many complete blocks a segment offers, so "how many
    Round-3 blocks would exist after recovery" is answered with the same
    arithmetic the real evaluation would use, without ever evaluating
    anything.
    """
    if not candles:
        return 0
    step_ms = interval_to_ms(interval)
    block_duration_ms = block_duration_days * _MS_PER_DAY
    segmentation = partition_into_segments(candles, interval)
    total_blocks = 0
    for segment in segmentation.segments:
        n = len(segment)
        if n < anchor_warm_up_candles_required:
            continue
        first_tradable_idx = anchor_warm_up_candles_required - 1
        open_times = [c.open_time_ms for c in segment]
        tradable_start_ms = open_times[first_tradable_idx]
        total_tradable_span_ms = (open_times[-1] - tradable_start_ms) + step_ms
        total_blocks += total_tradable_span_ms // block_duration_ms
    return int(total_blocks)


def _segment_lengths(candles: list[Candle], interval: str) -> list[SegmentLength]:
    if not candles:
        return []
    segmentation = partition_into_segments(candles, interval)
    return [
        SegmentLength(
            segment_index=idx,
            start_time_ms=segment[0].open_time_ms,
            end_time_ms=segment[-1].close_time_ms,
            candle_count=len(segment),
        )
        for idx, segment in enumerate(segmentation.segments)
    ]


def run_gap_forensics(
    client: BinancePublicMarketDataClient,
    symbol: str,
    interval: str,
    existing_candles: list[Candle],
    confirmed_gaps: list[GapRecord],
    max_retries: int = DEFAULT_MAX_RETRIES,
    sleep_fn: Callable[[float], None] = time.sleep,
    now_ms: int | None = None,
) -> GapForensicReport:
    """Read-only forensic analysis of every missing hour in
    `confirmed_gaps` - NEVER writes to any store (see `apply_gap_recovery`
    for the only code path that does). `existing_candles` is restricted to
    strictly-pre-cutoff data internally (via `split_at_cutoff`) before
    computing resulting segments or Round-3 block counts - the consumed,
    already-observed period is never touched by this module.

    Only `interval == "1h"` is supported - minute-level reconstruction is
    only meaningful for hourly gaps (see this module's docstring and the
    audit request this tool was built for).
    """
    if interval != "1h":
        raise ValueError(f"gap forensics/recovery only supports interval='1h', got {interval!r}")
    if now_ms is None:
        now_ms = int(time.time() * 1000)

    pre_cutoff_existing, _consumed = split_at_cutoff(existing_candles)

    gap_results: list[GapForensicResult] = []
    fully = partially = genuine = unresolved = 0
    recovered_candles: list[Candle] = []

    for gap in confirmed_gaps:
        outcomes: list[MissingHourOutcome] = []
        for hour_open_time_ms in _missing_hours_for_gap(gap):
            outcome = _classify_and_reconstruct_hour(client, symbol, hour_open_time_ms, max_retries, sleep_fn, now_ms)
            outcomes.append(outcome)
            if outcome.outcome is RecoveryOutcome.FULLY_RECOVERABLE:
                fully += 1
                assert outcome.reconstructed_candle is not None
                recovered_candles.append(outcome.reconstructed_candle)
            elif outcome.outcome is RecoveryOutcome.PARTIALLY_RECOVERABLE:
                partially += 1
            elif outcome.outcome is RecoveryOutcome.GENUINE_NO_DATA:
                genuine += 1
            else:
                unresolved += 1
        gap_results.append(GapForensicResult(gap=gap, missing_hours=outcomes))

    merged = _dedup_sort_1m(pre_cutoff_existing + recovered_candles)
    strategy = MultiTimeframeBreakoutStrategy()  # static warm-up constant only - never generates a signal

    return GapForensicReport(
        symbol=symbol,
        interval=interval,
        generated_at_ms=now_ms,
        total_gaps=len(confirmed_gaps),
        total_missing_hours=sum(gap.missing_intervals for gap in confirmed_gaps),
        fully_recoverable_hours=fully,
        partially_recoverable_hours=partially,
        genuine_no_data_hours=genuine,
        unresolved_hours=unresolved,
        gap_results=gap_results,
        resulting_segments_after_recovery=_segment_lengths(merged, interval),
        round3_min_required_candles=strategy.min_required_candles,
        round3_complete_blocks_after_recovery=count_round3_complete_blocks(
            merged, interval, strategy.min_required_candles
        ),
    )


def apply_gap_recovery(
    store: CandleStore,
    symbol: str,
    interval: str,
    report: GapForensicReport,
    detected_at_ms: int | None = None,
) -> RecoveryApplyResult:
    """Persist every `FULLY_RECOVERABLE` reconstructed candle from `report`
    into `store`, atomically alongside a freshly recomputed gap manifest -
    the ONLY function in this module that writes anything. Never called
    except by `research-gap-recover --confirm` (see `cli/main.py`).

    Idempotent: re-running this against a database that already has the
    recovered candles stored re-derives and re-asserts the identical
    candles and gap manifest (upserts are no-ops the second time); atomic:
    delegates to `CandleStore.store_candles_and_gaps`'s single transaction.
    """
    if report.interval != interval or report.symbol != symbol:
        raise ValueError(
            f"report was built for {report.symbol!r}/{report.interval!r}, not {symbol!r}/{interval!r}"
        )
    if detected_at_ms is None:
        detected_at_ms = int(time.time() * 1000)

    recovered_candles = [
        outcome.reconstructed_candle
        for gap_result in report.gap_results
        for outcome in gap_result.missing_hours
        if outcome.outcome is RecoveryOutcome.FULLY_RECOVERABLE and outcome.reconstructed_candle is not None
    ]
    old_gap_expected_open_times = [gap_result.gap.expected_open_time_ms for gap_result in report.gap_results]

    if not recovered_candles:
        return RecoveryApplyResult(
            stored_candle_count=0, remaining_confirmed_gaps=[gr.gap for gr in report.gap_results]
        )

    existing = store.get_candles(symbol, interval)
    if not existing and not recovered_candles:
        raise EmptyDataError("no candles to recover into")  # pragma: no cover - defensive, unreachable above
    merged = _dedup_sort_1m(existing + recovered_candles)
    new_segmentation = partition_into_segments(merged, interval)

    store.store_candles_and_gaps(
        recovered_candles,
        new_segmentation.gaps,
        symbol,
        interval,
        detected_at_ms=detected_at_ms,
        stale_gap_expected_open_times=old_gap_expected_open_times,
    )
    return RecoveryApplyResult(stored_candle_count=len(recovered_candles), remaining_confirmed_gaps=new_segmentation.gaps)
