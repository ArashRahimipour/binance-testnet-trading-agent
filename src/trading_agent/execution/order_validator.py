"""The order validator: the last gate before an order can be sent anywhere.

This deliberately re-derives exchange-filter compliance independently of
the position sizer (Phase 3), which already checked the same constraints
when the trade was sized. Recomputing here - right before submission,
against a freshly fetched `SymbolFilters` - catches the case where filters
changed between sizing and submission, or where a caller reached this point
without going through the sizer at all. Like the sizer, it only ever
rounds quantity DOWN and rejects (never enlarges) a trade that fails a
minimum.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from trading_agent.risk.limits import TradeIntent
from trading_agent.sizing.exchange_filters import (
    SymbolFilters,
    meets_lot_size,
    meets_min_notional,
    round_quantity,
)


@dataclass(frozen=True, slots=True)
class OrderValidationResult:
    approved: bool
    validated_quantity: Decimal | None
    reason_code: str


def validate_order(intent: TradeIntent, filters: SymbolFilters) -> OrderValidationResult:
    if intent.symbol != filters.symbol:
        return OrderValidationResult(False, None, "SYMBOL_MISMATCH")

    quantity = round_quantity(intent.quantity, filters)
    if quantity <= 0:
        return OrderValidationResult(False, None, "ZERO_QUANTITY_AFTER_ROUNDING")
    if not meets_lot_size(quantity, filters):
        return OrderValidationResult(False, None, "BELOW_MIN_LOT_SIZE")
    if not meets_min_notional(intent.price, quantity, filters):
        return OrderValidationResult(False, None, "BELOW_MIN_NOTIONAL")

    return OrderValidationResult(True, quantity, "VALIDATED")
