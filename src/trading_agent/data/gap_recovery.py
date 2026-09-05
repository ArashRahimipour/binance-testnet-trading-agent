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

OPERATIONAL HANG INCIDENT (post-incident correction - the exact code path
capable of waiting this long): a prior version fired ONE HTTP request per
MISSING HOUR (up to 128 for a real 28-gap dataset), each independently
retried up to 5 times with UNCAPPED exponential backoff
(2s,4s,8s,16s,32s = 62s of pure sleep per exhausted hour), and printed
NOTHING until `run_gap_forensics` returned - i.e. until literally every
hour of every gap had been fully processed. A single hour whose requests
kept failing/timing out could alone burn over a minute of pure `time.sleep`
(CPU 0%, process state "sleeping" - exactly what was observed); multiplied
across 128 such hours with zero interim output, the WORST-CASE cumulative
wait was multiple hours, indistinguishable from a true hang. This is now
fixed at every layer:
  - every HTTP request carries an explicit, bounded (connect, read)
    timeout (`_HTTP_TIMEOUT` - see `data/market_data_public.py`);
  - at most `DEFAULT_MAX_RETRIES` (3) attempts per request, with backoff
    CAPPED at `DEFAULT_MAX_BACKOFF_SECONDS` (never growing unbounded -
    see `_capped_backoff_seconds`);
  - each gap's entire missing minute-range is fetched in as few BATCHED,
    paginated requests as possible (up to 1000 candles/request - Binance's
    own per-request cap), never one request per hour or per minute (see
    `_fetch_1m_range`);
  - every gap carries its own bounded wall-clock deadline
    (`max_seconds_per_gap`, default `DEFAULT_MAX_SECONDS_PER_GAP`) -
    exceeding it truncates that gap's remaining unchecked hours to
    `UNRESOLVED` ("time budget exceeded") rather than continuing to wait;
  - `on_progress`, when given, is invoked immediately at every gap start,
    every fetch attempt (with attempt number and outcome), and every gap
    completion - `cli/main.py`'s callback both echoes AND flushes stdout
    immediately, so progress is visible in real time even when output is
    redirected to a file/pipe;
  - a `KeyboardInterrupt` (Ctrl+C) at any point is caught INSIDE
    `run_gap_forensics` itself, which returns a normal, `interrupted=True`
    `GapForensicReport` covering whatever gaps completed so far, rather
    than propagating a raw traceback;
  - an optional `GapAuditCheckpoint` persists every gap's completed result
    to disk immediately (atomic write) as it finishes, so re-running the
    audit after an interruption (or simply to resume later) never
    re-downloads a gap already fully audited;
  - `apply_gap_recovery` (and `research-gap-recover --confirm`) REFUSE to
    store anything at all (`IncompleteAuditError`) when handed an
    `interrupted` report - recovery remains impossible without a
    completed audit.

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
request for Binance's OWN native 1h kline at that same open time (skipped
if the gap's own time budget has already been exhausted - the 1m-based
reconstruction is still valid and used without it). Ordinarily this
returns nothing (that is exactly what "a confirmed gap" already means -
`data/historical_fetch.py::_attempt_narrow_recovery` already tried and
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
an already-completed (never `interrupted`) `GapForensicReport` - the CLI
(`research-gap-recover --confirm`) is the only caller, gated on an
explicit `--confirm` flag (see `cli/main.py`). Storage is atomic (one
transaction, via the existing `CandleStore.store_candles_and_gaps`) and
idempotent (re-running recovery against an already-recovered database
re-derives and re-asserts the exact same candles and gap manifest - see
`tests/unit/test_gap_recovery.py`).
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from pathlib import Path

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
_KLINE_PAGE_LIMIT = 1000  # Binance's own per-request cap on klines

#: Requirement: "Maximum 3 attempts per request with capped backoff."
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_SECONDS = 1.0
DEFAULT_MAX_BACKOFF_SECONDS = 5.0

#: Requirement: "Add a maximum bounded duration per gap and fail that gap
#: as unresolved rather than hanging." 28 gaps x 60s is a 28-minute
#: absolute worst case, vs. the previously unbounded multi-hour hang.
DEFAULT_MAX_SECONDS_PER_GAP = 60.0

#: Requirement: "Every HTTP request must have bounded connect/read
#: timeouts." Explicit (connect, read) tuple - see
#: `data/market_data_public.py::BinancePublicMarketDataClient`, which
#: accepts either a single float or this tuple form and forwards it
#: verbatim to every `requests` call it makes.
DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
DEFAULT_READ_TIMEOUT_SECONDS = 10.0

CHECKPOINT_FILENAME = "gap_audit_checkpoint.json"

ProgressCallback = Callable[[str], None]


class RecoveryOutcome(str, Enum):
    """Mutually exclusive classification of exactly ONE missing 1h hour -
    see this module's own docstring for the full definition of each."""

    FULLY_RECOVERABLE = "fully_recoverable"
    PARTIALLY_RECOVERABLE = "partially_recoverable"
    GENUINE_NO_DATA = "genuine_no_data_exchange_outage"
    UNRESOLVED = "unresolved"


class IncompleteAuditError(Exception):
    """Raised by `apply_gap_recovery` when handed an `interrupted`
    `GapForensicReport` - recovery is impossible without a completed
    audit, regardless of `--confirm`."""


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
    #: True if a KeyboardInterrupt (Ctrl+C) stopped this audit before every
    #: one of `total_gaps` confirmed gaps was processed - `gap_results`
    #: then covers only the first `gaps_processed` of them. Gates recovery
    #: (see `apply_gap_recovery`/`IncompleteAuditError`).
    interrupted: bool = False
    gaps_processed: int = 0


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


def _emit(on_progress: ProgressCallback | None, message: str) -> None:
    if on_progress is not None:
        on_progress(message)


def _capped_backoff_seconds(attempt: int) -> float:
    """Exponential, but CAPPED - never grows unbounded like the incident's
    2/4/8/16/32s ladder. `attempt` is 1-based (the attempt that just
    failed)."""
    return min(DEFAULT_MAX_BACKOFF_SECONDS, DEFAULT_BACKOFF_SECONDS * (2 ** (attempt - 1)))


def _fetch_klines_with_bounded_retries(
    client: BinancePublicMarketDataClient,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    limit: int,
    max_retries: int,
    sleep_fn: Callable[[float], None],
    on_progress: ProgressCallback | None,
    gap_index: int,
    total_gaps: int,
    label: str,
) -> tuple[list[Candle] | None, bool]:
    """At most `max_retries` attempts (default `DEFAULT_MAX_RETRIES`, 3),
    capped backoff between them, immediate progress on every attempt.
    Returns `(page, True)` on success (possibly an empty page - that is a
    valid, successful "nothing here" answer), or `(None, False)` once
    retries are exhausted.
    """
    for attempt in range(1, max_retries + 1):
        _emit(
            on_progress,
            f"gap {gap_index}/{total_gaps} attempt {attempt}/{max_retries}: requesting {interval} klines "
            f"[{start_ms}, {end_ms}) {label}",
        )
        try:
            page = client.get_klines(
                symbol=symbol,
                interval=interval,
                start_time_ms=start_ms,
                end_time_ms=exclusive_upper_bound_for_request(end_ms),
                limit=limit,
            )
        except requests.exceptions.RequestException as exc:
            will_retry = attempt < max_retries
            _emit(
                on_progress,
                f"gap {gap_index}/{total_gaps} attempt {attempt}/{max_retries}: FAILED "
                f"({exc.__class__.__name__}: {exc}){' - retrying' if will_retry else ' - giving up after max attempts'}",
            )
            if will_retry:
                sleep_fn(_capped_backoff_seconds(attempt))
            continue
        page = below_exclusive_end(page, end_ms)
        _emit(on_progress, f"gap {gap_index}/{total_gaps} attempt {attempt}/{max_retries}: ok ({len(page)} candle(s))")
        return page, True
    return None, False


def _fetch_1m_range(
    client: BinancePublicMarketDataClient,
    symbol: str,
    start_ms: int,
    end_ms: int,
    max_retries: int,
    sleep_fn: Callable[[float], None],
    deadline: float,
    now_fn: Callable[[], float],
    on_progress: ProgressCallback | None,
    gap_index: int,
    total_gaps: int,
) -> tuple[list[Candle], int, str | None]:
    """Paginated, BATCHED fetch of every 1-minute candle across
    `[start_ms, end_ms)` - up to `_KLINE_PAGE_LIMIT` (1000) candles per
    request, NEVER one request per hour or per minute (requirement: batch
    each contiguous missing range efficiently).

    Stops early, returning whatever was already collected, if the gap's
    own wall-clock `deadline` (checked via `now_fn`, before each new page)
    is reached, or if one page's bounded retries are exhausted.

    Returns `(candles, reached_up_to_ms, stopped_reason)`. `stopped_reason`
    is `None` when the full range was genuinely confirmed (either fully
    fetched, or the exchange returned a short/empty page proving nothing
    more exists in range) - in that case `reached_up_to_ms == end_ms`.
    Otherwise it names why fetching stopped short of `end_ms`
    (`"gap_time_budget_exceeded"` or `"max_retries_exhausted"`), and
    `reached_up_to_ms` is the cursor position actually reached - any hour
    at or after it was never even attempted.
    """
    all_candles: list[Candle] = []
    cursor = start_ms
    page_num = 0
    while cursor < end_ms:
        if now_fn() >= deadline:
            return all_candles, cursor, "gap_time_budget_exceeded"
        page_num += 1
        page, ok = _fetch_klines_with_bounded_retries(
            client, symbol, "1m", cursor, end_ms, _KLINE_PAGE_LIMIT, max_retries, sleep_fn,
            on_progress, gap_index, total_gaps, label=f"(batch page {page_num})",
        )
        if not ok:
            return all_candles, cursor, "max_retries_exhausted"
        assert page is not None
        if not page:
            return all_candles, end_ms, None  # confirmed: nothing more in range
        all_candles.extend(page)
        cursor = page[-1].open_time_ms + _ONE_MINUTE_MS
        if len(page) < _KLINE_PAGE_LIMIT:
            return all_candles, end_ms, None  # short page - exchange confirms nothing more
    return all_candles, end_ms, None


def _fetch_native_1h(
    client: BinancePublicMarketDataClient,
    symbol: str,
    hour_open_time_ms: int,
    max_retries: int,
    sleep_fn: Callable[[float], None],
    on_progress: ProgressCallback | None,
    gap_index: int,
    total_gaps: int,
) -> Candle | None:
    """One read-only cross-check request for Binance's OWN native 1h
    kline at this exact open time - see module docstring's "CROSS-CHECK"
    section. Returns None (never raises) if the exchange has nothing
    there, or if the bounded retries were exhausted - either is treated
    the same way (no cross-check available), never as a fatal error."""
    page, ok = _fetch_klines_with_bounded_retries(
        client, symbol, "1h", hour_open_time_ms, hour_open_time_ms + _ONE_HOUR_MS, 1, max_retries, sleep_fn,
        on_progress, gap_index, total_gaps, label="(native 1h cross-check)",
    )
    if not ok or not page:
        return None
    matching = [c for c in page if c.open_time_ms == hour_open_time_ms]
    return matching[0] if matching else None


def _classify_hour(
    client: BinancePublicMarketDataClient,
    symbol: str,
    hour_open_time_ms: int,
    minute_candles: list[Candle],
    max_retries: int,
    sleep_fn: Callable[[float], None],
    now_ms: int,
    now_fn: Callable[[], float],
    deadline: float,
    on_progress: ProgressCallback | None,
    gap_index: int,
    total_gaps: int,
) -> MissingHourOutcome:
    """Classify one hour from ALREADY-FETCHED `minute_candles` (sliced out
    of the gap's own batched range fetch - see `_fetch_1m_range`) - this
    function itself makes at most one further network call (the native 1h
    cross-check), never a per-hour minute-range fetch."""
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

    native: Candle | None = None
    native_skipped_for_time_budget = False
    if now_fn() < deadline:
        native = _fetch_native_1h(client, symbol, hour_open_time_ms, max_retries, sleep_fn, on_progress, gap_index, total_gaps)
    else:
        native_skipped_for_time_budget = True

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

    if native is not None:
        validation_result = "VALID_MATCHES_NATIVE_1H"
    elif native_skipped_for_time_budget:
        validation_result = "VALID_NATIVE_1H_CHECK_SKIPPED_TIME_BUDGET"
    else:
        validation_result = "VALID_NATIVE_1H_UNAVAILABLE_AS_EXPECTED_FOR_A_CONFIRMED_GAP"

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


# --- Checkpoint/resume persistence (JSON, gap-level granularity). ---


def _candle_to_json(c: Candle) -> dict:
    return {
        "symbol": c.symbol, "interval": c.interval, "open_time_ms": c.open_time_ms,
        "close_time_ms": c.close_time_ms, "open": str(c.open), "high": str(c.high),
        "low": str(c.low), "close": str(c.close), "volume": str(c.volume),
    }


def _candle_from_json(d: dict) -> Candle:
    return Candle(
        symbol=d["symbol"], interval=d["interval"], open_time_ms=d["open_time_ms"],
        close_time_ms=d["close_time_ms"], open=Decimal(d["open"]), high=Decimal(d["high"]),
        low=Decimal(d["low"]), close=Decimal(d["close"]), volume=Decimal(d["volume"]),
    )


def _provenance_to_json(p: RecoveredCandleProvenance) -> dict:
    return {
        "source": p.source, "retrieved_at_ms": p.retrieved_at_ms, "component_count": p.component_count,
        "first_component_open_time_ms": p.first_component_open_time_ms,
        "last_component_open_time_ms": p.last_component_open_time_ms,
        "validation_result": p.validation_result, "content_hash": p.content_hash,
    }


def _provenance_from_json(d: dict) -> RecoveredCandleProvenance:
    return RecoveredCandleProvenance(**d)


def _missing_hour_to_json(m: MissingHourOutcome) -> dict:
    return {
        "open_time_ms": m.open_time_ms,
        "outcome": m.outcome.value,
        "found_1m_candle_count": m.found_1m_candle_count,
        "detail": m.detail,
        "reconstructed_candle": _candle_to_json(m.reconstructed_candle) if m.reconstructed_candle is not None else None,
        "provenance": _provenance_to_json(m.provenance) if m.provenance is not None else None,
    }


def _missing_hour_from_json(d: dict) -> MissingHourOutcome:
    return MissingHourOutcome(
        open_time_ms=d["open_time_ms"],
        outcome=RecoveryOutcome(d["outcome"]),
        found_1m_candle_count=d["found_1m_candle_count"],
        detail=d["detail"],
        reconstructed_candle=_candle_from_json(d["reconstructed_candle"]) if d["reconstructed_candle"] is not None else None,
        provenance=_provenance_from_json(d["provenance"]) if d["provenance"] is not None else None,
    )


def _gap_result_to_json(gr: GapForensicResult) -> dict:
    gap = gr.gap
    return {
        "gap": {
            "expected_open_time_ms": gap.expected_open_time_ms,
            "previous_open_time_ms": gap.previous_open_time_ms,
            "next_open_time_ms": gap.next_open_time_ms,
            "missing_intervals": gap.missing_intervals,
        },
        "missing_hours": [_missing_hour_to_json(m) for m in gr.missing_hours],
    }


def _gap_result_from_json(d: dict) -> GapForensicResult:
    g = d["gap"]
    gap = GapRecord(
        expected_open_time_ms=g["expected_open_time_ms"],
        previous_open_time_ms=g["previous_open_time_ms"],
        next_open_time_ms=g["next_open_time_ms"],
        missing_intervals=g["missing_intervals"],
    )
    return GapForensicResult(gap=gap, missing_hours=[_missing_hour_from_json(m) for m in d["missing_hours"]])


def _gap_checkpoint_key(gap: GapRecord) -> str:
    """A checkpoint entry is only ever reused if the CURRENT confirmed gap
    matches every one of these fields exactly - a gap that changed since
    it was checkpointed (e.g. after an unrelated `fetch-data` run altered
    the surrounding candles) is treated as uncached and re-audited."""
    return f"{gap.expected_open_time_ms}:{gap.previous_open_time_ms}:{gap.next_open_time_ms}:{gap.missing_intervals}"


class GapAuditCheckpoint:
    """Persists completed `GapForensicResult`s to a JSON file, keyed by
    each gap's own identity (see `_gap_checkpoint_key`) - so a resumed
    audit never re-downloads a gap it (or an earlier, interrupted run)
    already fully audited.

    `record()` writes IMMEDIATELY, atomically (write to a temp file, then
    `Path.replace`), so a completed gap survives a crash or Ctrl+C right
    after it finishes. A missing, empty, or corrupted checkpoint file is
    tolerated silently (starts fresh) - this file is a resumability aid
    for this tool only, never candle or gap-manifest data itself, so
    losing it only costs re-work, never correctness or safety.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._entries: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            loaded = json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            self._entries = {}
            return
        self._entries = loaded if isinstance(loaded, dict) else {}

    def get(self, gap: GapRecord) -> GapForensicResult | None:
        raw = self._entries.get(_gap_checkpoint_key(gap))
        if raw is None:
            return None
        try:
            return _gap_result_from_json(raw)
        except (KeyError, ValueError, TypeError):
            return None  # a corrupted single entry just means: re-audit this one gap

    def record(self, result: GapForensicResult) -> None:
        self._entries[_gap_checkpoint_key(result.gap)] = _gap_result_to_json(result)
        self._flush()

    def _flush(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_name(self._path.name + ".tmp")
        tmp_path.write_text(json.dumps(self._entries, indent=2, sort_keys=True))
        tmp_path.replace(self._path)

    def processed_count(self) -> int:
        return len(self._entries)

    def clear(self) -> None:
        self._entries = {}
        if self._path.exists():
            self._path.unlink()


def _analyze_one_gap(
    client: BinancePublicMarketDataClient,
    symbol: str,
    gap: GapRecord,
    gap_index: int,
    total_gaps: int,
    max_retries: int,
    sleep_fn: Callable[[float], None],
    max_seconds_per_gap: float,
    now_fn: Callable[[], float],
    on_progress: ProgressCallback | None,
    now_ms: int,
) -> GapForensicResult:
    start_monotonic = now_fn()
    deadline = start_monotonic + max_seconds_per_gap
    _emit(
        on_progress,
        f"gap {gap_index}/{total_gaps}: expected_open_time_ms={gap.expected_open_time_ms} "
        f"previous_open_time_ms={gap.previous_open_time_ms} next_open_time_ms={gap.next_open_time_ms} "
        f"missing_intervals={gap.missing_intervals}",
    )

    all_missing_hours = _missing_hours_for_gap(gap)
    fetchable_hours = [h for h in all_missing_hours if h < RESEARCH_CUTOFF_MS]
    cutoff_hours = [h for h in all_missing_hours if h >= RESEARCH_CUTOFF_MS]

    minute_candles_by_open_time: dict[int, Candle] = {}
    reached_up_to_ms = fetchable_hours[0] if fetchable_hours else 0
    stopped_reason: str | None = None

    if fetchable_hours:
        range_start = fetchable_hours[0]
        range_end = fetchable_hours[-1] + _ONE_HOUR_MS
        fetched, reached_up_to_ms, stopped_reason = _fetch_1m_range(
            client, symbol, range_start, range_end, max_retries, sleep_fn, deadline, now_fn,
            on_progress, gap_index, total_gaps,
        )
        minute_candles_by_open_time = {c.open_time_ms: c for c in fetched}

    outcomes: list[MissingHourOutcome] = []
    for hour in fetchable_hours:
        if stopped_reason is not None and hour >= reached_up_to_ms:
            reason_text = (
                "the per-gap time budget was exceeded" if stopped_reason == "gap_time_budget_exceeded"
                else "max retries were exhausted fetching this range"
            )
            outcomes.append(
                MissingHourOutcome(
                    open_time_ms=hour,
                    outcome=RecoveryOutcome.UNRESOLVED,
                    found_1m_candle_count=0,
                    detail=(
                        f"not checked - {reason_text} before reaching this hour; re-run to resume (earlier "
                        "hours in this same gap remain valid and are not re-fetched)."
                    ),
                )
            )
            continue
        hour_minutes = sorted(
            (c for t, c in minute_candles_by_open_time.items() if hour <= t < hour + _ONE_HOUR_MS),
            key=lambda c: c.open_time_ms,
        )
        outcomes.append(
            _classify_hour(
                client, symbol, hour, hour_minutes, max_retries, sleep_fn, now_ms, now_fn, deadline,
                on_progress, gap_index, total_gaps,
            )
        )

    for hour in cutoff_hours:
        outcomes.append(
            MissingHourOutcome(
                open_time_ms=hour,
                outcome=RecoveryOutcome.UNRESOLVED,
                found_1m_candle_count=0,
                detail=(
                    f"excluded - at or after the immutable research cutoff ({RESEARCH_CUTOFF_MS}ms); "
                    "never fetched from the exchange."
                ),
            )
        )

    elapsed = now_fn() - start_monotonic
    fully = sum(1 for o in outcomes if o.outcome is RecoveryOutcome.FULLY_RECOVERABLE)
    partially = sum(1 for o in outcomes if o.outcome is RecoveryOutcome.PARTIALLY_RECOVERABLE)
    genuine = sum(1 for o in outcomes if o.outcome is RecoveryOutcome.GENUINE_NO_DATA)
    unresolved = sum(1 for o in outcomes if o.outcome is RecoveryOutcome.UNRESOLVED)
    _emit(
        on_progress,
        f"gap {gap_index}/{total_gaps}: done in {elapsed:.1f}s - fully_recoverable={fully} "
        f"partially_recoverable={partially} genuine_no_data={genuine} unresolved={unresolved}",
    )

    return GapForensicResult(gap=gap, missing_hours=outcomes)


def run_gap_forensics(
    client: BinancePublicMarketDataClient,
    symbol: str,
    interval: str,
    existing_candles: list[Candle],
    confirmed_gaps: list[GapRecord],
    max_retries: int = DEFAULT_MAX_RETRIES,
    sleep_fn: Callable[[float], None] = time.sleep,
    now_ms: int | None = None,
    max_seconds_per_gap: float = DEFAULT_MAX_SECONDS_PER_GAP,
    now_fn: Callable[[], float] = time.monotonic,
    on_progress: ProgressCallback | None = None,
    checkpoint: GapAuditCheckpoint | None = None,
) -> GapForensicReport:
    """Read-only forensic analysis of every missing hour in
    `confirmed_gaps` - NEVER writes to any store (see `apply_gap_recovery`
    for the only code path that does). `existing_candles` is restricted to
    strictly-pre-cutoff data internally (via `split_at_cutoff`) before
    computing resulting segments or Round-3 block counts - the consumed,
    already-observed period is never touched by this module.

    Bounded, resumable, and observable by design (see this module's
    "OPERATIONAL HANG INCIDENT" docstring section): each gap gets its own
    `max_seconds_per_gap` wall-clock budget, every network attempt is
    reported via `on_progress` as it happens, a `KeyboardInterrupt` is
    caught here and returns a normal `interrupted=True` report instead of
    propagating, and `checkpoint` (when given) skips any gap already
    fully audited in a prior run.

    Only `interval == "1h"` is supported - minute-level reconstruction is
    only meaningful for hourly gaps (see this module's docstring and the
    audit request this tool was built for).
    """
    if interval != "1h":
        raise ValueError(f"gap forensics/recovery only supports interval='1h', got {interval!r}")
    if now_ms is None:
        now_ms = int(time.time() * 1000)

    pre_cutoff_existing, _consumed = split_at_cutoff(existing_candles)

    total = len(confirmed_gaps)
    gap_results: list[GapForensicResult] = []
    interrupted = False
    try:
        for idx, gap in enumerate(confirmed_gaps, start=1):
            cached = checkpoint.get(gap) if checkpoint is not None else None
            if cached is not None:
                _emit(
                    on_progress,
                    f"gap {idx}/{total}: already audited (checkpoint) - reusing cached result, no network request made",
                )
                gap_results.append(cached)
                continue
            result = _analyze_one_gap(
                client, symbol, gap, idx, total, max_retries, sleep_fn, max_seconds_per_gap, now_fn, on_progress, now_ms,
            )
            gap_results.append(result)
            if checkpoint is not None:
                checkpoint.record(result)
    except KeyboardInterrupt:
        interrupted = True
        _emit(
            on_progress,
            f"INTERRUPTED after {len(gap_results)}/{total} gap(s) - partial summary follows; "
            "already-checkpointed gaps will be skipped on the next run.",
        )

    fully = partially = genuine = unresolved = 0
    recovered_candles: list[Candle] = []
    for gap_result in gap_results:
        for outcome in gap_result.missing_hours:
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

    merged = _dedup_sort_1m(pre_cutoff_existing + recovered_candles)
    strategy = MultiTimeframeBreakoutStrategy()  # static warm-up constant only - never generates a signal

    return GapForensicReport(
        symbol=symbol,
        interval=interval,
        generated_at_ms=now_ms,
        total_gaps=total,
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
        interrupted=interrupted,
        gaps_processed=len(gap_results),
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

    Raises `IncompleteAuditError` if `report.interrupted` - recovery is
    impossible without a completed audit, regardless of `--confirm`.

    Idempotent: re-running this against a database that already has the
    recovered candles stored re-derives and re-asserts the identical
    candles and gap manifest (upserts are no-ops the second time); atomic:
    delegates to `CandleStore.store_candles_and_gaps`'s single transaction.
    """
    if report.interrupted:
        raise IncompleteAuditError(
            "the audit backing this report was interrupted (Ctrl+C) before processing every confirmed gap "
            f"({report.gaps_processed}/{report.total_gaps} completed) - refusing to store anything. Re-run "
            "the audit (already-checkpointed gaps are skipped) until it completes, then retry recovery."
        )
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
