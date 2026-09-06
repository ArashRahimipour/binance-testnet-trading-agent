from decimal import Decimal

import pytest

from trading_agent.metrics.diagnostics import OpenPositionInfo
from trading_agent.metrics.performance import (
    EXIT_REASON_STOP_LOSS,
    EXIT_REASON_STRATEGY,
    EquityPoint,
    Trade,
)
from trading_agent.shadow.store import ShadowStore, ShadowTradeRecord, _InjectedTestFault


def _trade(entry_ms: int, exit_ms: int, pnl: str = "1.0") -> Trade:
    return Trade(
        entry_time_ms=entry_ms, exit_time_ms=exit_ms,
        entry_price=Decimal(100), exit_price=Decimal(101), quantity=Decimal(1),
        fees_paid=Decimal("0.1"), pnl_quote=Decimal(pnl), exit_reason=EXIT_REASON_STRATEGY,
        entry_fee_quote=Decimal("0.05"), exit_fee_quote=Decimal("0.05"),
        entry_reference_price=Decimal("99.9"), exit_reference_price=Decimal("101.1"),
    )


def test_empty_run_state_has_sensible_defaults(tmp_path):
    with ShadowStore(tmp_path / "shadow.db") as store:
        state = store.get_run_state()
    assert state.last_processed_close_time_ms is None
    assert state.total_cycles == 0
    assert state.open_position is None
    assert store is not None


def test_record_cycle_atomically_persists_everything_and_advances_mark(tmp_path):
    with ShadowStore(tmp_path / "shadow.db") as store:
        trade = _trade(1_000, 2_000)
        record = ShadowTradeRecord(trade=trade, planned_risk_quote=Decimal("0.5"), net_reward_to_risk=2.1)
        equity_points = [EquityPoint(timestamp_ms=1_000, equity=Decimal(50), in_position=True)]
        journal_entries = [{"entry_type": "SIGNAL", "timestamp_ms": 1_000, "payload": {"type": "buy"}}]
        open_position = OpenPositionInfo(
            entry_time_ms=3_000, entry_price=Decimal(102), entry_reference_price=Decimal("101.9"),
            quantity=Decimal("0.5"), entry_fee_quote=Decimal("0.02"),
        )

        store.record_cycle_atomically(
            [record], equity_points, journal_entries, 2_000, open_position, now_ms=5_000,
            segment_length=10, status="OK", detail="test cycle",
        )

        state = store.get_run_state()
        assert state.last_processed_close_time_ms == 2_000
        assert state.total_cycles == 1
        assert state.last_cycle_status == "OK"
        assert state.open_position is not None
        assert state.open_position.entry_time_ms == 3_000

        trades = store.get_all_trades()
        assert len(trades) == 1
        assert trades[0].trade.exit_time_ms == 2_000
        assert trades[0].planned_risk_quote == Decimal("0.5")
        assert trades[0].net_reward_to_risk == 2.1

        curve = store.get_equity_curve()
        assert curve == equity_points

        entries = store.get_journal_entries()
        assert len(entries) == 1
        assert entries[0]["entry_type"] == "SIGNAL"


def test_record_cycle_atomically_is_idempotent_on_retry(tmp_path):
    with ShadowStore(tmp_path / "shadow.db") as store:
        trade = _trade(1_000, 2_000)
        record = ShadowTradeRecord(trade=trade, planned_risk_quote=Decimal("0.5"), net_reward_to_risk=2.0)
        equity_points = [EquityPoint(timestamp_ms=1_000, equity=Decimal(50), in_position=True)]
        journal_entries = [{"entry_type": "SIGNAL", "timestamp_ms": 1_000, "payload": {"type": "buy"}}]

        for _ in range(3):
            store.record_cycle_atomically(
                [record], equity_points, journal_entries, 2_000, None, now_ms=5_000,
                segment_length=10, status="OK", detail="retry",
            )

        assert len(store.get_all_trades()) == 1
        assert len(store.get_equity_curve()) == 1
        assert len(store.get_journal_entries()) == 1
        assert store.get_run_state().total_cycles == 3


def test_touch_cycle_advances_observability_fields_but_never_last_processed(tmp_path):
    with ShadowStore(tmp_path / "shadow.db") as store:
        trade = _trade(1_000, 2_000)
        record = ShadowTradeRecord(trade=trade, planned_risk_quote=Decimal("0.5"), net_reward_to_risk=2.0)
        store.record_cycle_atomically([record], [], [], 2_000, None, now_ms=5_000, segment_length=10, status="OK", detail="")

        store.touch_cycle(now_ms=9_000, status="NO_NEW_CANDLES", detail="nothing new", segment_length=10)

        state = store.get_run_state()
        assert state.last_processed_close_time_ms == 2_000
        assert state.last_run_at_ms == 9_000
        assert state.total_cycles == 2
        assert state.last_cycle_status == "NO_NEW_CANDLES"


@pytest.mark.parametrize("fault_point", ["after_trades", "after_equity", "after_journal", "before_commit"])
def test_record_cycle_rolls_back_completely_on_any_fault(tmp_path, fault_point):
    with ShadowStore(tmp_path / "shadow.db") as store:
        trade = _trade(1_000, 2_000)
        record = ShadowTradeRecord(trade=trade, planned_risk_quote=Decimal("0.5"), net_reward_to_risk=2.0)
        equity_points = [EquityPoint(timestamp_ms=1_000, equity=Decimal(50), in_position=True)]
        journal_entries = [{"entry_type": "SIGNAL", "timestamp_ms": 1_000, "payload": {}}]

        store._raise_fault_at = fault_point
        with pytest.raises(_InjectedTestFault):
            store.record_cycle_atomically(
                [record], equity_points, journal_entries, 2_000, None, now_ms=5_000,
                segment_length=10, status="OK", detail="",
            )
        store._raise_fault_at = None

        assert store.get_all_trades() == []
        assert store.get_equity_curve() == []
        assert store.get_journal_entries() == []
        state = store.get_run_state()
        assert state.last_processed_close_time_ms is None
        assert state.total_cycles == 0


def test_open_position_can_be_cleared_on_a_later_cycle(tmp_path):
    with ShadowStore(tmp_path / "shadow.db") as store:
        open_position = OpenPositionInfo(
            entry_time_ms=1_000, entry_price=Decimal(100), entry_reference_price=Decimal("99.9"),
            quantity=Decimal(1), entry_fee_quote=Decimal("0.01"),
        )
        store.record_cycle_atomically([], [], [], 1_000, open_position, now_ms=1, segment_length=5, status="OK", detail="")
        assert store.get_run_state().open_position is not None

        closing_trade = _trade(1_000, 2_000)
        record = ShadowTradeRecord(trade=closing_trade, planned_risk_quote=Decimal("0.5"), net_reward_to_risk=2.0)
        store.record_cycle_atomically([record], [], [], 2_000, None, now_ms=2, segment_length=6, status="OK", detail="")
        assert store.get_run_state().open_position is None


def test_stop_loss_exit_reason_round_trips(tmp_path):
    with ShadowStore(tmp_path / "shadow.db") as store:
        trade = Trade(
            entry_time_ms=1_000, exit_time_ms=2_000, entry_price=Decimal(100), exit_price=Decimal(95),
            quantity=Decimal(1), fees_paid=Decimal("0.1"), pnl_quote=Decimal(-5),
            exit_reason=EXIT_REASON_STOP_LOSS, entry_fee_quote=Decimal("0.05"), exit_fee_quote=Decimal("0.05"),
            entry_reference_price=Decimal("99.9"), exit_reference_price=Decimal("95.1"),
        )
        record = ShadowTradeRecord(trade=trade, planned_risk_quote=Decimal(5), net_reward_to_risk=2.0)
        store.record_cycle_atomically([record], [], [], 2_000, None, now_ms=1, segment_length=5, status="OK", detail="")
        stored = store.get_all_trades()[0]
        assert stored.trade.exit_reason == EXIT_REASON_STOP_LOSS
        assert stored.trade.pnl_quote == Decimal(-5)
