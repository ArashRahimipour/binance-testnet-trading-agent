"""Exchange filter parsing and Decimal-exact rounding.

Every rounding operation here rounds DOWN (toward zero risk / toward the
exchange's stricter bound). Nothing in this module ever rounds a quantity
or price up to satisfy a minimum - a size that fails a minimum after
rounding down is rejected by the caller (sizing/order validator), never
silently enlarged.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal


@dataclass(frozen=True, slots=True)
class SymbolFilters:
    symbol: str
    tick_size: Decimal
    min_price: Decimal
    max_price: Decimal
    step_size: Decimal
    min_qty: Decimal
    max_qty: Decimal
    min_notional: Decimal
    max_notional: Decimal | None
    apply_min_notional_to_market: bool

    @staticmethod
    def from_exchange_info(exchange_info: dict) -> SymbolFilters:
        """Parse the `filters` array of one symbol from /api/v3/exchangeInfo."""
        symbols = exchange_info.get("symbols", [])
        if not symbols:
            raise ValueError("exchangeInfo response contains no symbols")
        symbol_info = symbols[0]
        filters_by_type = {f["filterType"]: f for f in symbol_info["filters"]}

        price_filter = filters_by_type.get("PRICE_FILTER", {})
        lot_size = filters_by_type.get("LOT_SIZE", {})

        notional_filter = filters_by_type.get("NOTIONAL") or filters_by_type.get("MIN_NOTIONAL") or {}
        min_notional = Decimal(str(notional_filter.get("minNotional", "0")))
        max_notional_raw = notional_filter.get("maxNotional")
        apply_min_to_market = bool(
            notional_filter.get("applyMinToMarket", notional_filter.get("applyToMarket", True))
        )

        return SymbolFilters(
            symbol=symbol_info["symbol"],
            tick_size=Decimal(str(price_filter.get("tickSize", "0"))),
            min_price=Decimal(str(price_filter.get("minPrice", "0"))),
            max_price=Decimal(str(price_filter.get("maxPrice", "0"))),
            step_size=Decimal(str(lot_size.get("stepSize", "0"))),
            min_qty=Decimal(str(lot_size.get("minQty", "0"))),
            max_qty=Decimal(str(lot_size.get("maxQty", "0"))),
            min_notional=min_notional,
            max_notional=Decimal(str(max_notional_raw)) if max_notional_raw is not None else None,
            apply_min_notional_to_market=apply_min_to_market,
        )


def round_down_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Round `value` down to the nearest multiple of `step` (step=0 disables rounding)."""
    if step == 0:
        return value
    steps = (value / step).to_integral_value(rounding=ROUND_DOWN)
    result = steps * step
    return result.quantize(step, rounding=ROUND_DOWN)


def round_price(price: Decimal, filters: SymbolFilters) -> Decimal:
    return round_down_to_step(price, filters.tick_size)


def round_quantity(quantity: Decimal, filters: SymbolFilters) -> Decimal:
    return round_down_to_step(quantity, filters.step_size)


def meets_min_notional(price: Decimal, quantity: Decimal, filters: SymbolFilters) -> bool:
    if not filters.apply_min_notional_to_market:
        return True
    return (price * quantity) >= filters.min_notional


def meets_lot_size(quantity: Decimal, filters: SymbolFilters) -> bool:
    if quantity < filters.min_qty:
        return False
    return not (filters.max_qty and quantity > filters.max_qty)
