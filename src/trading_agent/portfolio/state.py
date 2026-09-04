"""In-memory portfolio state and the pure functions that transition it.

`apply_buy`/`apply_sell` are pure - they return a new `PortfolioState`
rather than mutating in place, which makes state transitions easy to test
and to replay deterministically in the backtest engine.

`open_position_entry_fee` tracks the fee paid to open the current position
so that `apply_sell` can net it out of realized PnL - a full trade's P&L is
`proceeds - entry_fee - exit_fee`, not just `proceeds - exit_fee`. For a
partial close, the entry fee is allocated proportionally to the fraction
of the position being closed. `pnl_is_estimated` is set when either the
entry or exit fee could not be reliably converted to quote-currency terms
(see `execution/fees.py`) - it propagates forward until the position is
fully closed, so a partially-estimated trade is never reported as exact.
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
    open_position_entry_fee: Decimal = Decimal(0)
    pnl_is_estimated: bool = False

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


def apply_buy(
    state: PortfolioState,
    quantity: Decimal,
    price: Decimal,
    fee_quote: Decimal,
    fee_is_estimated: bool = False,
) -> PortfolioState:
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
        open_position_entry_fee=fee_quote,
        pnl_is_estimated=fee_is_estimated,
    )


def apply_partial_fill_increase(
    state: PortfolioState,
    quantity: Decimal,
    price: Decimal,
    fee_quote: Decimal,
    fee_is_estimated: bool = False,
) -> PortfolioState:
    """Add newly-confirmed execution to an ALREADY-OPEN position.

    Used only when a single order fills in more than one chunk across
    separate reconciliation passes (execution/order_outcome.py) - it
    intentionally bypasses `apply_buy`'s "cannot buy while already long"
    guard, which exists to stop a *separate* erroneous buy signal, not to
    prevent one order's own fills from accumulating into the position that
    order itself opened.
    """
    if state.position_side != PositionSide.LONG or state.avg_entry_price is None:
        raise InvalidTransitionError("cannot increase a position that is not open")
    cost = quantity * price + fee_quote
    if cost > state.quote_balance:
        raise InvalidTransitionError(
            f"partial-fill cost {cost} exceeds available quote balance {state.quote_balance}"
        )
    total_qty = state.base_balance + quantity
    new_avg_entry_price = (state.avg_entry_price * state.base_balance + price * quantity) / total_qty
    return replace(
        state,
        quote_balance=state.quote_balance - cost,
        base_balance=total_qty,
        avg_entry_price=new_avg_entry_price,
        open_position_entry_fee=state.open_position_entry_fee + fee_quote,
        pnl_is_estimated=state.pnl_is_estimated or fee_is_estimated,
    )


def apply_sell(
    state: PortfolioState,
    quantity: Decimal,
    price: Decimal,
    fee_quote: Decimal,
    fee_is_estimated: bool = False,
) -> PortfolioState:
    if state.position_side != PositionSide.LONG or state.avg_entry_price is None:
        raise InvalidTransitionError("cannot sell while not holding a position")
    if quantity > state.base_balance:
        raise InvalidTransitionError(
            f"sell quantity {quantity} exceeds held base balance {state.base_balance}"
        )
    proceeds = quantity * price - fee_quote
    entry_fee_allocated = (
        state.open_position_entry_fee * (quantity / state.base_balance)
        if state.base_balance > 0
        else Decimal(0)
    )
    realized_pnl = quantity * (price - state.avg_entry_price) - fee_quote - entry_fee_allocated
    remaining_base = state.base_balance - quantity
    combined_estimated = state.pnl_is_estimated or fee_is_estimated
    return replace(
        state,
        quote_balance=state.quote_balance + proceeds,
        base_balance=remaining_base,
        position_side=PositionSide.LONG if remaining_base > 0 else PositionSide.FLAT,
        avg_entry_price=state.avg_entry_price if remaining_base > 0 else None,
        realized_pnl_quote=state.realized_pnl_quote + realized_pnl,
        open_position_entry_fee=(
            state.open_position_entry_fee - entry_fee_allocated if remaining_base > 0 else Decimal(0)
        ),
        pnl_is_estimated=combined_estimated if remaining_base > 0 else False,
    )
