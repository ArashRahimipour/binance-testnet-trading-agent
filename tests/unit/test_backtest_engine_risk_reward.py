"""Proofs for backtest/engine.py::run_segment's fixed 1:2 risk/reward
policy integration (`use_fixed_risk_reward_policy=True`): stop/target
resolution, the conservative same-candle stop-first tie-break, gap-through-
stop loss tracking, take-profit execution, and no same-candle entry/exit
lookahead. The policy itself (sizing, gating math) is proven directly in
test_backtest_risk_reward.py; this file proves the ENGINE wires it in
correctly.
"""

from decimal import Decimal

from tests.fixtures.exchange_info import make_exchange_info
from trading_agent.backtest.engine import run_segment
from trading_agent.config.models import AppConfig
from trading_agent.data.models import Candle, interval_to_ms
from trading_agent.execution.backtest_broker import BacktestBroker
from trading_agent.metrics.performance import EXIT_REASON_STOP_LOSS, EXIT_REASON_TAKE_PROFIT
from trading_agent.risk.engine import RiskEngine
from trading_agent.sizing.exchange_filters import SymbolFilters
from trading_agent.strategy.base import PositionSide, Signal, SignalType

INTERVAL = "4h"
STEP = interval_to_ms(INTERVAL)
START = 1_700_000_000_000
STARTING_EQUITY = Decimal(50)


class _BuyOnceStrategy:
    """A minimal test double: emits exactly one BUY (when flat, at a fixed
    candle index) and HOLD otherwise - never an EXIT signal, so a stop or
    take-profit exit in these tests is unambiguously the ENGINE's own
    doing, not the strategy's."""

    def __init__(self, buy_at_index: int) -> None:
        self.buy_at_index = buy_at_index

    def generate_signal(self, candles: list[Candle], current_position: PositionSide) -> Signal:
        idx = len(candles) - 1
        last = candles[-1]
        if idx == self.buy_at_index and current_position == PositionSide.FLAT:
            return Signal(SignalType.BUY, "TEST_SCRIPTED_BUY", last.close_time_ms, {})
        return Signal(SignalType.HOLD, "TEST_SCRIPTED_HOLD", last.close_time_ms, {})


def _candle(i: int, open_: float, high: float, low: float, close: float) -> Candle:
    return Candle(
        symbol="BTCUSDT", interval=INTERVAL, open_time_ms=START + i * STEP, close_time_ms=START + i * STEP + STEP - 1,
        open=Decimal(str(open_)), high=Decimal(str(high)), low=Decimal(str(low)), close=Decimal(str(close)),
        volume=Decimal(1),
    )


def _config(**overrides) -> AppConfig:
    stop_loss_overrides = overrides.pop("stop_loss", {"stop_distance_pct": 0.05})
    fees_overrides = overrides.pop("fees", {"taker_fee_pct": 0.0, "slippage_pct": 0.0})
    return AppConfig(mode="backtest", stop_loss=stop_loss_overrides, fees=fees_overrides, **overrides)


def _filters() -> SymbolFilters:
    return SymbolFilters.from_exchange_info(make_exchange_info(min_notional="0.01"))


def _run(candles: list[Candle], buy_at_index: int, config: AppConfig | None = None):
    config = config or _config()
    strategy = _BuyOnceStrategy(buy_at_index)
    risk_engine = RiskEngine(config.risk)
    broker = BacktestBroker(config.fees)
    return run_segment(
        candles, config, _filters(), strategy, risk_engine, broker, None, min_required=1,
        starting_equity=STARTING_EQUITY, use_fixed_risk_reward_policy=True,
    )


# --- Take-profit execution + no same-candle entry/exit lookahead. ---


def test_no_same_candle_entry_exit_lookahead_and_take_profit_execution():
    # Entry signal at index 0 -> fills at candle 1's open (100). Stop=95,
    # target=110 (5% stop, 2x gross target). Candle 1 (the FILL candle
    # itself) is deliberately given a low/high that WOULD trigger both the
    # stop and the target if checked - it must be ignored entirely (no
    # same-candle entry/exit lookahead). Candle 2 then genuinely touches
    # the target (high=112) without touching the stop (low=99), and must
    # produce a TAKE_PROFIT exit at exactly the target price.
    candles = [
        _candle(0, open_=100, high=100, low=100, close=100),
        _candle(1, open_=100, high=120, low=90, close=100),  # entry candle - must be skipped for exits
        _candle(2, open_=100, high=112, low=99, close=105),
    ]
    result = _run(candles, buy_at_index=0)

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_reason == EXIT_REASON_TAKE_PROFIT
    assert trade.exit_price == Decimal(110)  # zero fees/slippage: fill == target exactly
    assert trade.entry_price == Decimal(100)
    assert result.risk_reward is not None
    assert result.risk_reward.entries_approved == 1
    assert result.risk_reward.take_profit_exits == 1
    assert result.risk_reward.stop_loss_exits == 0


def test_position_survives_the_entry_candles_own_low_and_high():
    # Same fixture as above, isolated: confirm the position is STILL OPEN
    # after processing candle 1 (the entry candle) alone, proving the
    # stop/target check truly skipped it rather than coincidentally not
    # triggering.
    candles = [
        _candle(0, open_=100, high=100, low=100, close=100),
        _candle(1, open_=100, high=120, low=90, close=100),
    ]
    result = _run(candles, buy_at_index=0)
    assert result.ends_with_open_position is True
    assert len(result.trades) == 0


# --- Stop-first same-candle ambiguity. ---


def test_stop_occurs_first_when_both_stop_and_target_are_touched_in_one_candle():
    candles = [
        _candle(0, open_=100, high=100, low=100, close=100),
        _candle(1, open_=100, high=100, low=100, close=100),  # entry candle - inert
        _candle(2, open_=105, high=115, low=90, close=100),  # both stop(95) and target(110) touched
    ]
    result = _run(candles, buy_at_index=0)
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_reason == EXIT_REASON_STOP_LOSS
    assert trade.exit_price == Decimal(95)  # no gap (open 105 > stop 95): fills exactly at the stop
    assert result.risk_reward is not None
    assert result.risk_reward.stop_loss_exits == 1
    assert result.risk_reward.take_profit_exits == 0


# --- Gap through stop exceeding the planned risk budget. ---


def test_gap_through_stop_can_exceed_the_planned_risk_budget():
    candles = [
        _candle(0, open_=100, high=100, low=100, close=100),
        _candle(1, open_=100, high=100, low=100, close=100),  # entry candle - inert
        _candle(2, open_=50, high=50, low=45, close=48),  # gaps far below the 95 stop
    ]
    result = _run(candles, buy_at_index=0)
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_reason == EXIT_REASON_STOP_LOSS
    assert trade.exit_price == Decimal(50)  # worse of stop(95) and open(50) -> the gapped-down open
    assert result.risk_reward is not None
    assert result.risk_reward.gap_losses_exceeding_planned_risk == 1
    # The realized loss is indeed larger than what was planned.
    planned_risk = result.risk_reward.planned_risk_quote_total
    assert -trade.pnl_quote > planned_risk
