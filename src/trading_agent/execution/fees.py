"""Commission-asset-aware fee computation from Binance's per-fill data.

Binance's `fills` array (with per-fill `commission`/`commissionAsset`) is
only present in the immediate response to placing an order
(`POST /api/v3/order`) - `GET /api/v3/order` (used for reconciliation)
never returns it, confirmed against the official docs and community
reports for this project. So exact, asset-aware commission accounting is
only possible on the happy path (a direct successful submission); any
reconciliation-sourced order result has no fills data at all and must fall
back to a percentage-of-notional estimate, honestly flagged as such -
never silently guessed and reported as exact.

Commission is bucketed by asset:
  - quote-asset commission -> `commission_quote` (reduces quote proceeds /
    increases quote cost - see `portfolio/state.py::apply_fill_delta`)
  - base-asset commission -> `commission_base` (reduces base quantity
    received on a BUY; unsupported on a SELL - see `apply_fill_delta`)
  - any other asset (e.g. a BNB fee discount) -> `commission_other`,
    recorded per-asset but never converted or allowed to touch the
    quote/base balances this project tracks.
"""

from __future__ import annotations

from decimal import Decimal

from trading_agent.execution.testnet_adapter import Fill


def compute_commission_buckets(
    fills: list[Fill],
    quote_asset: str,
    base_asset: str,
    fallback_notional: Decimal,
    fallback_fee_pct: Decimal,
) -> tuple[Decimal, Decimal, dict[str, Decimal], bool]:
    """Returns (commission_quote, commission_base, commission_other, is_estimated).

    `commission_other` maps asset -> total commission in that asset,
    recorded for the journal only. `is_estimated` is True only when there
    was no fill data at all and a fallback estimate was used - it is
    False whenever real fills were available, regardless of which
    asset(s) the commission was charged in, since quote/base accounting
    remains exact in every one of those cases.
    """
    if not fills:
        return estimate_fee_from_notional(fallback_notional, fallback_fee_pct), Decimal(0), {}, True

    commission_quote = Decimal(0)
    commission_base = Decimal(0)
    commission_other: dict[str, Decimal] = {}

    for fill in fills:
        if fill.commission_asset == quote_asset:
            commission_quote += fill.commission
        elif fill.commission_asset == base_asset:
            commission_base += fill.commission
        else:
            commission_other[fill.commission_asset] = (
                commission_other.get(fill.commission_asset, Decimal(0)) + fill.commission
            )

    return commission_quote, commission_base, commission_other, False


def estimate_fee_from_notional(notional: Decimal, fallback_fee_pct: Decimal) -> Decimal:
    """A percentage-of-notional fallback, used only when no fill data exists."""
    return notional * fallback_fee_pct
