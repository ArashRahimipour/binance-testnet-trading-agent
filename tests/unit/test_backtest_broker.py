from decimal import Decimal

from trading_agent.config.models import FeesConfig
from trading_agent.execution.backtest_broker import BacktestBroker


def _broker(taker_fee_pct=0.001, slippage_pct=0.0005) -> BacktestBroker:
    return BacktestBroker(FeesConfig(taker_fee_pct=taker_fee_pct, slippage_pct=slippage_pct))


def test_buy_fill_price_includes_slippage_against_trader():
    broker = _broker(slippage_pct=0.001)
    fill = broker.simulate_buy(Decimal(1), Decimal(100))
    assert fill.fill_price == Decimal("100.1")  # buys fill higher


def test_sell_fill_price_includes_slippage_against_trader():
    broker = _broker(slippage_pct=0.001)
    fill = broker.simulate_sell(Decimal(1), Decimal(100))
    assert fill.fill_price == Decimal("99.9")  # sells fill lower


def test_fee_is_applied_to_notional_at_fill_price():
    broker = _broker(taker_fee_pct=0.01, slippage_pct=0)
    fill = broker.simulate_buy(Decimal(2), Decimal(100))
    assert fill.fee_quote == Decimal(2) * Decimal(100) * Decimal("0.01")


def test_zero_fees_and_slippage_gives_exact_close_price():
    broker = _broker(taker_fee_pct=0, slippage_pct=0)
    fill = broker.simulate_buy(Decimal(1), Decimal(100))
    assert fill.fill_price == Decimal(100)
    assert fill.fee_quote == Decimal(0)
