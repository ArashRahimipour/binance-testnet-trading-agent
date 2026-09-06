from decimal import Decimal

import yaml

from tests.fixtures.klines import make_kline_row
from trading_agent.config.loader import load_config
from trading_agent.data.models import Candle
from trading_agent.data.storage import CandleStore
from trading_agent.metrics.diagnostics import OpenPositionInfo
from trading_agent.metrics.performance import (
    EXIT_REASON_STOP_LOSS,
    EXIT_REASON_STRATEGY,
    EquityPoint,
    Trade,
)
from trading_agent.shadow.boundary import SHADOW_START_BOUNDARY_MS
from trading_agent.shadow.report import (
    SHADOW_MIN_CLOSED_TRADES_FOR_PROMOTION_REVIEW,
    build_shadow_report,
)
from trading_agent.shadow.store import ShadowStore, ShadowTradeRecord

INTERVAL = "1h"
STEP_MS = 3_600_000
START = SHADOW_START_BOUNDARY_MS


def _shadow_config(tmp_path):
    config = {
        "mode": "shadow",
        "market": {"symbol": "BTCUSDT", "interval": INTERVAL},
        "risk": {"max_risk_per_trade_pct": 0.01},
        "backtest": {"starting_equity": 50.0},
        "paths": {
            "data_dir": str(tmp_path),
            "logs_dir": str(tmp_path),
            "db_path": str(tmp_path / "shadow_agent.db"),
        },
    }
    path = tmp_path / "shadow.yaml"
    path.write_text(yaml.safe_dump(config))
    return load_config(path)


def _win_trade(entry_ms: int, exit_ms: int) -> Trade:
    return Trade(
        entry_time_ms=entry_ms, exit_time_ms=exit_ms, entry_price=Decimal(100), exit_price=Decimal(110),
        quantity=Decimal(1), fees_paid=Decimal("0.2"), pnl_quote=Decimal(2), exit_reason=EXIT_REASON_STRATEGY,
        entry_fee_quote=Decimal("0.1"), exit_fee_quote=Decimal("0.1"),
        entry_reference_price=Decimal("99.9"), exit_reference_price=Decimal("110.1"),
    )


def _loss_trade(entry_ms: int, exit_ms: int) -> Trade:
    return Trade(
        entry_time_ms=entry_ms, exit_time_ms=exit_ms, entry_price=Decimal(100), exit_price=Decimal(95),
        quantity=Decimal(1), fees_paid=Decimal("0.2"), pnl_quote=Decimal(-1), exit_reason=EXIT_REASON_STOP_LOSS,
        entry_fee_quote=Decimal("0.1"), exit_fee_quote=Decimal("0.1"),
        entry_reference_price=Decimal("99.9"), exit_reference_price=Decimal("95.1"),
    )


def test_report_on_empty_store_is_all_zero_and_not_eligible(tmp_path):
    config = _shadow_config(tmp_path)
    report = build_shadow_report(config)
    assert report.performance.trade_count == 0
    assert report.expectancy_r is None
    assert report.expectancy_quote is None
    assert report.open_position is None
    assert report.promotion_review_eligible is False
    assert str(SHADOW_MIN_CLOSED_TRADES_FOR_PROMOTION_REVIEW) in report.promotion_review_note
    assert "SHADOW SIMULATION" in report.not_profitable_note


def test_report_computes_win_rate_expectancy_and_losing_streak(tmp_path):
    config = _shadow_config(tmp_path)
    with ShadowStore(config.paths.db_path) as store:
        records = [
            ShadowTradeRecord(_win_trade(1_000, 2_000), planned_risk_quote=Decimal(1), net_reward_to_risk=2.0),
            ShadowTradeRecord(_loss_trade(2_000, 3_000), planned_risk_quote=Decimal(1), net_reward_to_risk=2.0),
            ShadowTradeRecord(_loss_trade(3_000, 4_000), planned_risk_quote=Decimal(1), net_reward_to_risk=2.0),
            ShadowTradeRecord(_win_trade(4_000, 5_000), planned_risk_quote=Decimal(1), net_reward_to_risk=2.0),
        ]
        equity = [EquityPoint(timestamp_ms=t, equity=Decimal(50), in_position=False) for t in (1000, 2000, 3000, 4000, 5000)]
        store.record_cycle_atomically(records, equity, [], 5_000, None, now_ms=1, segment_length=100, status="OK", detail="")

    report = build_shadow_report(config)
    assert report.performance.trade_count == 4
    assert report.performance.win_rate == 50.0
    # r-multiples: +2, -1, -1, +2 -> expectancy_r = 0.5
    assert abs(report.expectancy_r - 0.5) < 1e-9
    # dollar pnl: +2, -1, -1, +2 -> expectancy_quote = 0.5
    assert abs(report.expectancy_quote - 0.5) < 1e-9
    assert report.longest_losing_streak == 2
    assert report.total_fees_paid_quote == Decimal("0.8")
    assert not report.promotion_review_eligible


def test_report_promotion_gate_flips_at_30_closed_trades(tmp_path):
    config = _shadow_config(tmp_path)
    with ShadowStore(config.paths.db_path) as store:
        records = [
            ShadowTradeRecord(_win_trade(i * 1000, i * 1000 + 500), planned_risk_quote=Decimal(1), net_reward_to_risk=2.0)
            for i in range(1, 31)
        ]
        equity = [EquityPoint(timestamp_ms=i * 1000, equity=Decimal(50), in_position=False) for i in range(1, 32)]
        store.record_cycle_atomically(
            records, equity, [], 31_000, None, now_ms=1, segment_length=1000, status="OK", detail=""
        )

    report = build_shadow_report(config)
    assert report.performance.trade_count == 30
    assert report.promotion_review_eligible is True
    assert "eligible" in report.promotion_review_note.lower()
    assert "not yet" not in report.promotion_review_note.lower()


def test_report_open_position_unrealized_pnl_uses_latest_stored_candle_close(tmp_path):
    config = _shadow_config(tmp_path)
    candle = Candle(
        symbol="BTCUSDT", interval=INTERVAL, open_time_ms=START, close_time_ms=START + STEP_MS - 1,
        open=Decimal(100), high=Decimal(106), low=Decimal(99), close=Decimal(105), volume=Decimal(1),
    )
    with CandleStore(config.paths.db_path) as candle_store:
        candle_store.upsert_candles([candle])

    open_position = OpenPositionInfo(
        entry_time_ms=START, entry_price=Decimal(100), entry_reference_price=Decimal("99.9"),
        quantity=Decimal(2), entry_fee_quote=Decimal("0.1"),
    )
    with ShadowStore(config.paths.db_path) as store:
        store.record_cycle_atomically([], [], [], START, open_position, now_ms=1, segment_length=1, status="OK", detail="")

    report = build_shadow_report(config)
    assert report.open_position is not None
    # (105 - 100) * 2 - 0.1 = 9.9
    assert report.open_position.unrealized_pnl_quote == Decimal("9.9")
    assert report.open_position.latest_close_price == Decimal(105)


def test_report_data_gaps_reflects_confirmed_gap_between_two_stored_segments(tmp_path):
    config = _shadow_config(tmp_path)
    first_segment = [
        Candle.from_binance_kline("BTCUSDT", INTERVAL, make_kline_row(START + i * STEP_MS, INTERVAL))
        for i in range(3)
    ]
    # Skip one interval to create a confirmed gap before the second segment.
    gap_start = START + 3 * STEP_MS + STEP_MS
    second_segment = [
        Candle.from_binance_kline("BTCUSDT", INTERVAL, make_kline_row(gap_start + i * STEP_MS, INTERVAL))
        for i in range(2)
    ]
    with CandleStore(config.paths.db_path) as candle_store:
        candle_store.upsert_candles(first_segment + second_segment)

    report = build_shadow_report(config)
    assert report.data_gaps.stored_candle_count == 5
    assert report.data_gaps.gap_count == 1
    assert report.data_gaps.total_missing_intervals == 1
    assert report.data_gaps.latest_segment_length == 2
