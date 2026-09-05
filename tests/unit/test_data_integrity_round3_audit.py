"""Pre-evaluation data-integrity audit for commit 47a353a (the Round-3
`multitimeframe_breakout_E1_round3` candidate).

This file is a SEPARATE, self-contained audit artifact - it deliberately
does not assume or rely on any other test file's coverage, even where
overlap exists, because its purpose is to stand alone as direct proof of
each of the 9 invariants below, traceable one-to-one back to the audit
request. Nothing here queries a real database, fetches real market data,
runs `research-round3` for real, connects to Testnet, or alters any
strategy parameter - every fixture is synthetic and every store is a
fresh `tmp_path` SQLite file.

The audit's conclusion (see each section below for its own proof): the
existing `data/storage.py` schema is ALREADY fully interval-aware
(`PRIMARY KEY (symbol, interval, open_time_ms)`, and every read/write path
filters or targets on `interval` explicitly) - no schema migration and no
separate database file is structurally required. Invariant 9's own test
below locks this fact in as a regression: if a future change ever weakens
the schema's interval-awareness, this file fails and a separate 1h
database becomes necessary again (see `config/round3_1h.yaml`'s own
header comment for the resulting operator guidance either way).
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from decimal import Decimal

from trading_agent.cli.main import research_round3_cmd
from trading_agent.data.gap_detection import partition_into_segments
from trading_agent.data.models import Candle, interval_to_ms
from trading_agent.data.storage import CandleStore
from trading_agent.research.candidate_registry_round3 import REQUIRED_MARKET_INTERVAL
from trading_agent.research.candidates.multitimeframe_breakout import (
    _aggregate_completed_buckets,
    _four_h_bucket_start_ms,
    _weekly_bucket_start_ms,
)
from trading_agent.research.cutoff import RESEARCH_CUTOFF_MS, assert_pre_cutoff, split_at_cutoff

STEP_1H = interval_to_ms("1h")
STEP_4H = interval_to_ms("4h")


def _candle(symbol: str, interval: str, open_time_ms: int, step_ms: int, close: str = "100") -> Candle:
    return Candle(
        symbol=symbol,
        interval=interval,
        open_time_ms=open_time_ms,
        close_time_ms=open_time_ms + step_ms - 1,
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=Decimal(1),
    )


def _hourly_candle(i: int, start: int, close: str = "100") -> Candle:
    return _candle("BTCUSDT", "1h", start + i * STEP_1H, STEP_1H, close=close)


# 1970-01-05T00:00:00Z is a Monday, and also a 4h boundary (345_600_000 ms
# is an exact multiple of both 3_600_000 and 14_400_000) - a convenient,
# independently-verifiable grid-aligned start for every aggregation fixture
# below.
_GRID_ALIGNED_START = 4 * 24 * 3600 * 1000


# --- 1. Candle storage uniquely identifies symbol + interval + open_time_ms. ---


def test_candles_table_primary_key_is_exactly_symbol_interval_open_time(tmp_path):
    db_path = tmp_path / "candles.db"
    with CandleStore(db_path) as store:
        cols = store._conn.execute("PRAGMA table_info(candles)").fetchall()
    # PRAGMA table_info columns: (cid, name, type, notnull, dflt_value, pk).
    # pk > 0 marks a column's 1-based position within a composite PRIMARY
    # KEY; every other column must have pk == 0.
    pk_cols = sorted((row[5], row[1]) for row in cols if row[5] > 0)
    assert [name for _, name in pk_cols] == ["symbol", "interval", "open_time_ms"]


def test_candle_gaps_table_primary_key_is_exactly_symbol_interval_expected_open_time(tmp_path):
    db_path = tmp_path / "candles.db"
    with CandleStore(db_path) as store:
        cols = store._conn.execute("PRAGMA table_info(candle_gaps)").fetchall()
    pk_cols = sorted((row[5], row[1]) for row in cols if row[5] > 0)
    assert [name for _, name in pk_cols] == ["symbol", "interval", "expected_open_time_ms"]


def test_sqlite_itself_rejects_a_duplicate_symbol_interval_open_time_row_via_raw_insert(tmp_path):
    # Bypasses the upsert helper entirely and inserts a raw duplicate
    # (symbol, interval, open_time_ms) row with a bare INSERT (no ON
    # CONFLICT clause) - proves the uniqueness is enforced by SQLite's own
    # PRIMARY KEY constraint, not merely by application-level upsert logic
    # that a future caller could bypass.
    import sqlite3

    db_path = tmp_path / "candles.db"
    with CandleStore(db_path):
        pass  # just to create the schema
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO candles (symbol, interval, open_time_ms, close_time_ms, open, high, low, close, volume) "
            "VALUES ('BTCUSDT', '1h', 1000, 1999, '1', '1', '1', '1', '1')"
        )
        conn.commit()
        raised = False
        try:
            conn.execute(
                "INSERT INTO candles (symbol, interval, open_time_ms, close_time_ms, open, high, low, close, volume) "
                "VALUES ('BTCUSDT', '1h', 1000, 1999, '2', '2', '2', '2', '2')"
            )
        except sqlite3.IntegrityError:
            raised = True
        assert raised
    finally:
        conn.close()


def test_same_symbol_and_open_time_but_different_interval_are_distinct_rows(tmp_path):
    # Same symbol, same open_time_ms, DIFFERENT interval - must coexist as
    # two independent rows, never collide.
    db_path = tmp_path / "candles.db"
    c_1h = _candle("BTCUSDT", "1h", 1_000_000, STEP_1H, close="111")
    c_4h = _candle("BTCUSDT", "4h", 1_000_000, STEP_4H, close="444")
    with CandleStore(db_path) as store:
        store.upsert_candles([c_1h, c_4h])
        fetched_1h = store.get_candles("BTCUSDT", "1h")
        fetched_4h = store.get_candles("BTCUSDT", "4h")
    assert [c.close for c in fetched_1h] == [Decimal(111)]
    assert [c.close for c in fetched_4h] == [Decimal(444)]


# --- 2. Changing market.interval from 4h to 1h cannot overwrite, mix
# with, or cause queries to return existing 4h candles. ---


def test_upserting_1h_candles_never_mutates_a_preexisting_4h_row_at_the_same_open_time(tmp_path):
    db_path = tmp_path / "candles.db"
    original_4h = _candle("BTCUSDT", "4h", 1_000_000, STEP_4H, close="444")
    with CandleStore(db_path) as store:
        store.upsert_candles([original_4h])
        # Simulate switching market.interval to "1h" and fetching fresh
        # 1h data that happens to share the exact same open_time_ms.
        store.upsert_candles([_candle("BTCUSDT", "1h", 1_000_000, STEP_1H, close="111")])
        still_4h = store.get_candles("BTCUSDT", "4h")
        now_1h = store.get_candles("BTCUSDT", "1h")
    assert still_4h == [original_4h]
    assert [c.close for c in now_1h] == [Decimal(111)]


def test_reupserting_the_same_4h_candle_after_switching_to_1h_only_updates_the_4h_row(tmp_path):
    # Re-running `fetch-data` against the 4h config later (e.g. to top up
    # recent candles) after 1h data has already been fetched into the SAME
    # database file must only ever touch the 4h row.
    db_path = tmp_path / "candles.db"
    with CandleStore(db_path) as store:
        store.upsert_candles([_candle("BTCUSDT", "4h", 1_000_000, STEP_4H, close="444")])
        store.upsert_candles([_candle("BTCUSDT", "1h", 1_000_000, STEP_1H, close="111")])
        # Re-fetch/upsert the 4h candle with an updated close (e.g. the
        # exchange revised a not-yet-final value on a subsequent request).
        store.upsert_candles([_candle("BTCUSDT", "4h", 1_000_000, STEP_4H, close="450")])
        fetched_4h = store.get_candles("BTCUSDT", "4h")
        fetched_1h = store.get_candles("BTCUSDT", "1h")
    assert [c.close for c in fetched_4h] == [Decimal(450)]  # updated in place
    assert [c.close for c in fetched_1h] == [Decimal(111)]  # completely untouched


def test_get_candles_for_one_interval_never_returns_rows_of_another_interval(tmp_path):
    db_path = tmp_path / "candles.db"
    candles_4h = [_candle("BTCUSDT", "4h", 1_000_000 + i * STEP_4H, STEP_4H) for i in range(5)]
    candles_1h = [_candle("BTCUSDT", "1h", 1_000_000 + i * STEP_1H, STEP_1H) for i in range(20)]
    with CandleStore(db_path) as store:
        store.upsert_candles(candles_4h + candles_1h)
        fetched_4h = store.get_candles("BTCUSDT", "4h")
        fetched_1h = store.get_candles("BTCUSDT", "1h")
    assert len(fetched_4h) == 5
    assert len(fetched_1h) == 20
    assert all(c.interval == "4h" for c in fetched_4h)
    assert all(c.interval == "1h" for c in fetched_1h)


def test_latest_close_time_ms_is_also_interval_isolated(tmp_path):
    db_path = tmp_path / "candles.db"
    with CandleStore(db_path) as store:
        store.upsert_candles([_candle("BTCUSDT", "4h", 1_000_000, STEP_4H)])
        store.upsert_candles([_candle("BTCUSDT", "1h", 2_000_000, STEP_1H)])
        assert store.latest_close_time_ms("BTCUSDT", "4h") == 1_000_000 + STEP_4H - 1
        assert store.latest_close_time_ms("BTCUSDT", "1h") == 2_000_000 + STEP_1H - 1


def test_upsert_conflict_target_includes_interval_column(tmp_path):
    # Architectural regression: the ON CONFLICT target must name `interval`
    # explicitly, not just `(symbol, open_time_ms)` - otherwise a 1h and a
    # 4h candle sharing an open_time_ms would collide as "the same row" on
    # write, even though the schema's own PRIMARY KEY says otherwise.
    source = inspect.getsource(CandleStore._upsert_candles_no_commit)
    assert "ON CONFLICT(symbol, interval, open_time_ms)" in source


# --- 3. research-round3 reads only BTCUSDT 1h candles. ---


def test_required_market_interval_for_round3_is_1h():
    assert REQUIRED_MARKET_INTERVAL == "1h"


def test_research_round3_cmd_source_overrides_interval_and_reads_only_that_interval():
    source = inspect.getsource(research_round3_cmd.callback)
    assert 'update={"interval": ROUND3_MARKET_INTERVAL}' in source
    assert "store.get_candles(config.market.symbol, ROUND3_MARKET_INTERVAL)" in source


def test_round3_read_path_ignores_other_intervals_and_other_symbols_in_the_same_database(tmp_path):
    # Direct data-level proof (not just source inspection): populate ONE
    # database file with BTCUSDT 1h, BTCUSDT 4h, and ETHUSDT 1h candles,
    # then perform the EXACT read `research_round3_cmd` performs
    # (`store.get_candles(config.market.symbol, ROUND3_MARKET_INTERVAL)`
    # with the default config.market.symbol == "BTCUSDT") and confirm only
    # the intended rows come back.
    db_path = tmp_path / "candles.db"
    btc_1h = [_candle("BTCUSDT", "1h", 1_000_000 + i * STEP_1H, STEP_1H, close="1") for i in range(3)]
    btc_4h = [_candle("BTCUSDT", "4h", 1_000_000 + i * STEP_4H, STEP_4H, close="2") for i in range(3)]
    eth_1h = [_candle("ETHUSDT", "1h", 1_000_000 + i * STEP_1H, STEP_1H, close="3") for i in range(3)]
    with CandleStore(db_path) as store:
        store.upsert_candles(btc_1h + btc_4h + eth_1h)
        result = store.get_candles("BTCUSDT", REQUIRED_MARKET_INTERVAL)
    assert len(result) == 3
    assert all(c.symbol == "BTCUSDT" and c.interval == "1h" for c in result)


# --- 4. Weekly buckets align with Binance 1w UTC boundaries (Monday 00:00 UTC). ---


def _iso_ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


def test_weekly_bucket_start_matches_known_binance_monday_utc_opens():
    # 2024-01-01, 2024-01-08, 2024-01-15 are real, independently verifiable
    # Monday 00:00 UTC dates (Binance's own 1w kline open convention).
    for monday_iso in ["2024-01-01T00:00:00+00:00", "2024-01-08T00:00:00+00:00", "2024-01-15T00:00:00+00:00"]:
        monday_ms = _iso_ms(monday_iso)
        # The Monday's own open, and every hour strictly within that week,
        # must all bucket to that same Monday 00:00 UTC.
        for offset_hours in [0, 1, 12, 24, 24 * 6 + 23]:  # up to Sunday 23:00
            t = monday_ms + offset_hours * STEP_1H
            assert _weekly_bucket_start_ms(t) == monday_ms


def test_weekly_bucket_start_is_always_a_monday_at_midnight_utc():
    for i in range(200):
        t = _GRID_ALIGNED_START + i * 37 * STEP_1H  # arbitrary, non-week-aligned stride
        start = _weekly_bucket_start_ms(t)
        dt = datetime.fromtimestamp(start / 1000, tz=UTC)
        assert dt.weekday() == 0
        assert (dt.hour, dt.minute, dt.second, dt.microsecond) == (0, 0, 0, 0)


def test_weekly_bucket_boundary_is_exclusive_on_the_next_monday():
    monday_ms = _iso_ms("2024-01-08T00:00:00+00:00")
    one_hour_before = monday_ms - STEP_1H
    assert _weekly_bucket_start_ms(one_hour_before) == _iso_ms("2024-01-01T00:00:00+00:00")
    assert _weekly_bucket_start_ms(monday_ms) == monday_ms


# --- 5. Four-hour buckets align exactly with Binance 4h UTC boundaries. ---


def test_four_h_bucket_start_matches_known_binance_4h_utc_opens():
    # Binance 4h klines open at 00:00, 04:00, 08:00, 12:00, 16:00, 20:00 UTC.
    day = _iso_ms("2024-03-14T00:00:00+00:00")
    expected_opens = [day + h * 3600 * 1000 for h in (0, 4, 8, 12, 16, 20)]
    for open_hour, expected_open in zip((0, 4, 8, 12, 16, 20), expected_opens, strict=True):
        for minute_offset in (0, 59, 3 * 60 + 59):  # top of hour .. last minute of the 4h window
            t = day + open_hour * 3600 * 1000 + minute_offset * 60 * 1000
            assert _four_h_bucket_start_ms(t) == expected_open


def test_four_h_bucket_start_is_always_on_the_six_daily_utc_boundaries():
    for i in range(500):
        t = _GRID_ALIGNED_START + i * 37 * STEP_1H
        start = _four_h_bucket_start_ms(t)
        dt = datetime.fromtimestamp(start / 1000, tz=UTC)
        assert dt.hour % 4 == 0
        assert (dt.minute, dt.second, dt.microsecond) == (0, 0, 0)


# --- 6. Only complete 4h and weekly buckets are exposed to a 1h decision. ---


def test_aggregation_excludes_a_trailing_incomplete_4h_bucket():
    # 6 completed 1h candles = one complete 4h bucket plus 2 leftover
    # hours that do NOT yet make a full 4h bucket - the leftover must
    # never be exposed as a (fabricated, partial) bucket.
    candles = [_hourly_candle(i, _GRID_ALIGNED_START) for i in range(6)]
    buckets = _aggregate_completed_buckets(candles, 4, _four_h_bucket_start_ms)
    assert len(buckets) == 1
    assert buckets[0].candle.open_time_ms == _GRID_ALIGNED_START
    assert buckets[0].last_hour_index == 3  # the 4th candle (index 3) closes the only complete bucket


def test_aggregation_excludes_a_trailing_incomplete_weekly_bucket():
    hours_in_five_days = 5 * 24
    candles = [_hourly_candle(i, _GRID_ALIGNED_START) for i in range(hours_in_five_days)]
    buckets = _aggregate_completed_buckets(candles, 7 * 24, _weekly_bucket_start_ms)
    assert buckets == []  # not even one full week yet - zero buckets, never a partial one


def test_appending_a_1h_candle_that_completes_a_bucket_exposes_exactly_that_bucket_and_no_more():
    hours_per_bucket = 4
    incomplete = [_hourly_candle(i, _GRID_ALIGNED_START) for i in range(3)]
    assert _aggregate_completed_buckets(incomplete, hours_per_bucket, _four_h_bucket_start_ms) == []
    completed = incomplete + [_hourly_candle(3, _GRID_ALIGNED_START)]
    buckets = _aggregate_completed_buckets(completed, hours_per_bucket, _four_h_bucket_start_ms)
    assert len(buckets) == 1
    assert buckets[0].last_hour_index == 3


def test_a_bucket_only_ever_derives_from_hours_strictly_before_the_current_1h_decision():
    # The Nth 1h candle (index N-1) is "now" - a bucket can only be
    # exposed once every one of its constituent hours (all <= N-1) is
    # already present, i.e. the bucket's own close must be causally
    # knowable as of the current decision. Feeding exactly up to the
    # bucket's last hour exposes it; one candle short does not.
    one_short = [_hourly_candle(i, _GRID_ALIGNED_START) for i in range(3)]
    exact = [_hourly_candle(i, _GRID_ALIGNED_START) for i in range(4)]
    assert _aggregate_completed_buckets(one_short, 4, _four_h_bucket_start_ms) == []
    assert len(_aggregate_completed_buckets(exact, 4, _four_h_bucket_start_ms)) == 1


# --- 7. Known historical gaps create segment boundaries and never
# fabricate aggregate candles. ---


def test_a_gap_spanning_a_4h_bucket_boundary_produces_no_aggregate_bucket_for_it():
    # Hours 0..3 form a complete first 4h bucket. Hour 4 (which would
    # start the SECOND 4h bucket) is missing - replaced by hour 5, so the
    # second bucket only ever has 3 of its 4 required hours (5, 6, 7) and
    # is never fabricated; a third bucket (hours 8-11) is complete again.
    start = _GRID_ALIGNED_START
    candles = [_hourly_candle(i, start) for i in range(4)]  # bucket 1: hours 0-3, complete
    candles += [_hourly_candle(i, start) for i in (5, 6, 7)]  # bucket 2: missing hour 4 - incomplete
    candles += [_hourly_candle(i, start) for i in range(8, 12)]  # bucket 3: hours 8-11, complete

    # partition_into_segments (the real upstream gap-detector) must itself
    # confirm exactly one gap here and split into two segments.
    segmentation = partition_into_segments(candles, "1h")
    assert len(segmentation.gaps) == 1
    assert segmentation.gaps[0].missing_intervals == 1
    assert len(segmentation.segments) == 2
    assert [c.open_time_ms for c in segmentation.segments[0]] == [start + i * STEP_1H for i in range(4)]
    assert [c.open_time_ms for c in segmentation.segments[1]] == [start + i * STEP_1H for i in (5, 6, 7, 8, 9, 10, 11)]

    # Aggregating the ORIGINAL (gapped) candle list directly must produce
    # exactly buckets 1 and 3 - never a fabricated bucket 2, and never
    # silently bridging the gap by treating hour 5 as if it were hour 4.
    buckets = _aggregate_completed_buckets(candles, 4, _four_h_bucket_start_ms)
    assert [b.candle.open_time_ms for b in buckets] == [start, start + 8 * STEP_1H]


def test_aggregating_each_gap_segment_independently_matches_aggregating_the_whole_gapped_list():
    # Ties gap_detection's segmentation directly to the aggregator's own
    # gap-tolerance: running the aggregator separately over each segment
    # `partition_into_segments` produces must yield exactly the same
    # buckets as running it once over the original, ungapped-looking
    # concatenation - proving the aggregator's own boundary checks (not
    # just upstream segmentation) are what prevents fabrication.
    start = _GRID_ALIGNED_START
    candles = [_hourly_candle(i, start) for i in range(4)]
    candles += [_hourly_candle(i, start) for i in (5, 6, 7)]
    candles += [_hourly_candle(i, start) for i in range(8, 12)]

    segmentation = partition_into_segments(candles, "1h")
    per_segment_buckets = []
    for segment in segmentation.segments:
        per_segment_buckets.extend(_aggregate_completed_buckets(segment, 4, _four_h_bucket_start_ms))
    whole_list_buckets = _aggregate_completed_buckets(candles, 4, _four_h_bucket_start_ms)

    assert [b.candle.open_time_ms for b in per_segment_buckets] == [b.candle.open_time_ms for b in whole_list_buckets]


def test_a_gap_never_produces_an_aggregate_candle_whose_ohlc_blends_data_across_the_gap():
    # If fabrication ever crept in, the "bridged" bucket's OHLC would mix
    # pre-gap and post-gap prices. Use distinguishable closes on each side
    # of the gap and prove no such blended bucket exists at all.
    start = _GRID_ALIGNED_START
    pre_gap = [_hourly_candle(i, start, close="100") for i in range(4)]
    post_gap = [_hourly_candle(i, start, close="999") for i in (5, 6, 7)]  # hour 4 missing, incomplete bucket
    candles = pre_gap + post_gap
    buckets = _aggregate_completed_buckets(candles, 4, _four_h_bucket_start_ms)
    assert len(buckets) == 1
    assert buckets[0].candle.close == Decimal(100)  # only the complete, pre-gap bucket - never a blended one


# --- 8. The immutable research cutoff excludes every candle at or after
# 2025-05-16T00:00:00Z. ---


def test_cutoff_ms_is_exactly_the_documented_iso_instant():
    assert RESEARCH_CUTOFF_MS == _iso_ms("2025-05-16T00:00:00+00:00")


def test_split_at_cutoff_on_1h_candles_excludes_the_boundary_candle_itself():
    last_pre_cutoff_open = RESEARCH_CUTOFF_MS - STEP_1H  # 2025-05-15T23:00:00Z
    boundary_candle = _hourly_candle(0, RESEARCH_CUTOFF_MS)  # opens exactly AT cutoff
    candles = [_hourly_candle(0, last_pre_cutoff_open), boundary_candle]
    pre_cutoff, consumed = split_at_cutoff(candles)
    assert [c.open_time_ms for c in pre_cutoff] == [last_pre_cutoff_open]
    assert [c.open_time_ms for c in consumed] == [RESEARCH_CUTOFF_MS]
    assert_pre_cutoff(pre_cutoff)  # must not raise


def test_a_1h_candle_one_millisecond_before_cutoff_is_pre_cutoff_and_at_cutoff_is_not():
    just_before = _hourly_candle(0, RESEARCH_CUTOFF_MS - 1)
    exactly_at = _hourly_candle(0, RESEARCH_CUTOFF_MS)
    pre, consumed = split_at_cutoff([just_before, exactly_at])
    assert pre == [just_before]
    assert consumed == [exactly_at]


def test_research_round3_cmd_splits_at_cutoff_before_ever_scoring_a_candidate():
    source = inspect.getsource(research_round3_cmd.callback)
    assert "split_at_cutoff(candles)" in source
    assert "pre_cutoff" in source
    # The report call must be built from the pre-cutoff half, never the raw,
    # unfiltered `candles` read from the store.
    assert "build_round3_report(pre_cutoff," in source


# --- 9. Fetching 1h data can use a separate database/file if interval
# isolation is not structurally guaranteed - AUDIT CONCLUSION: it already
# is, so no separate file is required. This test locks that fact in as a
# regression: if it ever starts failing, the schema has regressed and
# `config/round3_1h.yaml`'s recommendation to reuse the same db_path is no
# longer safe. ---


def test_storage_schema_source_declares_interval_in_both_primary_keys():
    import trading_agent.data.storage as storage_module

    source = inspect.getsource(storage_module)
    assert "PRIMARY KEY (symbol, interval, open_time_ms)" in source
    assert "PRIMARY KEY (symbol, interval, expected_open_time_ms)" in source


def test_a_single_shared_database_file_safely_holds_both_4h_and_1h_history_for_the_same_symbol(tmp_path):
    # End-to-end proof that the SAME db_path (as config/round3_1h.yaml
    # deliberately reuses) is safe: a realistic mixed 4h+1h population,
    # read back independently, with neither interval's row count or
    # values disturbed by the other's presence.
    db_path = tmp_path / "shared.db"
    candles_4h = [_candle("BTCUSDT", "4h", 1_000_000 + i * STEP_4H, STEP_4H, close=str(i)) for i in range(50)]
    candles_1h = [_candle("BTCUSDT", "1h", 1_000_000 + i * STEP_1H, STEP_1H, close=str(i)) for i in range(200)]
    with CandleStore(db_path) as store:
        store.upsert_candles(candles_4h)
        store.upsert_candles(candles_1h)
        # Re-fetch/re-upsert 4h again (idempotent top-up), as an operator
        # would do periodically, interleaved with 1h fetches.
        store.upsert_candles(candles_4h)
        fetched_4h = store.get_candles("BTCUSDT", "4h")
        fetched_1h = store.get_candles("BTCUSDT", "1h")
    assert [c.close for c in fetched_4h] == [Decimal(str(i)) for i in range(50)]
    assert [c.close for c in fetched_1h] == [Decimal(str(i)) for i in range(200)]
