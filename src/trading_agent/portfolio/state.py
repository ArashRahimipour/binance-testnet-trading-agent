"""In-memory portfolio state and the pure functions that transition it.

`apply_buy`/`apply_sell` are pure - they return a new `PortfolioState`
rather than mutating in place, which makes state transitions easy to test
and to replay deterministically in the backtest engine.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

from trading_agent.strategy.base import PositionSide


@dataclass(frozen=True, slots=True)
class PortfolioState:
    quote_balance: Decimal
    base_balance: Decimal
    position_side: PositionSide
    avg_entry_price: Decimal | None
    realized_pnl_quote: Decimal

    def equity(self, current_price: Decimal) -> Decimal:
        return self.quote_balance + self.base_balance * current_price

    @staticmethod
    def initial(starting_quote_balance: Decimal) -> PortfolioState:
        return PortfolioState(
            quote_balance=starting_quote_balance,
            base_balance=Decimal(0),
            position_side=PositionSide.FLAT,
            avg_entry_price=None,
            realized_pnl_quote=Decimal(0),
        )


class InvalidTransitionError(Exception):
    """Raised when a buy/sell would violate the portfolio's own state invariants."""


def apply_buy(state: PortfolioState, quantity: Decimal, price: Decimal, fee_quote: Decimal) -> PortfolioState:
    if state.position_side == PositionSide.LONG:
        raise InvalidTransitionError("cannot buy while already holding a position")
    cost = quantity * price + fee_quote
    if cost > state.quote_balance:
        raise InvalidTransitionError(
            f"buy cost {cost} exceeds available quote balance {state.quote_balance}"
        )
    return replace(
        state,
        quote_balance=state.quote_balance - cost,
        base_balance=state.base_balance + quantity,
        position_side=PositionSide.LONG,
        avg_entry_price=price,
    )


def apply_sell(state: PortfolioState, quantity: Decimal, price: Decimal, fee_quote: Decimal) -> PortfolioState:
    if state.position_side != PositionSide.LONG or state.avg_entry_price is None:
        raise InvalidTransitionError("cannot sell while not holding a position")
    if quantity > state.base_balance:
        raise InvalidTransitionError(
            f"sell quantity {quantity} exceeds held base balance {state.base_balance}"
        )
    proceeds = quantity * price - fee_quote
    realized_pnl = quantity * (price - state.avg_entry_price) - fee_quote
    remaining_base = state.base_balance - quantity
    return replace(
        state,
        quote_balance=state.quote_balance + proceeds,
        base_balance=remaining_base,
        position_side=PositionSide.LONG if remaining_base > 0 else PositionSide.FLAT,
        avg_entry_price=state.avg_entry_price if remaining_base > 0 else None,
        realized_pnl_quote=state.realized_pnl_quote + realized_pnl,
    )
