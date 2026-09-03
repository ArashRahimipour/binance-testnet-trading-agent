"""Data contracts for the risk engine.

`RiskContext` is deliberately a flat, plain snapshot of everything the risk
engine needs to know - it never reaches into the broker, the portfolio, or
the database itself. That is what makes it "independent" of the strategy
and of the execution layer: it only ever sees numbers it is handed.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from trading_agent.strategy.base import SignalType


@dataclass(frozen=True, slots=True)
class TradeIntent:
    signal_type: SignalType  # BUY or EXIT only - HOLD never reaches the risk engine
    symbol: str
    quantity: Decimal
    price: Decimal

    def __post_init__(self) -> None:
        if self.signal_type not in (SignalType.BUY, SignalType.EXIT):
            raise ValueError(f"TradeIntent.signal_type must be BUY or EXIT, got {self.signal_type}")


@dataclass(frozen=True, slots=True)
class RiskContext:
    equity: Decimal
    quote_balance: Decimal
    trades_today: int
    cooldown_bars_remaining: int
    daily_realized_pnl_pct: float
    current_drawdown_pct: float
    data_age_seconds: float
    consecutive_api_errors: int
    kill_switch_engaged: bool
    is_duplicate_order: bool


@dataclass(frozen=True, slots=True)
class RiskDecision:
    approved: bool
    reason_code: str
