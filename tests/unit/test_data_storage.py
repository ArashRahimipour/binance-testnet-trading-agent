from decimal import Decimal

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
