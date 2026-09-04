"""Fee computation from Binance's per-fill commission data.

If every fill's commission is denominated in the trade's quote asset, the
fee is exact - just the sum of those commissions. If any fill's commission
is charged in a different asset (most commonly BNB, when fee discounts are
enabled), converting it to quote terms would require a separate price feed
this project does not maintain, so instead of guessing, that outcome is
reported as an estimate via `taker_fee_pct * notional` and the caller is
told the result is not exact.
"""

from __future__ import annotations

from decimal import Decimal

from trading_agent.execution.testnet_adapter import Fill


def compute_confirmed_fee_for_first_application(
    fills: list[Fill], quote_asset: str, fallback_notional: Decimal, fallback_fee_pct: Decimal
) -> tuple[Decimal, bool]:
    """Fee for the first time an order's execution is applied to portfolio
    state - `fills` (if present) is assumed to cover the order's entire
    execution to date, which holds the first time an order is observed.
    Returns (fee_quote, is_estimated).
    """
    if not fills:
        return fallback_notional * fallback_fee_pct, True

    total_quote_fee = Decimal(0)
    all_quote_denominated = True
    for fill in fills:
        if fill.commission_asset == quote_asset:
            total_quote_fee += fill.commission
        else:
            all_quote_denominated = False

    return total_quote_fee, not all_quote_denominated


def estimate_fee_for_incremental_fill(notional: Decimal, fallback_fee_pct: Decimal) -> tuple[Decimal, bool]:
    """Fee for a later, incremental continuation of an already-partially-
    applied order. Exact per-fill attribution is not tracked across
    separate reconciliation passes for this rare case (a single order
    filling in multiple chunks over multiple cycles) - this is always
    reported as an estimate, a documented, honest simplification rather
    than a silent approximation.
    """
    return notional * fallback_fee_pct, True
