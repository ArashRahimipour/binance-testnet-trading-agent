"""Simulated fills for backtesting: taker fee + configurable slippage.

Slippage is modeled as always working against the trader (buys fill higher,
sells fill lower than the candle close), which is the conservative
direction for evaluating a strategy - it never makes the backtest look
better than reality would.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from trading_agent.config.models import FeesConfig


@dataclass(frozen=True, slots=True)
class SimulatedFill:
    fill_price: Decimal
    fee_quote: Decimal


class BacktestBroker:
    def __init__(self, fees: FeesConfig) -> None:
        self._taker_fee_pct = Decimal(str(fees.taker_fee_pct))
        self._slippage_pct = Decimal(str(fees.slippage_pct))

    def simulate_buy(self, quantity: Decimal, candle_close_price: Decimal) -> SimulatedFill:
        fill_price = candle_close_price * (Decimal(1) + self._slippage_pct)
        fee_quote = quantity * fill_price * self._taker_fee_pct
        return SimulatedFill(fill_price=fill_price, fee_quote=fee_quote)

    def simulate_sell(self, quantity: Decimal, candle_close_price: Decimal) -> SimulatedFill:
        fill_price = candle_close_price * (Decimal(1) - self._slippage_pct)
        fee_quote = quantity * fill_price * self._taker_fee_pct
        return SimulatedFill(fill_price=fill_price, fee_quote=fee_quote)
