"""Position sizing: fixed-fractional of available quote balance.

Two hard rules enforced here:
  - A computed size that fails the exchange's minimum notional/lot-size
    filters after rounding down is REJECTED, never bumped up to meet the
    minimum (bumping up would silently exceed the configured risk budget).
  - A sell can never be sized above the quantity actually held - rounding
    is always downward, so the returned quantity is always <= the input
    held quantity.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from trading_agent.sizing.exchange_filters import (
    SymbolFilters,
    meets_lot_size,
    meets_min_notional,
    round_quantity,
)


@dataclass(frozen=True, slots=True)
class SizingDecision:
    approved: bool
    quantity: Decimal | None
    reason_code: str


def compute_buy_quantity(
    available_quote_balance: Decimal,
    price: Decimal,
    max_allocation_pct: float,
    min_quote_buffer: Decimal,
    filters: SymbolFilters,
) -> SizingDecision:
    if price <= 0:
        return SizingDecision(False, None, "INVALID_PRICE")

    spendable = available_quote_balance - min_quote_buffer
    if spendable <= 0:
        return SizingDecision(False, None, "BELOW_MIN_QUOTE_BUFFER")

    budget = spendable * Decimal(str(max_allocation_pct))
    raw_quantity = budget / price
    quantity = round_quantity(raw_quantity, filters)

    if not meets_lot_size(quantity, filters):
        return SizingDecision(False, None, "BELOW_MIN_LOT_SIZE")
    if not meets_min_notional(price, quantity, filters):
        return SizingDecision(False, None, "BELOW_MIN_NOTIONAL")

    return SizingDecision(True, quantity, "SIZED_OK")


def compute_risk_based_buy_quantity(
    equity: Decimal,
    entry_price: Decimal,
    stop_price: Decimal,
    max_risk_per_trade_pct: float,
    max_position_pct: float,
    taker_fee_pct: float,
    slippage_pct: float,
    filters: SymbolFilters,
) -> SizingDecision:
    """Size a position from a real, cost-aware stop-loss distance, not a
    notional guess and not the bare price gap between entry and stop.

    Review round 2, Finding 6: the raw `entry_price - stop_price` gap
    understates what an ordinary (non-gap) stop actually costs, because it
    ignores the same slippage and fees every other simulated fill in this
    project pays. quantity = risk_budget / total_loss_per_unit, where:

      - effective_entry = entry_price * (1 + slippage_pct)  (a buy fills
        higher than the reference price - see execution/backtest_broker.py)
      - effective_exit  = stop_price  * (1 - slippage_pct)  (a sell/stop
        fills lower)
      - total_loss_per_unit = (effective_entry - effective_exit)
        + effective_entry * taker_fee_pct + effective_exit * taker_fee_pct

    Sizing against this total means the EXPECTED loss of an ordinary
    (non-gap) stop hit is <= equity * max_risk_per_trade_pct. It does NOT
    bound a gap-through-stop: if the market gaps below the stop price
    before an exit can be filled at or near it (backtest/engine.py's
    `_execute_stop_exit` fills at the worse of the stop price or that
    candle's open, modeling this conservatively rather than hiding it),
    the realized loss can still exceed the risk budget. That is a real,
    disclosed limitation of a fixed-percentage stop - not something any
    position sizing can fully prevent - see RISK_POLICY.md and STRATEGY.md.

    `max_position_pct` is enforced as a SEPARATE notional ceiling on top of
    the risk-based size - a tight stop can legitimately justify a large
    notional for a small risk budget, but the position can still never
    exceed the configured maximum share of equity.

    As with all sizing in this project: rounds DOWN only, and REJECTS
    (never enlarges) a trade that fails an exchange minimum after rounding
    - including when satisfying the minimum would require risking more
    than the budget allows.
    """
    if entry_price <= 0 or stop_price <= 0:
        return SizingDecision(False, None, "INVALID_PRICE")
    if equity <= 0:
        return SizingDecision(False, None, "INVALID_EQUITY")

    slippage = Decimal(str(slippage_pct))
    fee = Decimal(str(taker_fee_pct))
    effective_entry = entry_price * (1 + slippage)
    effective_exit = stop_price * (1 - slippage)
    total_loss_per_unit = (
        (effective_entry - effective_exit) + effective_entry * fee + effective_exit * fee
    )
    if total_loss_per_unit <= 0:
        return SizingDecision(False, None, "INVALID_STOP_DISTANCE")

    risk_budget = equity * Decimal(str(max_risk_per_trade_pct))
    risk_based_quantity = risk_budget / total_loss_per_unit

    max_notional = equity * Decimal(str(max_position_pct))
    position_cap_quantity = max_notional / entry_price

    quantity = round_quantity(min(risk_based_quantity, position_cap_quantity), filters)

    if not meets_lot_size(quantity, filters):
        return SizingDecision(False, None, "BELOW_MIN_LOT_SIZE")
    if not meets_min_notional(entry_price, quantity, filters):
        return SizingDecision(False, None, "BELOW_MIN_NOTIONAL")

    return SizingDecision(True, quantity, "SIZED_OK")


def compute_sell_quantity(held_quantity: Decimal, price: Decimal, filters: SymbolFilters) -> SizingDecision:
    quantity = round_quantity(held_quantity, filters)
    if quantity <= 0 or quantity > held_quantity:
        return SizingDecision(False, None, "NO_SELLABLE_QUANTITY")
    if not meets_lot_size(quantity, filters):
        return SizingDecision(False, None, "BELOW_MIN_LOT_SIZE")
    if not meets_min_notional(price, quantity, filters):
        return SizingDecision(False, None, "BELOW_MIN_NOTIONAL")
    return SizingDecision(True, quantity, "SIZED_OK")
