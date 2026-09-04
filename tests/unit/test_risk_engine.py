from decimal import Decimal

import pytest

from trading_agent.config.models import RiskConfig
from trading_agent.risk.engine import RiskEngine
from trading_agent.risk.limits import RiskContext, TradeIntent
from trading_agent.strategy.base import SignalType

DEFAULT_CONFIG = RiskConfig()


def _buy_intent(quantity="0.001", price="50000") -> TradeIntent:
    return TradeIntent(SignalType.BUY, "BTCUSDT", Decimal(quantity), Decimal(price))


def _exit_intent(quantity="0.001", price="50000") -> TradeIntent:
    return TradeIntent(SignalType.EXIT, "BTCUSDT", Decimal(quantity), Decimal(price))


def _context(**overrides) -> RiskContext:
    base = {
        "equity": Decimal(50),
        "quote_balance": Decimal(50),
        "trades_today": 0,
        "cooldown_bars_remaining": 0,
        "daily_realized_pnl_pct": 0.0,
        "current_drawdown_pct": 0.0,
        "data_age_seconds": 0.0,
        "consecutive_api_errors": 0,
        "kill_switch_engaged": False,
        "is_duplicate_order": False,
    }
    base.update(overrides)
    return RiskContext(**base)


def test_trade_intent_rejects_hold_signal():
    with pytest.raises(ValueError):
        TradeIntent(SignalType.HOLD, "BTCUSDT", Decimal(1), Decimal(1))


def test_approves_reasonable_buy():
    engine = RiskEngine(DEFAULT_CONFIG)
    decision = engine.evaluate(_buy_intent(price="1000", quantity="0.001"), _context())
    assert decision.approved is True


def test_kill_switch_blocks_buy_and_exit():
    engine = RiskEngine(DEFAULT_CONFIG)
    ctx = _context(kill_switch_engaged=True)
    assert engine.evaluate(_buy_intent(), ctx).reason_code == "KILL_SWITCH_ENGAGED"
    assert engine.evaluate(_exit_intent(), ctx).reason_code == "KILL_SWITCH_ENGAGED"


def test_consecutive_api_error_shutdown():
    engine = RiskEngine(RiskConfig(max_consecutive_api_errors=3))
    ctx = _context(consecutive_api_errors=3)
    decision = engine.evaluate(_buy_intent(), ctx)
    assert decision.approved is False
    assert decision.reason_code == "CONSECUTIVE_API_ERROR_SHUTDOWN"


def test_stale_data_blocks_buy_and_exit():
    engine = RiskEngine(RiskConfig(stale_data_max_age_seconds=60))
    ctx = _context(data_age_seconds=61)
    assert engine.evaluate(_buy_intent(), ctx).reason_code == "STALE_DATA"
    assert engine.evaluate(_exit_intent(), ctx).reason_code == "STALE_DATA"


def test_duplicate_order_blocked():
    engine = RiskEngine(DEFAULT_CONFIG)
    ctx = _context(is_duplicate_order=True)
    decision = engine.evaluate(_buy_intent(), ctx)
    assert decision.approved is False
    assert decision.reason_code == "DUPLICATE_ORDER_BLOCKED"


def test_max_drawdown_shutdown_blocks_buy_but_not_exit():
    engine = RiskEngine(RiskConfig(max_drawdown_pct=0.15))
    ctx = _context(current_drawdown_pct=0.20)
    assert engine.evaluate(_buy_intent(), ctx).reason_code == "MAX_DRAWDOWN_SHUTDOWN"
    assert engine.evaluate(_exit_intent(), ctx).approved is True


def test_max_daily_loss_shutdown_blocks_buy_but_not_exit():
    engine = RiskEngine(RiskConfig(max_daily_loss_pct=0.05))
    ctx = _context(daily_realized_pnl_pct=-0.06)
    assert engine.evaluate(_buy_intent(), ctx).reason_code == "MAX_DAILY_LOSS_SHUTDOWN"
    assert engine.evaluate(_exit_intent(), ctx).approved is True


def test_max_trades_per_day_blocks_buy_but_not_exit():
    engine = RiskEngine(RiskConfig(max_trades_per_day=4))
    ctx = _context(trades_today=4)
    assert engine.evaluate(_buy_intent(), ctx).reason_code == "MAX_TRADES_PER_DAY_REACHED"
    assert engine.evaluate(_exit_intent(), ctx).approved is True


def test_cooldown_after_loss_blocks_buy_but_not_exit():
    engine = RiskEngine(DEFAULT_CONFIG)
    ctx = _context(cooldown_bars_remaining=2)
    assert engine.evaluate(_buy_intent(), ctx).reason_code == "COOLDOWN_AFTER_LOSS_ACTIVE"
    assert engine.evaluate(_exit_intent(), ctx).approved is True


def test_min_quote_balance_blocks_buy_but_not_exit():
    engine = RiskEngine(RiskConfig(min_quote_balance=5.0))
    ctx = _context(quote_balance=Decimal(3))
    assert engine.evaluate(_buy_intent(), ctx).reason_code == "BELOW_MIN_QUOTE_BALANCE"
    assert engine.evaluate(_exit_intent(), ctx).approved is True


def test_exceeds_max_position_pct():
    engine = RiskEngine(RiskConfig(max_position_pct=0.5, max_risk_per_trade_pct=0.5))
    ctx = _context(equity=Decimal(50))
    # notional = 0.001 * 50000 = 50 -> 100% of equity, exceeds 50% cap
    decision = engine.evaluate(_buy_intent(quantity="0.001", price="50000"), ctx)
    assert decision.approved is False
    assert decision.reason_code == "EXCEEDS_MAX_POSITION_PCT"


def test_max_risk_per_trade_pct_is_not_a_notional_gate_here():
    # max_risk_per_trade_pct is consumed by the backtest's risk-budget sizer
    # (sizing/position_sizer.py::compute_risk_based_buy_quantity), not
    # re-checked here as a notional cap - a tight stop legitimately allows
    # a larger notional for the same risk budget. Only max_position_pct is
    # enforced as a notional ceiling in the risk engine.
    engine = RiskEngine(RiskConfig(max_position_pct=0.9, max_risk_per_trade_pct=0.02))
    ctx = _context(equity=Decimal(50))
    # notional = 0.0004 * 50000 = 20 -> 40% of equity: under the 90% position
    # cap, so this must be approved even though it dwarfs a 2% risk figure.
    decision = engine.evaluate(_buy_intent(quantity="0.0004", price="50000"), ctx)
    assert decision.approved is True


def test_reconciliation_blocked_blocks_both_buy_and_exit():
    # Round 2 finding #2: an untrusted local balance is unsafe for sizing
    # a SELL too, so this is now a universal gate, not a buy-only one.
    engine = RiskEngine(DEFAULT_CONFIG)
    ctx = _context(reconciliation_blocked=True)
    buy_decision = engine.evaluate(_buy_intent(), ctx)
    exit_decision = engine.evaluate(_exit_intent(), ctx)
    assert buy_decision.approved is False
    assert buy_decision.reason_code == "RECONCILIATION_DISCREPANCY_BLOCKS_ALL_ORDERS"
    assert exit_decision.approved is False
    assert exit_decision.reason_code == "RECONCILIATION_DISCREPANCY_BLOCKS_ALL_ORDERS"


def test_exit_always_approved_when_no_universal_gate_triggered():
    engine = RiskEngine(DEFAULT_CONFIG)
    ctx = _context(
        current_drawdown_pct=0.99,
        daily_realized_pnl_pct=-0.99,
        trades_today=999,
        cooldown_bars_remaining=999,
        quote_balance=Decimal(0),
    )
    decision = engine.evaluate(_exit_intent(), ctx)
    assert decision.approved is True
    assert decision.reason_code == "APPROVED_EXIT_ALWAYS_ALLOWED"
