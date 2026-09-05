"""Proofs for backtest/engine.py::run_segment's fixed 1:2 risk/reward
policy integration (`use_fixed_risk_reward_policy=True`): stop/target
resolution, the conservative same-candle stop-first tie-break, gap-through-
stop loss tracking, take-profit execution, and - the corrected behavior -
protection beginning IMMEDIATELY on the SAME candle an entry fills on
(never delayed to the next candle). The policy itself (sizing, gating
math) is proven directly in test_backtest_risk_reward.py; this file
proves the ENGINE wires it in correctly.

Why same-candle protection is correct, not "same-close execution": a
pending BUY signal is decided from the PREVIOUS candle's CLOSE and fills
at THIS candle's OPEN (the project's existing next-open-fill rule,
unchanged). This candle's own high/low describe price action that occurs
AFTER that open - a real market can genuinely hit a protective stop (or a
take-profit) within the very candle a position was just opened in, so the
engine evaluates that candle's OHLC against the freshly-established
stop/target immediately after the fill. The strategy itself is never
consulted about this candle's close before the entry - only the engine's
own post-fill protective-exit check reads this candle's high/low, and
only after `apply_buy` has already happened.
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


# --- Not a universal rejection: a normal trade with realistic nonzero
# fees and slippage is approved and executes end-to-end through the engine.


def test_normal_trade_with_realistic_costs_is_approved_and_executes():
    config = _config(fees={"taker_fee_pct": 0.001, "slippage_pct": 0.0005})
    candles = [
        _candle(0, open_=100, high=100, low=100, close=100),
        _candle(1, open_=100, high=100, low=100, close=100),  # entry candle - inert
        _candle(2, open_=100, high=200, low=99, close=105),  # comfortably clears any realistic target
    ]
    result = _run(candles, buy_at_index=0, config=config)
    assert result.risk_reward is not None
    assert result.risk_reward.entries_approved == 1
    assert result.risk_reward.entries_rejected_net_rr_below_minimum == 0
    assert result.risk_reward.entries_rejected_post_fill_revalidation == 0
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_reason == EXIT_REASON_TAKE_PROFIT
    # Gross R/R exceeds 2.0 (costs are nonzero) but net stays at the fixed floor.
    assert result.risk_reward.gross_reward_to_risk_values[0] > 2.0
    assert result.risk_reward.net_reward_to_risk_values[0] >= 2.0 - 1e-6


# --- Protection begins IMMEDIATELY on the entry candle itself. ---


def test_stop_is_hit_later_in_the_same_candle_the_entry_filled_on():
    # Entry signal decided at candle 0's close -> fills at candle 1's open
    # (100). Stop=95, target=110. Candle 1's low (90) touches the stop
    # AFTER its own open - this is the SAME candle the position was
    # opened in, and the stop must still fire within it.
    candles = [
        _candle(0, open_=100, high=100, low=100, close=100),
        _candle(1, open_=100, high=101, low=90, close=98),
    ]
    result = _run(candles, buy_at_index=0)
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_reason == EXIT_REASON_STOP_LOSS
    assert trade.exit_price == Decimal(95)  # no gap (open 100 > stop 95): fills exactly at the stop
    # Entry and exit both happened within candle 1 - not delayed to a later candle.
    assert trade.entry_time_ms == candles[1].open_time_ms
    assert trade.exit_time_ms == candles[1].open_time_ms


def test_target_is_hit_later_in_the_same_candle_the_entry_filled_on():
    candles = [
        _candle(0, open_=100, high=100, low=100, close=100),
        _candle(1, open_=100, high=112, low=99, close=105),
    ]
    result = _run(candles, buy_at_index=0)
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_reason == EXIT_REASON_TAKE_PROFIT
    assert trade.exit_price == Decimal(110)  # zero fees/slippage: fill == target exactly
    assert trade.entry_time_ms == candles[1].open_time_ms
    assert trade.exit_time_ms == candles[1].open_time_ms


def test_both_touched_on_the_entry_candle_itself_resolves_to_stop():
    candles = [
        _candle(0, open_=100, high=100, low=100, close=100),
        _candle(1, open_=100, high=115, low=90, close=100),  # both stop(95) and target(110) touched
    ]
    result = _run(candles, buy_at_index=0)
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_reason == EXIT_REASON_STOP_LOSS
    assert trade.exit_price == Decimal(95)
    assert result.risk_reward is not None
    assert result.risk_reward.stop_loss_exits == 1
    assert result.risk_reward.take_profit_exits == 0


def test_neither_touched_on_the_entry_candle_position_remains_open():
    candles = [
        _candle(0, open_=100, high=100, low=100, close=100),
        _candle(1, open_=100, high=105, low=97, close=102),  # stays strictly between stop(95) and target(110)
    ]
    result = _run(candles, buy_at_index=0)
    assert result.ends_with_open_position is True
    assert len(result.trades) == 0


def test_signal_candles_high_low_cannot_trigger_protection_before_the_next_open_entry():
    # candle 0 is where the BUY signal is DECIDED (from its close) - no
    # position and no stop/target exist yet at that point, so its own
    # wildly extreme high/low must have zero effect, however they compare
    # to what the stop/target will later become.
    candles = [
        _candle(0, open_=100, high=100_000, low=1, close=100),
        _candle(1, open_=100, high=105, low=97, close=102),  # normal fill, no touch
    ]
    result = _run(candles, buy_at_index=0)
    assert len(result.trades) == 0
    assert result.ends_with_open_position is True


def test_stop_and_target_evaluation_never_uses_a_future_candle():
    # A take-profit fires within the entry candle (candle 1); a wildly
    # different FUTURE candle 2 must never be able to change that already-
    # resolved outcome. Compare the full run against a run truncated right
    # after the exit - the resolved trade must be identical in both.
    candles = [
        _candle(0, open_=100, high=100, low=100, close=100),
        _candle(1, open_=100, high=112, low=99, close=105),  # take-profit fires here
        _candle(2, open_=1, high=1_000_000, low=0.01, close=500),  # wild future candle
    ]
    full_result = _run(candles, buy_at_index=0)
    truncated_result = _run(candles[:2], buy_at_index=0)
    assert len(full_result.trades) == 1
    assert len(truncated_result.trades) == 1
    assert full_result.trades[0] == truncated_result.trades[0]


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


# --- Realized economics stay within/meet the planned figures. ---


def test_ordinary_non_gap_stop_loss_stays_within_the_planned_risk():
    config = _config(fees={"taker_fee_pct": 0.001, "slippage_pct": 0.0005})
    candles = [
        _candle(0, open_=100, high=100, low=100, close=100),
        _candle(1, open_=100, high=101, low=90, close=98),  # non-gap stop touch (open 100 > stop 95)
    ]
    result = _run(candles, buy_at_index=0, config=config)
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_reason == EXIT_REASON_STOP_LOSS
    assert result.risk_reward is not None
    planned_risk = result.risk_reward.planned_risk_quote_total
    assert -trade.pnl_quote <= planned_risk


def test_take_profit_net_pnl_is_at_least_twice_the_planned_risk():
    config = _config(fees={"taker_fee_pct": 0.001, "slippage_pct": 0.0005})
    candles = [
        _candle(0, open_=100, high=100, low=100, close=100),
        _candle(1, open_=100, high=112, low=99, close=105),  # non-gap take-profit touch
    ]
    result = _run(candles, buy_at_index=0, config=config)
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_reason == EXIT_REASON_TAKE_PROFIT
    assert result.risk_reward is not None
    planned_risk = result.risk_reward.planned_risk_quote_total
    # Tick rounding only ever rounds the target UP (more reward), so the
    # realized net gain can exceed, but never fall short of, 2x the risk.
    assert trade.pnl_quote >= 2 * planned_risk
