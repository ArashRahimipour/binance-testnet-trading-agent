"""Proofs for the research-gap-audit operational-hang fix: bounded
connect/read timeouts, capped-retry backoff, per-gap batching (never one
request per hour/minute), immediate flushed progress, a bounded duration
per gap, clean Ctrl+C handling with a partial summary, and JSON
checkpoint/resume so an already-audited gap is never re-downloaded.

Every fixture is synthetic - no real Binance host is contacted and no
real database is touched anywhere in this file. `sleep_fn` and `now_fn`
are always injected fakes, so nothing here ever actually sleeps or takes
wall-clock time, however large the simulated durations are.
"""

from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
import requests

from trading_agent.data.gap_detection import GapRecord
from trading_agent.data.gap_recovery import (
    DEFAULT_MAX_BACKOFF_SECONDS,
    DEFAULT_MAX_RETRIES,
    GapAuditCheckpoint,
    IncompleteAuditError,
    RecoveryOutcome,
    _capped_backoff_seconds,
    apply_gap_recovery,
    run_gap_forensics,
)
from trading_agent.data.market_data_public import TESTNET_HOST, BinancePublicMarketDataClient
from trading_agent.data.models import Candle, interval_to_ms
from trading_agent.data.storage import CandleStore

SYMBOL = "BTCUSDT"
STEP_1H = interval_to_ms("1h")
STEP_1M = interval_to_ms("1m")
START = 1_700_000_000_000


def _1m(open_time_ms: int) -> Candle:
    return Candle(
        symbol=SYMBOL, interval="1m", open_time_ms=open_time_ms, close_time_ms=open_time_ms + STEP_1M - 1,
        open=Decimal(100), high=Decimal(101), low=Decimal(99), close=Decimal(100), volume=Decimal(1),
    )


def _60_minutes(hour_start: int) -> list[Candle]:
    return [_1m(hour_start + i * STEP_1M) for i in range(60)]


def _gap(expected_open: int, previous_open: int, next_open: int, missing: int) -> GapRecord:
    return GapRecord(
        expected_open_time_ms=expected_open, previous_open_time_ms=previous_open,
        next_open_time_ms=next_open, missing_intervals=missing,
    )


def _make_fake_clock(increment_per_call: float):
    """A monotonic-like clock, injected as `now_fn`, that advances by a
    fixed amount on every call - lets a test simulate any elapsed
    duration deterministically without any real waiting."""
    state = {"t": 0.0}

    def _now() -> float:
        state["t"] += increment_per_call
        return state["t"]

    return _now


class _CallLoggingClient:
    """Records every `get_klines` call's arguments - used to prove
    batching (few calls, not one per hour/minute) and to simulate
    always-failing or always-full-page responses."""

    def __init__(self, responder):
        self.calls: list[dict] = []
        self._responder = responder

    def get_klines(self, symbol, interval, start_time_ms=None, end_time_ms=None, limit=1000):
        call = {"symbol": symbol, "interval": interval, "start_time_ms": start_time_ms, "end_time_ms": end_time_ms, "limit": limit}
        self.calls.append(call)
        return self._responder(call)

    def calls_for(self, interval: str) -> list[dict]:
        return [c for c in self.calls if c["interval"] == interval]


# --- 2. Requirement: at most 3 attempts per request, capped backoff. ---


def test_default_max_retries_is_3():
    assert DEFAULT_MAX_RETRIES == 3


def test_capped_backoff_never_exceeds_the_cap():
    for attempt in range(1, 10):
        assert _capped_backoff_seconds(attempt) <= DEFAULT_MAX_BACKOFF_SECONDS


def test_capped_backoff_is_nondecreasing_until_the_cap():
    values = [_capped_backoff_seconds(a) for a in range(1, 6)]
    assert values == sorted(values)


def test_a_request_that_always_fails_is_retried_exactly_max_retries_times_then_marked_unresolved():
    call_count = {"n": 0}

    def always_fail(call):
        call_count["n"] += 1
        raise requests.exceptions.ConnectionError("simulated network failure")

    client = _CallLoggingClient(always_fail)
    sleeps: list[float] = []
    gap = _gap(START, START - STEP_1H, START + STEP_1H, 1)

    report = run_gap_forensics(
        client, SYMBOL, "1h", [], [gap], max_retries=3, sleep_fn=sleeps.append, now_fn=lambda: 0.0,
    )

    assert call_count["n"] == 3  # exactly max_retries attempts, never more
    assert len(sleeps) == 2  # sleeps between attempts 1->2 and 2->3, never after the last attempt
    assert all(s <= DEFAULT_MAX_BACKOFF_SECONDS for s in sleeps)
    outcome = report.gap_results[0].missing_hours[0]
    assert outcome.outcome is RecoveryOutcome.UNRESOLVED
    assert "max retries" in outcome.detail.lower()


def test_a_timeout_exception_is_treated_exactly_like_any_other_request_failure():
    def always_timeout(call):
        raise requests.exceptions.Timeout("simulated read timeout")

    client = _CallLoggingClient(always_timeout)
    gap = _gap(START, START - STEP_1H, START + STEP_1H, 1)
    report = run_gap_forensics(client, SYMBOL, "1h", [], [gap], max_retries=3, sleep_fn=lambda s: None, now_fn=lambda: 0.0)
    outcome = report.gap_results[0].missing_hours[0]
    assert outcome.outcome is RecoveryOutcome.UNRESOLVED


# --- 1. Requirement: every HTTP request has bounded connect/read timeouts. ---


def test_get_klines_forwards_an_explicit_connect_read_timeout_tuple():
    client = BinancePublicMarketDataClient(TESTNET_HOST, timeout_seconds=(3.0, 7.0))
    client._session = MagicMock()
    client._session.get.return_value.json.return_value = []
    client.get_klines(symbol=SYMBOL, interval="1m", start_time_ms=START, end_time_ms=START + STEP_1M)
    _, kwargs = client._session.get.call_args
    assert kwargs["timeout"] == (3.0, 7.0)


def test_get_server_time_ms_forwards_the_same_bounded_timeout():
    client = BinancePublicMarketDataClient(TESTNET_HOST, timeout_seconds=(3.0, 7.0))
    client._session = MagicMock()
    client._session.get.return_value.json.return_value = {"serverTime": 1}
    client.get_server_time_ms()
    _, kwargs = client._session.get.call_args
    assert kwargs["timeout"] == (3.0, 7.0)


def test_get_exchange_info_forwards_the_same_bounded_timeout():
    client = BinancePublicMarketDataClient(TESTNET_HOST, timeout_seconds=(3.0, 7.0))
    client._session = MagicMock()
    client._session.get.return_value.json.return_value = {}
    client.get_exchange_info(SYMBOL)
    _, kwargs = client._session.get.call_args
    assert kwargs["timeout"] == (3.0, 7.0)


# --- 4. Requirement: batch each contiguous missing range efficiently;
# never one request per minute (or per hour). ---


def test_a_multi_hour_gap_is_fetched_in_one_batched_request_not_one_per_hour():
    missing_hours = 5
    all_minutes: dict[int, Candle] = {}
    for h in range(missing_hours):
        for c in _60_minutes(START + h * STEP_1H):
            all_minutes[c.open_time_ms] = c

    def respond(call):
        if call["interval"] != "1m":
            return []  # native 1h cross-check - nothing there, as expected for a genuine gap
        lo, hi = call["start_time_ms"], call["end_time_ms"]
        return [c for t, c in sorted(all_minutes.items()) if lo <= t <= hi][: call["limit"]]

    client = _CallLoggingClient(respond)
    gap = _gap(START, START - STEP_1H, START + missing_hours * STEP_1H, missing_hours)
    report = run_gap_forensics(client, SYMBOL, "1h", [], [gap], max_retries=1, sleep_fn=lambda s: None, now_fn=lambda: 0.0)

    minute_calls = client.calls_for("1m")
    assert len(minute_calls) == 1  # ONE batched request for all 5 missing hours, not 5
    assert report.fully_recoverable_hours == missing_hours


def test_a_large_gap_needing_multiple_pages_still_uses_far_fewer_requests_than_one_per_hour():
    missing_hours = 20  # 1200 missing minutes - needs 2 pages of <=1000, never 20 requests
    all_minutes: dict[int, Candle] = {}
    for h in range(missing_hours):
        for c in _60_minutes(START + h * STEP_1H):
            all_minutes[c.open_time_ms] = c

    def respond(call):
        if call["interval"] != "1m":
            return []  # native 1h cross-check - nothing there, as expected for a genuine gap
        lo, hi = call["start_time_ms"], call["end_time_ms"]
        return [c for t, c in sorted(all_minutes.items()) if lo <= t <= hi][: call["limit"]]

    client = _CallLoggingClient(respond)
    gap = _gap(START, START - STEP_1H, START + missing_hours * STEP_1H, missing_hours)
    report = run_gap_forensics(client, SYMBOL, "1h", [], [gap], max_retries=1, sleep_fn=lambda s: None, now_fn=lambda: 0.0)

    minute_calls = client.calls_for("1m")
    assert len(minute_calls) <= 3  # a handful of pages, nowhere near 20 (one per hour) or 1200 (one per minute)
    assert report.fully_recoverable_hours == missing_hours


# --- 7. Requirement: a maximum bounded duration per gap - fail that gap's
# remaining hours as unresolved rather than hanging. "Slow"/"timeout"
# synthetic scenario: an exchange that ALWAYS has more data (an otherwise
# endless paginated response) is still bounded by the per-gap deadline. ---


def test_an_endlessly_paginating_response_is_still_bounded_by_the_per_gap_deadline():
    missing_hours = 50  # 3000 missing minutes - needs 3 full 1000-candle pages

    def always_full_page(call):
        if call["interval"] != "1m":
            return []  # native 1h cross-check - nothing there, as expected for a genuine gap
        # Simulates an exchange that always has another full page available
        # starting exactly where asked - if unbounded, pagination would
        # never terminate on its own.
        start = call["start_time_ms"]
        return [_1m(start + i * STEP_1M) for i in range(call["limit"])]

    client = _CallLoggingClient(always_full_page)
    gap = _gap(START, START - STEP_1H, START + missing_hours * STEP_1H, missing_hours)

    # Clock advances +2.0s per call; max_seconds_per_gap=5.0 means the
    # deadline trips after a couple of page fetches, not zero and not all.
    report = run_gap_forensics(
        client, SYMBOL, "1h", [], [gap], max_retries=1, sleep_fn=lambda s: None,
        now_fn=_make_fake_clock(2.0), max_seconds_per_gap=5.0,
    )

    outcomes = report.gap_results[0].missing_hours
    assert len(outcomes) == missing_hours
    assert any(o.outcome is RecoveryOutcome.FULLY_RECOVERABLE for o in outcomes)
    time_budget_outcomes = [o for o in outcomes if "time budget" in o.detail]
    assert time_budget_outcomes  # some hours were cut off by the deadline
    assert all(o.outcome is RecoveryOutcome.UNRESOLVED for o in time_budget_outcomes)
    # The cut-off hours are strictly the LATER ones - earlier hours already
    # fetched are still fully classified, never discarded.
    first_cutoff_index = next(i for i, o in enumerate(outcomes) if "time budget" in o.detail)
    # Everything actually reached before the cutoff is properly classified
    # (FULLY_RECOVERABLE, or PARTIALLY_RECOVERABLE for the one boundary
    # hour whose 60 minutes straddle exactly where fetching stopped) -
    # never silently discarded or mislabeled as a time-budget failure.
    assert all(
        o.outcome in (RecoveryOutcome.FULLY_RECOVERABLE, RecoveryOutcome.PARTIALLY_RECOVERABLE)
        for o in outcomes[:first_cutoff_index]
    )
    # Pagination genuinely stopped early - far fewer than the ~3+ pages
    # needed to cover all 3000 minutes if it had run to completion, and
    # nowhere near "one request per hour" (50) or "one per minute" (3000).
    assert len(client.calls_for("1m")) <= 4


# --- 5 & 6. Requirement: immediate, flushed progress output:
# "gap X/28", timestamp range, attempt number and outcome. ---


def test_progress_reports_gap_index_total_attempt_number_and_outcome():
    messages: list[str] = []
    gap1 = _gap(START, START - STEP_1H, START + STEP_1H, 1)
    gap2 = _gap(START + 10 * STEP_1H, START + 9 * STEP_1H, START + 11 * STEP_1H, 1)

    def respond(call):
        if call["interval"] != "1m":
            return []  # native 1h cross-check - nothing there, as expected for a genuine gap
        return _60_minutes(call["start_time_ms"])

    client = _CallLoggingClient(respond)
    run_gap_forensics(
        client, SYMBOL, "1h", [], [gap1, gap2], max_retries=3, sleep_fn=lambda s: None,
        now_fn=lambda: 0.0, on_progress=messages.append,
    )

    assert any("gap 1/2" in m for m in messages)
    assert any("gap 2/2" in m for m in messages)
    assert any("attempt 1/3" in m for m in messages)
    assert any(str(START) in m and str(START + STEP_1H) in m for m in messages)  # timestamp range
    assert any("ok (" in m for m in messages)  # outcome of a successful attempt
    assert any("done in" in m for m in messages)  # per-gap completion summary


def test_progress_reports_failed_attempts_with_outcome_before_retrying():
    messages: list[str] = []
    attempts = {"n": 0}

    def fail_once_then_succeed(call):
        if call["interval"] != "1m":
            return []  # native 1h cross-check - nothing there, as expected for a genuine gap
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise requests.exceptions.ConnectionError("simulated")
        return _60_minutes(call["start_time_ms"])

    client = _CallLoggingClient(fail_once_then_succeed)
    gap = _gap(START, START - STEP_1H, START + STEP_1H, 1)
    run_gap_forensics(
        client, SYMBOL, "1h", [], [gap], max_retries=3, sleep_fn=lambda s: None,
        now_fn=lambda: 0.0, on_progress=messages.append,
    )
    assert any("FAILED" in m and "retrying" in m for m in messages)
    assert any("ok (" in m for m in messages)


def test_cli_progress_printer_flushes_stdout_immediately(monkeypatch, capsys):
    from trading_agent.cli.main import _gap_audit_progress_printer

    flush_calls = {"n": 0}
    real_flush = __import__("sys").stdout.flush

    def counting_flush():
        flush_calls["n"] += 1
        return real_flush()

    monkeypatch.setattr("sys.stdout.flush", counting_flush)
    printer = _gap_audit_progress_printer()
    printer("hello")
    printer("world")
    assert flush_calls["n"] >= 2  # at least once per call (click.echo may also flush internally)
    assert "hello" in capsys.readouterr().out


# --- 8. Requirement: Control+C must exit cleanly with a partial summary. ---


def test_keyboard_interrupt_during_a_later_gap_yields_a_clean_partial_report():
    gap1 = _gap(START, START - STEP_1H, START + STEP_1H, 1)
    gap2 = _gap(START + 10 * STEP_1H, START + 9 * STEP_1H, START + 11 * STEP_1H, 1)
    gap3 = _gap(START + 20 * STEP_1H, START + 19 * STEP_1H, START + 21 * STEP_1H, 1)

    call_count = {"n": 0}

    def respond(call):
        if call["interval"] != "1m":
            return []  # native 1h cross-check - nothing there, as expected for a genuine gap
        call_count["n"] += 1
        if call_count["n"] == 2:  # interrupt partway through the SECOND gap's own batch fetch
            raise KeyboardInterrupt()
        return _60_minutes(call["start_time_ms"])

    client = _CallLoggingClient(respond)
    report = run_gap_forensics(
        client, SYMBOL, "1h", [], [gap1, gap2, gap3], max_retries=1, sleep_fn=lambda s: None, now_fn=lambda: 0.0,
    )

    assert report.interrupted is True
    assert report.gaps_processed == 1  # only the first gap fully completed
    assert len(report.gap_results) == 1
    assert report.gap_results[0].gap == gap1


def test_keyboard_interrupt_raised_from_sleep_during_backoff_is_also_caught_cleanly():
    def always_fail(call):
        raise requests.exceptions.ConnectionError("simulated")

    client = _CallLoggingClient(always_fail)
    gap = _gap(START, START - STEP_1H, START + STEP_1H, 1)

    def interrupting_sleep(seconds):
        raise KeyboardInterrupt()

    report = run_gap_forensics(
        client, SYMBOL, "1h", [], [gap], max_retries=3, sleep_fn=interrupting_sleep, now_fn=lambda: 0.0,
    )
    assert report.interrupted is True
    assert report.gaps_processed == 0
    assert report.gap_results == []


def test_apply_gap_recovery_refuses_an_interrupted_report(tmp_path):
    gap1 = _gap(START, START - STEP_1H, START + STEP_1H, 1)
    gap2 = _gap(START + 10 * STEP_1H, START + 9 * STEP_1H, START + 11 * STEP_1H, 1)
    call_count = {"n": 0}

    def respond(call):
        if call["interval"] != "1m":
            return []  # native 1h cross-check - nothing there, as expected for a genuine gap
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise KeyboardInterrupt()
        return _60_minutes(call["start_time_ms"])

    client = _CallLoggingClient(respond)
    report = run_gap_forensics(client, SYMBOL, "1h", [], [gap1, gap2], max_retries=1, sleep_fn=lambda s: None, now_fn=lambda: 0.0)
    assert report.interrupted is True

    db_path = tmp_path / "candles.db"
    with CandleStore(db_path) as store:
        store.store_candles_and_gaps([], [gap1, gap2], SYMBOL, "1h", detected_at_ms=1)
        with pytest.raises(IncompleteAuditError):
            apply_gap_recovery(store, SYMBOL, "1h", report)
        # Nothing was stored - the refusal happened before any write.
        assert store.get_candles(SYMBOL, "1h") == []


# --- 9. Requirement: audit remains strictly read-only (still true after
# this rewrite - unchanged behavior, re-asserted here for this module). ---


def test_run_gap_forensics_still_never_writes_even_with_checkpoint_and_progress(tmp_path):
    gap = _gap(START, START - STEP_1H, START + STEP_1H, 1)

    def respond(call):
        if call["interval"] != "1m":
            return []  # native 1h cross-check - nothing there, as expected for a genuine gap
        return _60_minutes(call["start_time_ms"])

    client = _CallLoggingClient(respond)
    db_path = tmp_path / "candles.db"
    checkpoint = GapAuditCheckpoint(tmp_path / "checkpoint.json")
    with CandleStore(db_path) as store:
        store.store_candles_and_gaps([], [gap], SYMBOL, "1h", detected_at_ms=1)
        run_gap_forensics(
            client, SYMBOL, "1h", [], [gap], max_retries=1, sleep_fn=lambda s: None, now_fn=lambda: 0.0,
            checkpoint=checkpoint, on_progress=lambda m: None,
        )
        assert store.get_candles(SYMBOL, "1h") == []
        assert store.get_gaps(SYMBOL, "1h") == [gap]


# --- 10. Requirement: checkpoint/resume so an already-audited gap is
# never re-downloaded. ---


def test_a_second_run_with_the_same_checkpoint_does_not_refetch_a_completed_gap(tmp_path):
    gap1 = _gap(START, START - STEP_1H, START + STEP_1H, 1)
    gap2 = _gap(START + 10 * STEP_1H, START + 9 * STEP_1H, START + 11 * STEP_1H, 1)

    def respond(call):
        if call["interval"] != "1m":
            return []  # native 1h cross-check - nothing there, as expected for a genuine gap
        return _60_minutes(call["start_time_ms"])

    checkpoint_path = tmp_path / "checkpoint.json"

    client1 = _CallLoggingClient(respond)
    checkpoint1 = GapAuditCheckpoint(checkpoint_path)
    report1 = run_gap_forensics(
        client1, SYMBOL, "1h", [], [gap1, gap2], max_retries=1, sleep_fn=lambda s: None, now_fn=lambda: 0.0,
        checkpoint=checkpoint1,
    )
    assert report1.interrupted is False
    first_run_call_count = len(client1.calls_for("1m"))
    assert first_run_call_count > 0

    # A FRESH checkpoint object loaded from the SAME file - proves real
    # on-disk persistence, not just in-memory reuse of the same instance.
    client2 = _CallLoggingClient(respond)
    checkpoint2 = GapAuditCheckpoint(checkpoint_path)
    report2 = run_gap_forensics(
        client2, SYMBOL, "1h", [], [gap1, gap2], max_retries=1, sleep_fn=lambda s: None, now_fn=lambda: 0.0,
        checkpoint=checkpoint2,
    )

    assert len(client2.calls_for("1m")) == 0  # NOT re-downloaded at all
    assert report2.gaps_processed == 2
    assert report2.fully_recoverable_hours == report1.fully_recoverable_hours


def test_resuming_after_an_interruption_only_refetches_the_uncompleted_gap(tmp_path):
    gap1 = _gap(START, START - STEP_1H, START + STEP_1H, 1)
    gap2 = _gap(START + 10 * STEP_1H, START + 9 * STEP_1H, START + 11 * STEP_1H, 1)
    checkpoint_path = tmp_path / "checkpoint.json"

    call_count = {"n": 0}

    def interrupt_on_second_gap(call):
        if call["interval"] != "1m":
            return []  # native 1h cross-check - nothing there, as expected for a genuine gap
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise KeyboardInterrupt()
        return _60_minutes(call["start_time_ms"])

    client1 = _CallLoggingClient(interrupt_on_second_gap)
    checkpoint1 = GapAuditCheckpoint(checkpoint_path)
    report1 = run_gap_forensics(
        client1, SYMBOL, "1h", [], [gap1, gap2], max_retries=1, sleep_fn=lambda s: None, now_fn=lambda: 0.0,
        checkpoint=checkpoint1,
    )
    assert report1.interrupted is True
    assert report1.gaps_processed == 1

    # Resume: gap1 is checkpointed and skipped; only gap2 is fetched.
    def respond(call):
        if call["interval"] != "1m":
            return []  # native 1h cross-check - nothing there, as expected for a genuine gap
        return _60_minutes(call["start_time_ms"])

    client2 = _CallLoggingClient(respond)
    checkpoint2 = GapAuditCheckpoint(checkpoint_path)
    report2 = run_gap_forensics(
        client2, SYMBOL, "1h", [], [gap1, gap2], max_retries=1, sleep_fn=lambda s: None, now_fn=lambda: 0.0,
        checkpoint=checkpoint2,
    )
    assert report2.interrupted is False
    assert report2.gaps_processed == 2
    assert len(client2.calls_for("1m")) == 1  # only gap2's own request - gap1 was skipped


def test_checkpoint_get_returns_none_when_the_gap_itself_has_changed(tmp_path):
    original_gap = _gap(START, START - STEP_1H, START + STEP_1H, 1)
    changed_gap = _gap(START, START - STEP_1H, START + STEP_1H, 2)  # missing_intervals differs

    checkpoint = GapAuditCheckpoint(tmp_path / "checkpoint.json")

    def respond(call):
        if call["interval"] != "1m":
            return []  # native 1h cross-check - nothing there, as expected for a genuine gap
        return _60_minutes(call["start_time_ms"])

    client = _CallLoggingClient(respond)
    run_gap_forensics(client, SYMBOL, "1h", [], [original_gap], max_retries=1, sleep_fn=lambda s: None, now_fn=lambda: 0.0, checkpoint=checkpoint)

    assert checkpoint.get(original_gap) is not None
    assert checkpoint.get(changed_gap) is None  # different identity - never a stale hit


def test_checkpoint_tolerates_a_missing_or_corrupted_file(tmp_path):
    missing_path = tmp_path / "does_not_exist.json"
    checkpoint = GapAuditCheckpoint(missing_path)
    assert checkpoint.processed_count() == 0
    assert checkpoint.get(_gap(START, START - STEP_1H, START + STEP_1H, 1)) is None

    corrupt_path = tmp_path / "corrupt.json"
    corrupt_path.write_text("{not valid json::")
    corrupt_checkpoint = GapAuditCheckpoint(corrupt_path)
    assert corrupt_checkpoint.processed_count() == 0


def test_checkpoint_record_writes_immediately_and_survives_reload(tmp_path):
    path = tmp_path / "checkpoint.json"
    checkpoint = GapAuditCheckpoint(path)
    gap = _gap(START, START - STEP_1H, START + STEP_1H, 1)

    def respond(call):
        if call["interval"] != "1m":
            return []  # native 1h cross-check - nothing there, as expected for a genuine gap
        return _60_minutes(call["start_time_ms"])

    client = _CallLoggingClient(respond)
    run_gap_forensics(client, SYMBOL, "1h", [], [gap], max_retries=1, sleep_fn=lambda s: None, now_fn=lambda: 0.0, checkpoint=checkpoint)

    assert path.exists()
    on_disk = json.loads(path.read_text())
    assert len(on_disk) == 1

    reloaded = GapAuditCheckpoint(path)
    cached = reloaded.get(gap)
    assert cached is not None
    assert cached.missing_hours[0].outcome is RecoveryOutcome.FULLY_RECOVERABLE
    assert cached.missing_hours[0].reconstructed_candle is not None
    assert cached.missing_hours[0].provenance is not None


def test_checkpoint_clear_removes_the_file_and_forces_a_fresh_audit(tmp_path):
    path = tmp_path / "checkpoint.json"
    gap = _gap(START, START - STEP_1H, START + STEP_1H, 1)

    def respond(call):
        if call["interval"] != "1m":
            return []  # native 1h cross-check - nothing there, as expected for a genuine gap
        return _60_minutes(call["start_time_ms"])

    client1 = _CallLoggingClient(respond)
    checkpoint = GapAuditCheckpoint(path)
    run_gap_forensics(client1, SYMBOL, "1h", [], [gap], max_retries=1, sleep_fn=lambda s: None, now_fn=lambda: 0.0, checkpoint=checkpoint)
    assert path.exists()

    checkpoint.clear()
    assert not path.exists()
    assert checkpoint.processed_count() == 0

    client2 = _CallLoggingClient(respond)
    run_gap_forensics(client2, SYMBOL, "1h", [], [gap], max_retries=1, sleep_fn=lambda s: None, now_fn=lambda: 0.0, checkpoint=checkpoint)
    assert len(client2.calls_for("1m")) == 1  # re-fetched since the checkpoint was cleared


# --- 12. Requirement: no strategy/candidate/parameter/scorecard/execution
# change - a source-level regression lock for this specific rewrite. ---


def test_gap_recovery_module_never_imports_execution_or_backtest_machinery():
    # Checks actual CALL/IMPORT patterns, not prose - this module's own
    # docstring legitimately DISCUSSES run_segment/generate_signal (to
    # explain what it deliberately never does), so a bare substring check
    # would false-positive on its own documentation.
    import trading_agent.data.gap_recovery as module

    source = __import__("inspect").getsource(module)
    for forbidden in ("run_segment(", ".generate_signal(", "RiskEngine(", "BacktestBroker(", "from trading_agent.backtest"):
        assert forbidden not in source, f"unexpected {forbidden!r} in data/gap_recovery.py"
