import sqlite3
from decimal import Decimal

import pytest

from trading_agent.data.gap_detection import GapRecord
from trading_agent.data.models import Candle, interval_to_ms
from trading_agent.data.storage import CandleStore

INTERVAL = "4h"
STEP = interval_to_ms(INTERVAL)
START = 1_700_000_000_000


def _candle(open_time_ms: int) -> Candle:
    return Candle(
        symbol="BTCUSDT",
        interval=INTERVAL,
        open_time_ms=open_time_ms,
        close_time_ms=open_time_ms + STEP - 1,
        open=Decimal("100.12345678"),
        high=Decimal("101.00000000"),
        low=Decimal("99.00000000"),
        close=Decimal("100.5"),
        volume=Decimal("12.34"),
    )


def test_round_trip_preserves_decimal_precision(tmp_path):
    db_path = tmp_path / "candles.db"
    with CandleStore(db_path) as store:
        candle = _candle(START)
        store.upsert_candles([candle])
        fetched = store.get_candles("BTCUSDT", INTERVAL)
    assert len(fetched) == 1
    assert fetched[0].open == Decimal("100.12345678")
    assert fetched[0] == candle


def test_upsert_is_idempotent_on_conflict(tmp_path):
    db_path = tmp_path / "candles.db"
    with CandleStore(db_path) as store:
        store.upsert_candles([_candle(START)])
        store.upsert_candles([_candle(START)])  # same primary key, re-inserted
        fetched = store.get_candles("BTCUSDT", INTERVAL)
    assert len(fetched) == 1


def test_get_candles_time_range_filter(tmp_path):
    db_path = tmp_path / "candles.db"
    candles = [_candle(START + i * STEP) for i in range(5)]
    with CandleStore(db_path) as store:
        store.upsert_candles(candles)
        fetched = store.get_candles(
            "BTCUSDT", INTERVAL, start_time_ms=START + STEP, end_time_ms=START + 2 * STEP
        )
    assert [c.open_time_ms for c in fetched] == [START + STEP, START + 2 * STEP]


def test_latest_close_time_ms(tmp_path):
    db_path = tmp_path / "candles.db"
    candles = [_candle(START + i * STEP) for i in range(3)]
    with CandleStore(db_path) as store:
        assert store.latest_close_time_ms("BTCUSDT", INTERVAL) is None
        store.upsert_candles(candles)
        assert store.latest_close_time_ms("BTCUSDT", INTERVAL) == candles[-1].close_time_ms


# --- Gap manifest persistence, idempotency, and atomicity. ---


def _gap(previous_open: int, next_open: int, missing: int) -> GapRecord:
    return GapRecord(
        expected_open_time_ms=previous_open + STEP,
        previous_open_time_ms=previous_open,
        next_open_time_ms=next_open,
        missing_intervals=missing,
    )


def test_store_candles_and_gaps_persists_both(tmp_path):
    db_path = tmp_path / "candles.db"
    candles = [_candle(START), _candle(START + 2 * STEP)]
    gaps = [_gap(START, START + 2 * STEP, 1)]
    with CandleStore(db_path) as store:
        store.store_candles_and_gaps(candles, gaps, "BTCUSDT", INTERVAL, detected_at_ms=1000)
        fetched_candles = store.get_candles("BTCUSDT", INTERVAL)
        fetched_gaps = store.get_gaps("BTCUSDT", INTERVAL)
    assert len(fetched_candles) == 2
    assert len(fetched_gaps) == 1
    assert fetched_gaps[0].previous_open_time_ms == START
    assert fetched_gaps[0].next_open_time_ms == START + 2 * STEP
    assert fetched_gaps[0].missing_intervals == 1


def test_rerunning_the_same_download_is_idempotent(tmp_path):
    db_path = tmp_path / "candles.db"
    candles = [_candle(START), _candle(START + 2 * STEP)]
    gaps = [_gap(START, START + 2 * STEP, 1)]
    with CandleStore(db_path) as store:
        store.store_candles_and_gaps(candles, gaps, "BTCUSDT", INTERVAL, detected_at_ms=1000)
        store.store_candles_and_gaps(candles, gaps, "BTCUSDT", INTERVAL, detected_at_ms=2000)
        fetched_candles = store.get_candles("BTCUSDT", INTERVAL)
        fetched_gaps = store.get_gaps("BTCUSDT", INTERVAL)
    assert len(fetched_candles) == 2  # not duplicated
    assert len(fetched_gaps) == 1  # not duplicated
    assert fetched_gaps[0].missing_intervals == 1


# --- Stale-gap deletion (gap_recovery.py's own storage requirement). ---


def test_stale_gap_expected_open_times_deletes_the_old_gap_row(tmp_path):
    db_path = tmp_path / "candles.db"
    original_gap = _gap(START, START + 2 * STEP, 1)  # expected_open_time_ms = START + STEP
    with CandleStore(db_path) as store:
        store.store_candles_and_gaps([], [original_gap], "BTCUSDT", INTERVAL, detected_at_ms=1000)
        assert len(store.get_gaps("BTCUSDT", INTERVAL)) == 1
        # The gap is now fully resolved - no replacement gap, but the old
        # row must be deleted, not merely left stale alongside nothing new.
        store.store_candles_and_gaps(
            [_candle(START + STEP)], [], "BTCUSDT", INTERVAL, detected_at_ms=2000,
            stale_gap_expected_open_times=[original_gap.expected_open_time_ms],
        )
        assert store.get_gaps("BTCUSDT", INTERVAL) == []


def test_stale_gap_expected_open_times_replaces_with_a_narrower_gap(tmp_path):
    db_path = tmp_path / "candles.db"
    original_gap = _gap(START, START + 3 * STEP, 2)  # missing START+STEP and START+2*STEP
    narrower_gap = _gap(START + STEP, START + 3 * STEP, 1)  # only START+2*STEP still missing
    with CandleStore(db_path) as store:
        store.store_candles_and_gaps([], [original_gap], "BTCUSDT", INTERVAL, detected_at_ms=1000)
        store.store_candles_and_gaps(
            [_candle(START + STEP)], [narrower_gap], "BTCUSDT", INTERVAL, detected_at_ms=2000,
            stale_gap_expected_open_times=[original_gap.expected_open_time_ms],
        )
        fetched_gaps = store.get_gaps("BTCUSDT", INTERVAL)
    assert len(fetched_gaps) == 1
    assert fetched_gaps[0].expected_open_time_ms == narrower_gap.expected_open_time_ms
    assert fetched_gaps[0].missing_intervals == 1


def test_stale_gap_expected_open_times_deleting_a_nonexistent_row_is_a_noop(tmp_path):
    db_path = tmp_path / "candles.db"
    with CandleStore(db_path) as store:
        # No prior gap at all - deleting a "stale" row that was never
        # there must not raise or otherwise misbehave.
        store.store_candles_and_gaps(
            [_candle(START)], [], "BTCUSDT", INTERVAL, detected_at_ms=1000,
            stale_gap_expected_open_times=[START + STEP],
        )
        assert store.get_gaps("BTCUSDT", INTERVAL) == []


def test_omitting_stale_gap_expected_open_times_preserves_old_behavior(tmp_path):
    # Default (None) must behave EXACTLY like before this parameter
    # existed - re-running fetch-data's own normal path is unaffected.
    db_path = tmp_path / "candles.db"
    gap = _gap(START, START + 2 * STEP, 1)
    with CandleStore(db_path) as store:
        store.store_candles_and_gaps([_candle(START)], [gap], "BTCUSDT", INTERVAL, detected_at_ms=1000)
        store.store_candles_and_gaps([_candle(START)], [gap], "BTCUSDT", INTERVAL, detected_at_ms=2000)
        fetched_gaps = store.get_gaps("BTCUSDT", INTERVAL)
    assert len(fetched_gaps) == 1
    assert fetched_gaps[0].missing_intervals == 1


def test_a_failed_write_leaves_no_partially_inconsistent_state(tmp_path):
    db_path = tmp_path / "candles.db"
    candles = [_candle(START), _candle(START + 2 * STEP)]
    # A gap row with a NULL-triggering bad value (None isn't a valid int
    # column) forces the second half of the transaction to fail after the
    # candles' executemany has already run but before either is committed.
    bad_gaps = [GapRecord(expected_open_time_ms=None, previous_open_time_ms=START, next_open_time_ms=START + 2 * STEP, missing_intervals=1)]  # type: ignore[arg-type]
    with CandleStore(db_path) as store:
        with pytest.raises(sqlite3.IntegrityError):
            store.store_candles_and_gaps(candles, bad_gaps, "BTCUSDT", INTERVAL, detected_at_ms=1000)
        # Neither the candles nor the gap made it in - fully rolled back.
        assert store.get_candles("BTCUSDT", INTERVAL) == []
        assert store.get_gaps("BTCUSDT", INTERVAL) == []
