"""Proofs for `data/gap_recovery.py`: 1-minute-kline gap forensics and
OPTIONAL, explicitly-applied recovery of confirmed BTCUSDT 1h gaps.

Every fixture here is synthetic - no real Binance host is ever contacted,
no real database is touched, and no candidate evaluation of any kind is
ever run (nothing here imports `backtest/engine.py`, `RiskEngine`,
`BacktestBroker`, or calls `generate_signal`/`run_segment`).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from trading_agent.data.gap_detection import GapRecord, partition_into_segments
from trading_agent.data.gap_recovery import (
    RecoveredCandleProvenance,
    RecoveryOutcome,
    _is_continuous_60_minutes,
    apply_gap_recovery,
    count_round3_complete_blocks,
    run_gap_forensics,
)
from trading_agent.data.models import Candle, interval_to_ms
from trading_agent.data.storage import CandleStore
from trading_agent.research.cutoff import RESEARCH_CUTOFF_MS

SYMBOL = "BTCUSDT"
STEP_1H = interval_to_ms("1h")
STEP_1M = interval_to_ms("1m")
START = 1_700_000_000_000  # arbitrary, fixed ms - not grid-aligned, and doesn't need to be for this module


def _1h(open_time_ms: int, close: str = "100") -> Candle:
    return Candle(
        symbol=SYMBOL, interval="1h", open_time_ms=open_time_ms, close_time_ms=open_time_ms + STEP_1H - 1,
        open=Decimal(close), high=Decimal(close), low=Decimal(close), close=Decimal(close), volume=Decimal(1),
    )


def _1m(open_time_ms: int, o: str, h: str, low: str, c: str, v: str) -> Candle:
    return Candle(
        symbol=SYMBOL, interval="1m", open_time_ms=open_time_ms, close_time_ms=open_time_ms + STEP_1M - 1,
        open=Decimal(o), high=Decimal(h), low=Decimal(low), close=Decimal(c), volume=Decimal(v),
    )


def _60_minutes(hour_start: int, base_close: float = 100.0) -> list[Candle]:
    """60 perfectly continuous, hour-aligned 1-minute candles with
    distinguishable OHLCV so aggregation-math tests can verify exact
    open/high/low/close/volume selection, not just "some value"."""
    candles = []
    for i in range(60):
        close = base_close + i
        candles.append(_1m(hour_start + i * STEP_1M, o=str(close - 0.5), h=str(close + 0.5), low=str(close - 1), c=str(close), v="2"))
    return candles


def _gap(expected_open: int, previous_open: int, next_open: int, missing: int) -> GapRecord:
    return GapRecord(
        expected_open_time_ms=expected_open, previous_open_time_ms=previous_open,
        next_open_time_ms=next_open, missing_intervals=missing,
    )


class _FakeClient:
    """A test double standing in for `BinancePublicMarketDataClient` -
    never makes a real HTTP request. `minute_rows` maps open_time_ms ->
    Candle for the 1m interval; `native_1h_rows` does the same for a
    (normally absent) native 1h cross-check."""

    def __init__(self, minute_rows: dict[int, Candle] | None = None, native_1h_rows: dict[int, Candle] | None = None):
        self.minute_rows = minute_rows or {}
        self.native_1h_rows = native_1h_rows or {}
        self.requested_intervals: list[str] = []

    def get_klines(self, symbol, interval, start_time_ms=None, end_time_ms=None, limit=1000):
        self.requested_intervals.append(interval)
        source = self.minute_rows if interval == "1m" else self.native_1h_rows if interval == "1h" else {}
        lo = start_time_ms if start_time_ms is not None else min(source, default=0)
        hi = end_time_ms if end_time_ms is not None else max(source, default=0)
        return [c for t, c in sorted(source.items()) if lo <= t <= hi][:limit]


# --- 1 & 2 & 3. Query 1m klines for the exact missing interval;
# reconstruct only when all 60 exist continuously. ---


def test_all_60_minutes_present_and_continuous_is_fully_recoverable():
    hour = START
    client = _FakeClient(minute_rows={c.open_time_ms: c for c in _60_minutes(hour)})
    gap = _gap(hour, hour - STEP_1H, hour + STEP_1H, 1)
    report = run_gap_forensics(client, SYMBOL, "1h", [], [gap], max_retries=1, sleep_fn=lambda s: None, now_ms=999)
    outcome = report.gap_results[0].missing_hours[0]
    assert outcome.outcome is RecoveryOutcome.FULLY_RECOVERABLE
    assert outcome.found_1m_candle_count == 60
    assert report.fully_recoverable_hours == 1
    assert report.partially_recoverable_hours == 0
    assert report.genuine_no_data_hours == 0
    assert report.unresolved_hours == 0


def test_zero_minutes_present_is_genuine_no_data():
    hour = START
    client = _FakeClient(minute_rows={})
    gap = _gap(hour, hour - STEP_1H, hour + STEP_1H, 1)
    report = run_gap_forensics(client, SYMBOL, "1h", [], [gap], max_retries=1, sleep_fn=lambda s: None)
    outcome = report.gap_results[0].missing_hours[0]
    assert outcome.outcome is RecoveryOutcome.GENUINE_NO_DATA
    assert outcome.found_1m_candle_count == 0
    assert outcome.reconstructed_candle is None
    assert report.genuine_no_data_hours == 1


@pytest.mark.parametrize("present_count", [1, 30, 59])
def test_partial_minutes_present_is_partially_recoverable_never_reconstructed(present_count):
    hour = START
    all_minutes = _60_minutes(hour)
    client = _FakeClient(minute_rows={c.open_time_ms: c for c in all_minutes[:present_count]})
    gap = _gap(hour, hour - STEP_1H, hour + STEP_1H, 1)
    report = run_gap_forensics(client, SYMBOL, "1h", [], [gap], max_retries=1, sleep_fn=lambda s: None)
    outcome = report.gap_results[0].missing_hours[0]
    assert outcome.outcome is RecoveryOutcome.PARTIALLY_RECOVERABLE
    assert outcome.found_1m_candle_count == present_count
    assert outcome.reconstructed_candle is None
    assert report.partially_recoverable_hours == 1


def test_is_continuous_60_minutes_rejects_a_shifted_first_candle():
    # Direct unit test of the validation helper itself: within the actual
    # fetch pipeline, a request bounded to exactly one hour of 1-minute
    # grid slots can never legitimately RETURN 60 real candles that are
    # also misaligned (there are only 60 valid grid slots in that exact
    # window) - this proves the helper's own defensive first-timestamp
    # check independently of whether that state is reachable end-to-end.
    hour = START
    shifted = _60_minutes(hour + STEP_1M)  # starts one minute late
    assert _is_continuous_60_minutes(shifted, hour) is False


def test_is_continuous_60_minutes_rejects_an_internal_discontinuity():
    hour = START
    minutes = _60_minutes(hour)[:59] + [_60_minutes(hour + STEP_1H)[0]]  # last one is from the next hour
    assert _is_continuous_60_minutes(minutes, hour) is False


def test_is_continuous_60_minutes_accepts_the_canonical_case():
    hour = START
    assert _is_continuous_60_minutes(_60_minutes(hour), hour) is True


def test_is_continuous_60_minutes_rejects_wrong_count():
    hour = START
    assert _is_continuous_60_minutes(_60_minutes(hour)[:59], hour) is False


# --- 3. Aggregate exactly: open=first open, high=max high, low=min low,
# close=last close, volume=sum volume. ---


def test_aggregation_matches_binances_documented_1h_from_1m_convention():
    hour = START
    minutes = _60_minutes(hour, base_close=100.0)
    client = _FakeClient(minute_rows={c.open_time_ms: c for c in minutes})
    gap = _gap(hour, hour - STEP_1H, hour + STEP_1H, 1)
    report = run_gap_forensics(client, SYMBOL, "1h", [], [gap], max_retries=1, sleep_fn=lambda s: None)
    candle = report.gap_results[0].missing_hours[0].reconstructed_candle
    assert candle is not None
    assert candle.symbol == SYMBOL
    assert candle.interval == "1h"
    assert candle.open_time_ms == hour
    assert candle.close_time_ms == hour + STEP_1H - 1
    assert candle.open == minutes[0].open  # first 1m open
    assert candle.high == max(c.high for c in minutes)  # max 1m high
    assert candle.low == min(c.low for c in minutes)  # min 1m low
    assert candle.close == minutes[-1].close  # last 1m close
    assert candle.volume == sum((c.volume for c in minutes), Decimal(0))  # sum of 1m volumes


# --- 4. Compare reconstructed candles against Binance's normal 1h
# aggregation rules (a native-1h cross-check, when unexpectedly available). ---


def test_native_1h_unexpectedly_available_and_matching_is_still_fully_recoverable():
    hour = START
    minutes = _60_minutes(hour)
    aggregated = Candle(
        symbol=SYMBOL, interval="1h", open_time_ms=hour, close_time_ms=hour + STEP_1H - 1,
        open=minutes[0].open, high=max(c.high for c in minutes), low=min(c.low for c in minutes),
        close=minutes[-1].close, volume=sum((c.volume for c in minutes), Decimal(0)),
    )
    client = _FakeClient(
        minute_rows={c.open_time_ms: c for c in minutes},
        native_1h_rows={hour: aggregated},
    )
    gap = _gap(hour, hour - STEP_1H, hour + STEP_1H, 1)
    report = run_gap_forensics(client, SYMBOL, "1h", [], [gap], max_retries=1, sleep_fn=lambda s: None)
    outcome = report.gap_results[0].missing_hours[0]
    assert outcome.outcome is RecoveryOutcome.FULLY_RECOVERABLE
    assert outcome.provenance is not None
    assert outcome.provenance.validation_result == "VALID_MATCHES_NATIVE_1H"


def test_native_1h_unexpectedly_available_and_mismatching_is_unresolved_never_stored():
    hour = START
    minutes = _60_minutes(hour)
    wrong_native = _1h(hour, close="99999")  # deliberately does not match the 1m aggregate
    client = _FakeClient(
        minute_rows={c.open_time_ms: c for c in minutes},
        native_1h_rows={hour: wrong_native},
    )
    gap = _gap(hour, hour - STEP_1H, hour + STEP_1H, 1)
    report = run_gap_forensics(client, SYMBOL, "1h", [], [gap], max_retries=1, sleep_fn=lambda s: None)
    outcome = report.gap_results[0].missing_hours[0]
    assert outcome.outcome is RecoveryOutcome.UNRESOLVED
    assert outcome.reconstructed_candle is None


def test_native_1h_absent_as_expected_for_a_genuine_confirmed_gap():
    hour = START
    minutes = _60_minutes(hour)
    client = _FakeClient(minute_rows={c.open_time_ms: c for c in minutes})  # no native_1h_rows at all
    gap = _gap(hour, hour - STEP_1H, hour + STEP_1H, 1)
    report = run_gap_forensics(client, SYMBOL, "1h", [], [gap], max_retries=1, sleep_fn=lambda s: None)
    outcome = report.gap_results[0].missing_hours[0]
    assert outcome.outcome is RecoveryOutcome.FULLY_RECOVERABLE
    assert outcome.provenance is not None
    assert outcome.provenance.validation_result == "VALID_NATIVE_1H_UNAVAILABLE_AS_EXPECTED_FOR_A_CONFIRMED_GAP"


# --- 5. Never fabricate zero-volume candles or interpolate prices. ---


def test_a_reconstructed_candle_only_ever_contains_real_fetched_1m_values():
    # There is no code path that invents a value: assert the reconstructed
    # candle's every field traces back to an actual fetched 1m candle's
    # own field (already proven by the aggregation test above) - this
    # test additionally proves NOTHING is ever reconstructed when fewer
    # than 60 real candles exist, i.e. no zero-volume/interpolated
    # placeholder is ever substituted for missing real data.
    hour = START
    client = _FakeClient(minute_rows={})  # nothing real available
    gap = _gap(hour, hour - STEP_1H, hour + STEP_1H, 1)
    report = run_gap_forensics(client, SYMBOL, "1h", [], [gap], max_retries=1, sleep_fn=lambda s: None)
    outcome = report.gap_results[0].missing_hours[0]
    assert outcome.reconstructed_candle is None
    assert outcome.provenance is None


# --- 6. If all 60 minutes are unavailable, preserve the 1h gap unchanged. ---


def test_unrecoverable_hour_leaves_the_original_gap_untouched_by_forensics_alone():
    hour = START
    client = _FakeClient(minute_rows={})
    original_gap = _gap(hour, hour - STEP_1H, hour + STEP_1H, 1)
    report = run_gap_forensics(client, SYMBOL, "1h", [], [original_gap], max_retries=1, sleep_fn=lambda s: None)
    # run_gap_forensics is read-only - the input GapRecord object itself,
    # and the report's own reference to it, are completely unchanged.
    assert report.gap_results[0].gap == original_gap


def test_applying_recovery_with_nothing_fully_recoverable_leaves_the_gap_in_the_store(tmp_path):
    hour = START
    client = _FakeClient(minute_rows={})
    gap = _gap(hour, hour - STEP_1H, hour + STEP_1H, 1)
    db_path = tmp_path / "candles.db"
    with CandleStore(db_path) as store:
        store.store_candles_and_gaps([], [gap], SYMBOL, "1h", detected_at_ms=1)
        report = run_gap_forensics(client, SYMBOL, "1h", [], [gap], max_retries=1, sleep_fn=lambda s: None)
        result = apply_gap_recovery(store, SYMBOL, "1h", report)
        assert result.stored_candle_count == 0
        assert store.get_gaps(SYMBOL, "1h") == [gap]  # completely untouched


# --- 7. Provenance: source, retrieval time, component count, first/last
# timestamp, validation result, deterministic content hash. ---


def test_provenance_fields_are_fully_populated_for_a_recovered_candle():
    hour = START
    minutes = _60_minutes(hour)
    client = _FakeClient(minute_rows={c.open_time_ms: c for c in minutes})
    gap = _gap(hour, hour - STEP_1H, hour + STEP_1H, 1)
    report = run_gap_forensics(client, SYMBOL, "1h", [], [gap], max_retries=1, sleep_fn=lambda s: None, now_ms=555)
    provenance = report.gap_results[0].missing_hours[0].provenance
    assert isinstance(provenance, RecoveredCandleProvenance)
    assert provenance.source == "binance_1m_klines_aggregation"
    assert provenance.retrieved_at_ms == 555
    assert provenance.component_count == 60
    assert provenance.first_component_open_time_ms == hour
    assert provenance.last_component_open_time_ms == hour + 59 * STEP_1M
    assert provenance.validation_result.startswith("VALID")
    assert len(provenance.content_hash) == 64  # sha256 hex digest


def test_content_hash_is_deterministic_for_identical_input():
    hour = START
    minutes = _60_minutes(hour)
    client1 = _FakeClient(minute_rows={c.open_time_ms: c for c in minutes})
    client2 = _FakeClient(minute_rows={c.open_time_ms: c for c in minutes})
    gap = _gap(hour, hour - STEP_1H, hour + STEP_1H, 1)
    report1 = run_gap_forensics(client1, SYMBOL, "1h", [], [gap], max_retries=1, sleep_fn=lambda s: None, now_ms=1)
    report2 = run_gap_forensics(client2, SYMBOL, "1h", [], [gap], max_retries=1, sleep_fn=lambda s: None, now_ms=999999)
    hash1 = report1.gap_results[0].missing_hours[0].provenance.content_hash
    hash2 = report2.gap_results[0].missing_hours[0].provenance.content_hash
    assert hash1 == hash2  # retrieval time differs, but the CANDLE's own content hash must not


def test_content_hash_differs_for_a_different_reconstructed_candle():
    hour = START
    minutes_a = _60_minutes(hour, base_close=100.0)
    minutes_b = _60_minutes(hour, base_close=200.0)
    gap = _gap(hour, hour - STEP_1H, hour + STEP_1H, 1)
    report_a = run_gap_forensics(
        _FakeClient(minute_rows={c.open_time_ms: c for c in minutes_a}), SYMBOL, "1h", [], [gap],
        max_retries=1, sleep_fn=lambda s: None,
    )
    report_b = run_gap_forensics(
        _FakeClient(minute_rows={c.open_time_ms: c for c in minutes_b}), SYMBOL, "1h", [], [gap],
        max_retries=1, sleep_fn=lambda s: None,
    )
    hash_a = report_a.gap_results[0].missing_hours[0].provenance.content_hash
    hash_b = report_b.gap_results[0].missing_hours[0].provenance.content_hash
    assert hash_a != hash_b


# --- 8 & 9. Store recovered candles only after explicit confirmation;
# atomic and idempotent storage. ---


def test_run_gap_forensics_never_writes_to_any_store(tmp_path):
    hour = START
    minutes = _60_minutes(hour)
    client = _FakeClient(minute_rows={c.open_time_ms: c for c in minutes})
    gap = _gap(hour, hour - STEP_1H, hour + STEP_1H, 1)
    db_path = tmp_path / "candles.db"
    with CandleStore(db_path) as store:
        store.store_candles_and_gaps([], [gap], SYMBOL, "1h", detected_at_ms=1)
        run_gap_forensics(client, SYMBOL, "1h", [], [gap], max_retries=1, sleep_fn=lambda s: None)
        # Read-only analysis alone must never have stored anything.
        assert store.get_candles(SYMBOL, "1h") == []
        assert store.get_gaps(SYMBOL, "1h") == [gap]


def test_apply_gap_recovery_stores_atomically_and_removes_the_resolved_gap(tmp_path):
    hour = START
    minutes = _60_minutes(hour)
    client = _FakeClient(minute_rows={c.open_time_ms: c for c in minutes})
    existing = [_1h(hour - STEP_1H), _1h(hour + STEP_1H)]
    gap = _gap(hour, hour - STEP_1H, hour + STEP_1H, 1)
    db_path = tmp_path / "candles.db"
    with CandleStore(db_path) as store:
        store.upsert_candles(existing)
        store.store_candles_and_gaps([], [gap], SYMBOL, "1h", detected_at_ms=1)

        report = run_gap_forensics(client, SYMBOL, "1h", existing, [gap], max_retries=1, sleep_fn=lambda s: None)
        result = apply_gap_recovery(store, SYMBOL, "1h", report, detected_at_ms=2)

        assert result.stored_candle_count == 1
        assert result.remaining_confirmed_gaps == []
        stored = store.get_candles(SYMBOL, "1h")
        assert [c.open_time_ms for c in stored] == [hour - STEP_1H, hour, hour + STEP_1H]
        assert store.get_gaps(SYMBOL, "1h") == []


def test_apply_gap_recovery_is_idempotent(tmp_path):
    hour = START
    minutes = _60_minutes(hour)
    client = _FakeClient(minute_rows={c.open_time_ms: c for c in minutes})
    existing = [_1h(hour - STEP_1H), _1h(hour + STEP_1H)]
    gap = _gap(hour, hour - STEP_1H, hour + STEP_1H, 1)
    db_path = tmp_path / "candles.db"
    with CandleStore(db_path) as store:
        store.upsert_candles(existing)
        store.store_candles_and_gaps([], [gap], SYMBOL, "1h", detected_at_ms=1)

        report1 = run_gap_forensics(client, SYMBOL, "1h", existing, [gap], max_retries=1, sleep_fn=lambda s: None)
        apply_gap_recovery(store, SYMBOL, "1h", report1, detected_at_ms=2)
        after_first = store.get_candles(SYMBOL, "1h")

        # Re-running the ENTIRE flow again (fresh forensics against the
        # now-recovered database, which reports zero remaining gaps) must
        # be a complete no-op.
        gaps_now = store.get_gaps(SYMBOL, "1h")
        assert gaps_now == []
        report2 = run_gap_forensics(client, SYMBOL, "1h", after_first, gaps_now, max_retries=1, sleep_fn=lambda s: None)
        assert report2.total_gaps == 0
        after_second = store.get_candles(SYMBOL, "1h")
        assert after_first == after_second


def test_apply_gap_recovery_is_atomic_partial_recovery_produces_a_narrower_gap(tmp_path):
    # A gap of 2 missing hours where only the FIRST is fully recoverable -
    # the stored result must be exactly one new candle plus a narrower,
    # single-hour gap for the still-missing second hour.
    hour1 = START
    hour2 = START + STEP_1H
    minutes_hour1 = _60_minutes(hour1)
    client = _FakeClient(minute_rows={c.open_time_ms: c for c in minutes_hour1})  # hour2 has NO 1m data at all
    existing = [_1h(hour1 - STEP_1H), _1h(hour2 + STEP_1H)]
    gap = _gap(hour1, hour1 - STEP_1H, hour2 + STEP_1H, 2)
    db_path = tmp_path / "candles.db"
    with CandleStore(db_path) as store:
        store.upsert_candles(existing)
        store.store_candles_and_gaps([], [gap], SYMBOL, "1h", detected_at_ms=1)

        report = run_gap_forensics(client, SYMBOL, "1h", existing, [gap], max_retries=1, sleep_fn=lambda s: None)
        result = apply_gap_recovery(store, SYMBOL, "1h", report, detected_at_ms=2)

        assert result.stored_candle_count == 1
        assert len(result.remaining_confirmed_gaps) == 1
        remaining = result.remaining_confirmed_gaps[0]
        assert remaining.expected_open_time_ms == hour2
        assert remaining.missing_intervals == 1
        stored = store.get_candles(SYMBOL, "1h")
        assert [c.open_time_ms for c in stored] == [hour1 - STEP_1H, hour1, hour2 + STEP_1H]


def test_apply_gap_recovery_rejects_a_report_built_for_a_different_symbol_or_interval(tmp_path):
    db_path = tmp_path / "candles.db"
    hour = START
    client = _FakeClient(minute_rows={c.open_time_ms: c for c in _60_minutes(hour)})
    gap = _gap(hour, hour - STEP_1H, hour + STEP_1H, 1)
    report = run_gap_forensics(client, SYMBOL, "1h", [], [gap], max_retries=1, sleep_fn=lambda s: None)
    with CandleStore(db_path) as store, pytest.raises(ValueError):
        apply_gap_recovery(store, "ETHUSDT", "1h", report)


# --- 10. Never fetch or store anything at or after RESEARCH_CUTOFF_MS. ---


def test_a_missing_hour_at_the_cutoff_is_never_fetched():
    client = _FakeClient(minute_rows={RESEARCH_CUTOFF_MS + i * STEP_1M: c for i, c in enumerate(_60_minutes(RESEARCH_CUTOFF_MS))})
    gap = _gap(RESEARCH_CUTOFF_MS, RESEARCH_CUTOFF_MS - STEP_1H, RESEARCH_CUTOFF_MS + STEP_1H, 1)
    report = run_gap_forensics(client, SYMBOL, "1h", [], [gap], max_retries=1, sleep_fn=lambda s: None)
    outcome = report.gap_results[0].missing_hours[0]
    assert outcome.outcome is RecoveryOutcome.UNRESOLVED
    assert "cutoff" in outcome.detail.lower()
    assert outcome.found_1m_candle_count == 0
    assert "1m" not in client.requested_intervals  # the exchange was never even contacted for this hour


def test_a_missing_hour_one_interval_before_the_cutoff_is_still_processed_normally():
    hour = RESEARCH_CUTOFF_MS - STEP_1H
    client = _FakeClient(minute_rows={c.open_time_ms: c for c in _60_minutes(hour)})
    gap = _gap(hour, hour - STEP_1H, hour + STEP_1H, 1)
    report = run_gap_forensics(client, SYMBOL, "1h", [], [gap], max_retries=1, sleep_fn=lambda s: None)
    outcome = report.gap_results[0].missing_hours[0]
    assert outcome.outcome is RecoveryOutcome.FULLY_RECOVERABLE


def test_existing_candles_at_or_after_cutoff_are_excluded_from_segment_and_block_computation():
    pre_cutoff_candle = _1h(RESEARCH_CUTOFF_MS - STEP_1H)
    post_cutoff_candle = _1h(RESEARCH_CUTOFF_MS)
    client = _FakeClient()
    report = run_gap_forensics(
        client, SYMBOL, "1h", [pre_cutoff_candle, post_cutoff_candle], [], max_retries=1, sleep_fn=lambda s: None
    )
    all_open_times = {
        seg_open for seg in report.resulting_segments_after_recovery for seg_open in (seg.start_time_ms, seg.end_time_ms)
    }
    assert RESEARCH_CUTOFF_MS not in all_open_times
    assert len(report.resulting_segments_after_recovery) == 1
    assert report.resulting_segments_after_recovery[0].candle_count == 1  # only the pre-cutoff candle


def test_apply_gap_recovery_never_stores_a_candle_at_or_after_cutoff(tmp_path):
    gap = _gap(RESEARCH_CUTOFF_MS, RESEARCH_CUTOFF_MS - STEP_1H, RESEARCH_CUTOFF_MS + STEP_1H, 1)
    client = _FakeClient(minute_rows={c.open_time_ms: c for c in _60_minutes(RESEARCH_CUTOFF_MS)})
    db_path = tmp_path / "candles.db"
    with CandleStore(db_path) as store:
        store.store_candles_and_gaps([], [gap], SYMBOL, "1h", detected_at_ms=1)
        report = run_gap_forensics(client, SYMBOL, "1h", [], [gap], max_retries=1, sleep_fn=lambda s: None)
        result = apply_gap_recovery(store, SYMBOL, "1h", report)
        assert result.stored_candle_count == 0
        assert store.get_candles(SYMBOL, "1h") == []


# --- Report: resulting gap-free segment lengths + Round-3 block count. ---


def test_resulting_segments_after_recovery_merges_around_a_fully_recovered_hour():
    hour = START
    existing = [_1h(hour - STEP_1H), _1h(hour + STEP_1H)]
    client = _FakeClient(minute_rows={c.open_time_ms: c for c in _60_minutes(hour)})
    gap = _gap(hour, hour - STEP_1H, hour + STEP_1H, 1)
    report = run_gap_forensics(client, SYMBOL, "1h", existing, [gap], max_retries=1, sleep_fn=lambda s: None)
    assert len(report.resulting_segments_after_recovery) == 1
    assert report.resulting_segments_after_recovery[0].candle_count == 3


def test_resulting_segments_stay_split_when_the_hour_is_not_recoverable():
    hour = START
    existing = [_1h(hour - STEP_1H), _1h(hour + STEP_1H)]
    client = _FakeClient(minute_rows={})  # unrecoverable
    gap = _gap(hour, hour - STEP_1H, hour + STEP_1H, 1)
    report = run_gap_forensics(client, SYMBOL, "1h", existing, [gap], max_retries=1, sleep_fn=lambda s: None)
    assert len(report.resulting_segments_after_recovery) == 2
    assert [seg.candle_count for seg in report.resulting_segments_after_recovery] == [1, 1]


def test_count_round3_complete_blocks_pure_arithmetic_never_runs_a_candidate():
    # A single gap-free segment spanning exactly 2 full 10-day "blocks"
    # (using a tiny block_duration_days so this test runs instantly) with
    # a warm-up requirement of 5 candles.
    step = STEP_1H
    n_candles = 24 * 20 + 5  # 20 days of hourly candles + 5 warm-up candles
    candles = [_1h(START + i * step) for i in range(n_candles)]
    blocks = count_round3_complete_blocks(candles, "1h", anchor_warm_up_candles_required=5, block_duration_days=10)
    assert blocks == 2


def test_count_round3_complete_blocks_is_zero_for_a_too_short_segment():
    candles = [_1h(START + i * STEP_1H) for i in range(3)]
    assert count_round3_complete_blocks(candles, "1h", anchor_warm_up_candles_required=10) == 0


def test_count_round3_complete_blocks_is_zero_for_empty_input():
    assert count_round3_complete_blocks([], "1h", anchor_warm_up_candles_required=10) == 0


def test_round3_block_count_field_is_populated_in_the_report():
    report = run_gap_forensics(_FakeClient(), SYMBOL, "1h", [], [], max_retries=1, sleep_fn=lambda s: None)
    assert report.round3_min_required_candles > 0
    assert report.round3_complete_blocks_after_recovery == 0  # no candles supplied at all
    assert report.round3_block_duration_days == 365


# --- Interval restriction (this module only supports 1h gaps). ---


def test_run_gap_forensics_rejects_a_non_1h_interval():
    with pytest.raises(ValueError):
        run_gap_forensics(_FakeClient(), SYMBOL, "4h", [], [], max_retries=1, sleep_fn=lambda s: None)


# --- Sanity: the aggregation helper's output is itself a valid,
# gap-detector-compatible candle (ties this module back to
# data/gap_detection.py, never a parallel/incompatible notion of "candle"). ---


def test_a_fully_recovered_candle_slots_cleanly_into_partition_into_segments():
    hour = START
    existing = [_1h(hour - STEP_1H), _1h(hour + STEP_1H)]
    client = _FakeClient(minute_rows={c.open_time_ms: c for c in _60_minutes(hour)})
    gap = _gap(hour, hour - STEP_1H, hour + STEP_1H, 1)
    report = run_gap_forensics(client, SYMBOL, "1h", existing, [gap], max_retries=1, sleep_fn=lambda s: None)
    recovered = report.gap_results[0].missing_hours[0].reconstructed_candle
    assert recovered is not None
    merged = sorted([*existing, recovered], key=lambda c: c.open_time_ms)
    segmentation = partition_into_segments(merged, "1h")
    assert len(segmentation.segments) == 1
    assert segmentation.gaps == []
