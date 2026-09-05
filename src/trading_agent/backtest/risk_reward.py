"""Fixed minimum 1:2 planned reward/risk policy - a USER-MANDATED
pre-real-evaluation risk policy, applied identically to every research
candidate's simulated entries, decided and fixed BEFORE any real candidate
evaluation and never tuned from a result.

Account baseline (fixed, not configurable per candidate):
  - starting equity: whatever `config.backtest.starting_equity` is (e.g.
    $50) - this module reads it from the caller, it is not hardcoded here.
  - maximum planned loss per trade: `FIXED_MAX_RISK_PER_TRADE_PCT` (1%) of
    CURRENT equity at decision time - e.g. $0.50 on $50 equity.
  - minimum planned net reward: `MIN_NET_REWARD_TO_RISK` (2.0) times that
    planned risk, AFTER estimated entry fee, exit fee, and slippage - e.g.
    at least $1.00 on a $0.50 planned risk.
  - no leverage (sizing never exceeds available quote balance; fees are
    reserved so a fill can never overspend it - see
    `_affordability_cap_quantity`).
  - one open position maximum (enforced structurally by the engine's
    single-position `PortfolioState`, unchanged by this module).
  - the exchange's minimum notional/lot-size is NEVER satisfied by
    increasing the risk-based quantity - `plan_risk_reward_entry` only ever
    rounds DOWN and REJECTS (`RR_REJECTED_BELOW_MIN_LOT_SIZE` /
    `RR_REJECTED_BELOW_MIN_NOTIONAL`) a trade that fails a filter after
    rounding, exactly like every other sizing function in this project
    (`sizing/position_sizer.py`).

Stop and target are BOTH planned as a fixed price distance from the entry:
the stop at `stop_distance_pct` below entry (`config.stop_loss.
stop_distance_pct`, the SAME field the pre-existing stop-only policy uses -
not a new tunable), and the target at exactly
`GROSS_REWARD_TO_RISK_MULTIPLE` (2.0) times that same distance ABOVE entry.
This makes the GROSS (pre-cost) reward/risk ratio exactly 2.0 by
construction, always. Entry/exit fees and slippage are then applied on
BOTH the loss side and the reward side (mirroring `sizing/
position_sizer.py::compute_risk_based_buy_quantity`'s existing cost-aware
loss calculation, extended symmetrically to the reward side) to get the
NET reward/risk ratio - which costs can and do push below 2.0, especially
for a tight stop distance where fees/slippage are a larger fraction of the
planned move. A net ratio below `MIN_NET_REWARD_TO_RISK` is a real,
expected outcome of this policy, not a bug: the entry is REJECTED
(`RR_REJECTED_NET_REWARD_TO_RISK_BELOW_MINIMUM`), never forced through and
never "fixed" by loosening the 2.0 threshold after seeing a result.

IMPORTANT, DISCLOSED PROPERTY of fixing the gross ratio at EXACTLY
`MIN_NET_REWARD_TO_RISK` (both are 2.0): algebraically, whenever the
taker fee or slippage rate is strictly positive, the net ratio is
STRICTLY LESS than the gross ratio for every possible `stop_distance_pct`
in (0, 1) - net can approach 2.0 arbitrarily closely as the stop distance
widens, but can never reach or exceed it. This means that under this
policy, given ANY nonzero fee/slippage, `stop_distance_pct` alone can
never be widened or narrowed to make an entry pass - EVERY entry is
rejected for the SAME structural reason under the project's current
non-zero fee/slippage defaults. This is the direct, intended consequence
of a maximally conservative, user-mandated pre-real-evaluation policy that
never forces a trade and never loosens its own bar to manufacture one -
"Do not force trades" - not evidence of a bug in this module. If a future,
explicitly-authorized policy revision wants entries to be approvable under
realistic costs, the fix is to raise `GROSS_REWARD_TO_RISK_MULTIPLE` above
`MIN_NET_REWARD_TO_RISK` (never to lower `MIN_NET_REWARD_TO_RISK` itself,
and never based on having observed real evaluation results).

The plan is computed twice per approved entry:
  1. `plan_risk_reward_entry` - BEFORE the fill, from the reference price
     (the next candle's open, per this project's existing next-open-fill
     rule), used only to decide quantity and whether to approve the entry
     at all.
  2. `build_realized_plan` - AFTER the fill, from the REAL simulated fill
     price (not the reference price, and not the earlier signal's close) -
     this is the plan that is actually PERSISTED with the simulated
     position (`backtest/engine.py::_OpenTrade`) and used to check the
     stop/take-profit on every subsequent candle.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

from trading_agent.sizing.exchange_filters import (
    SymbolFilters,
    meets_lot_size,
    meets_min_notional,
    round_quantity,
)

#: Fixed, declared BEFORE any real candidate evaluation - see module
#: docstring. Never read from `AppConfig`: this is a policy mandated for
#: this pre-real-evaluation correction, deliberately independent of (and
#: not to be confused with) `config.risk.max_risk_per_trade_pct`, which
#: governs the unrelated, unmodified frozen-baseline stop-only path.
FIXED_MAX_RISK_PER_TRADE_PCT = 0.01

#: The gross (pre-cost) target distance is always exactly this multiple of
#: the stop distance - see module docstring.
GROSS_REWARD_TO_RISK_MULTIPLE = 2.0

#: The NET (cost-adjusted) reward/risk ratio an entry must clear to be
#: approved. Identical value to `GROSS_REWARD_TO_RISK_MULTIPLE` by design -
#: a tight stop whose costs erode the gross 2.0 below this is rejected.
MIN_NET_REWARD_TO_RISK = 2.0

RR_APPROVED = "RR_APPROVED"
RR_REJECTED_INVALID_PRICE = "RR_REJECTED_INVALID_PRICE"
RR_REJECTED_INVALID_EQUITY = "RR_REJECTED_INVALID_EQUITY"
RR_REJECTED_INSUFFICIENT_QUOTE_BALANCE = "RR_REJECTED_INSUFFICIENT_QUOTE_BALANCE"
RR_REJECTED_INVALID_STOP_DISTANCE = "RR_REJECTED_INVALID_STOP_DISTANCE"
RR_REJECTED_BELOW_MIN_LOT_SIZE = "RR_REJECTED_BELOW_MIN_LOT_SIZE"
RR_REJECTED_BELOW_MIN_NOTIONAL = "RR_REJECTED_BELOW_MIN_NOTIONAL"
RR_REJECTED_NET_REWARD_TO_RISK_BELOW_MINIMUM = "RR_REJECTED_NET_REWARD_TO_RISK_BELOW_MINIMUM"

#: Reason codes meaning "this trade could only have been made possible by
#: risking more than the fixed budget" - i.e. the exchange's own minimum
#: notional/lot-size, not this policy's 2R gate, is what blocked it. Kept
#: as an explicit set so callers/reports can separate "rejected for R/R"
#: from "rejected because risk-safe sizing cannot meet exchange filters"
#: per the reporting requirement.
EXCHANGE_FILTER_REJECTION_REASONS = frozenset({RR_REJECTED_BELOW_MIN_LOT_SIZE, RR_REJECTED_BELOW_MIN_NOTIONAL})


@dataclass(frozen=True, slots=True)
class RiskRewardPlan:
    approved: bool
    reason_code: str
    quantity: Decimal | None
    #: Planned stop/target PRICE LEVELS. Present (non-None) whenever they
    #: could be computed at all, even for a plan that is ultimately
    #: rejected - useful for reporting exactly what was ruled out and why.
    stop_price: Decimal | None
    target_price: Decimal | None
    planned_risk_quote: Decimal | None
    planned_risk_pct: float | None
    planned_reward_quote: Decimal | None
    planned_reward_pct: float | None
    #: Always exactly `GROSS_REWARD_TO_RISK_MULTIPLE` when defined - the
    #: reward/risk ratio BEFORE fees and slippage.
    gross_reward_to_risk: float | None
    #: The reward/risk ratio AFTER fees and slippage - the figure actually
    #: checked against `MIN_NET_REWARD_TO_RISK`.
    net_reward_to_risk: float | None


def _loss_and_gain_per_unit(
    entry_price: Decimal, stop_price: Decimal, target_price: Decimal, taker_fee_pct: float, slippage_pct: float
) -> tuple[Decimal, Decimal]:
    """Cost-aware planned loss/gain per unit of quantity, mirroring
    `sizing/position_sizer.py::compute_risk_based_buy_quantity`'s existing
    loss-side calculation and extending it symmetrically to the reward
    side (fees and adverse slippage REDUCE a gain exactly as they INCREASE
    a loss)."""
    slippage = Decimal(str(slippage_pct))
    fee = Decimal(str(taker_fee_pct))
    effective_entry = entry_price * (1 + slippage)
    effective_stop_exit = stop_price * (1 - slippage)
    effective_target_exit = target_price * (1 - slippage)
    loss_per_unit = (effective_entry - effective_stop_exit) + effective_entry * fee + effective_stop_exit * fee
    gain_per_unit = (effective_target_exit - effective_entry) - effective_entry * fee - effective_target_exit * fee
    return loss_per_unit, gain_per_unit


def _rejected(
    reason_code: str,
    stop_price: Decimal | None = None,
    target_price: Decimal | None = None,
    planned_risk_quote: Decimal | None = None,
    planned_risk_pct: float | None = None,
    planned_reward_quote: Decimal | None = None,
    planned_reward_pct: float | None = None,
    gross_reward_to_risk: float | None = None,
    net_reward_to_risk: float | None = None,
) -> RiskRewardPlan:
    return RiskRewardPlan(
        approved=False, reason_code=reason_code, quantity=None,
        stop_price=stop_price, target_price=target_price,
        planned_risk_quote=planned_risk_quote, planned_risk_pct=planned_risk_pct,
        planned_reward_quote=planned_reward_quote, planned_reward_pct=planned_reward_pct,
        gross_reward_to_risk=gross_reward_to_risk, net_reward_to_risk=net_reward_to_risk,
    )


def plan_risk_reward_entry(
    equity: Decimal,
    quote_balance: Decimal,
    reference_price: Decimal,
    stop_distance_pct: float,
    taker_fee_pct: float,
    slippage_pct: float,
    filters: SymbolFilters,
) -> RiskRewardPlan:
    """Decide whether a BUY may proceed under the fixed 1:2 policy, and if
    so, how much to buy. `reference_price` is the PRE-fill reference (the
    next candle's open) - this plan is provisional and MUST be replaced by
    `build_realized_plan` once the real fill price is known; only
    `quantity` and the approve/reject decision made here are final."""
    if reference_price <= 0:
        return _rejected(RR_REJECTED_INVALID_PRICE)
    if equity <= 0:
        return _rejected(RR_REJECTED_INVALID_EQUITY)
    if quote_balance <= 0:
        return _rejected(RR_REJECTED_INSUFFICIENT_QUOTE_BALANCE)
    if not (0 < stop_distance_pct < 1):
        return _rejected(RR_REJECTED_INVALID_STOP_DISTANCE)

    stop_price = reference_price * (1 - Decimal(str(stop_distance_pct)))
    target_price = reference_price * (1 + Decimal(str(stop_distance_pct)) * Decimal(str(GROSS_REWARD_TO_RISK_MULTIPLE)))

    loss_per_unit, gain_per_unit = _loss_and_gain_per_unit(
        reference_price, stop_price, target_price, taker_fee_pct, slippage_pct
    )
    if loss_per_unit <= 0:
        return _rejected(RR_REJECTED_INVALID_STOP_DISTANCE, stop_price, target_price)

    risk_budget = equity * Decimal(str(FIXED_MAX_RISK_PER_TRADE_PCT))
    risk_based_quantity = risk_budget / loss_per_unit

    # Fees must be RESERVED, never causing overspending: the affordability
    # cap divides available quote balance by (effective, slippage-adjusted
    # entry price) inflated by the entry fee rate, so quantity * that
    # denominator never exceeds `quote_balance` - no leverage, ever.
    slippage = Decimal(str(slippage_pct))
    fee = Decimal(str(taker_fee_pct))
    effective_entry = reference_price * (1 + slippage)
    affordability_cap_quantity = quote_balance / (effective_entry * (1 + fee))

    raw_quantity = min(risk_based_quantity, affordability_cap_quantity)
    quantity = round_quantity(raw_quantity, filters)

    # Never increase risk to satisfy Binance's minimum notional/lot-size -
    # round DOWN only, and REJECT rather than bump up. This is a "risk
    # cannot meet exchange filters" NO TRADE, reported separately from a
    # below-2R rejection (see `EXCHANGE_FILTER_REJECTION_REASONS`).
    if quantity <= 0 or not meets_lot_size(quantity, filters):
        return _rejected(RR_REJECTED_BELOW_MIN_LOT_SIZE, stop_price, target_price)
    if not meets_min_notional(reference_price, quantity, filters):
        return _rejected(RR_REJECTED_BELOW_MIN_NOTIONAL, stop_price, target_price)

    planned_risk_quote = quantity * loss_per_unit
    planned_reward_quote = quantity * gain_per_unit
    planned_risk_pct = float(planned_risk_quote / equity)
    planned_reward_pct = float(planned_reward_quote / equity)
    gross_reward_to_risk = float((target_price - reference_price) / (reference_price - stop_price))
    net_reward_to_risk = float(planned_reward_quote / planned_risk_quote) if planned_risk_quote > 0 else None

    if net_reward_to_risk is None or net_reward_to_risk < MIN_NET_REWARD_TO_RISK:
        return _rejected(
            RR_REJECTED_NET_REWARD_TO_RISK_BELOW_MINIMUM, stop_price, target_price,
            planned_risk_quote, planned_risk_pct, planned_reward_quote, planned_reward_pct,
            gross_reward_to_risk, net_reward_to_risk,
        )

    return RiskRewardPlan(
        approved=True, reason_code=RR_APPROVED, quantity=quantity,
        stop_price=stop_price, target_price=target_price,
        planned_risk_quote=planned_risk_quote, planned_risk_pct=planned_risk_pct,
        planned_reward_quote=planned_reward_quote, planned_reward_pct=planned_reward_pct,
        gross_reward_to_risk=gross_reward_to_risk, net_reward_to_risk=net_reward_to_risk,
    )


def build_realized_plan(
    plan: RiskRewardPlan,
    fill_price: Decimal,
    stop_distance_pct: float,
    taker_fee_pct: float,
    slippage_pct: float,
    equity: Decimal,
) -> RiskRewardPlan:
    """Recompute the FINAL stop/target price levels (and every dollar/
    percentage figure that follows from them) from the REAL simulated
    entry fill price - never from the earlier reference price or the
    signal's close. `plan` must be an APPROVED plan (quantity already
    decided); only the price anchor changes here, never the quantity or
    the approve/reject decision. `equity` is the SAME pre-fill equity
    `plan_risk_reward_entry` was given (percentages are always relative to
    equity at decision time, not equity after the fill). This is the plan
    that gets persisted with the open position and checked on every
    subsequent candle."""
    if not plan.approved or plan.quantity is None:
        raise ValueError("build_realized_plan requires an approved plan with a decided quantity")

    stop_price = fill_price * (1 - Decimal(str(stop_distance_pct)))
    target_price = fill_price * (1 + Decimal(str(stop_distance_pct)) * Decimal(str(GROSS_REWARD_TO_RISK_MULTIPLE)))
    loss_per_unit, gain_per_unit = _loss_and_gain_per_unit(fill_price, stop_price, target_price, taker_fee_pct, slippage_pct)

    planned_risk_quote = plan.quantity * loss_per_unit
    planned_reward_quote = plan.quantity * gain_per_unit
    planned_risk_pct = float(planned_risk_quote / equity) if equity > 0 else None
    planned_reward_pct = float(planned_reward_quote / equity) if equity > 0 else None
    gross_reward_to_risk = float((target_price - fill_price) / (fill_price - stop_price))
    net_reward_to_risk = float(planned_reward_quote / planned_risk_quote) if planned_risk_quote > 0 else None

    return replace(
        plan,
        stop_price=stop_price,
        target_price=target_price,
        planned_risk_quote=planned_risk_quote,
        planned_risk_pct=planned_risk_pct,
        planned_reward_quote=planned_reward_quote,
        planned_reward_pct=planned_reward_pct,
        gross_reward_to_risk=gross_reward_to_risk,
        net_reward_to_risk=net_reward_to_risk,
    )


@dataclass(frozen=True, slots=True)
class RiskRewardDiagnostics:
    """Per-`run_segment`-call (i.e. per candidate/block, when the fixed
    risk/reward policy is enabled) rollup of everything the reporting
    requirement asks for. `planned_risk_quote_total`/`planned_reward_quote_total`
    sum only over APPROVED (executed) entries; the per-entry lists carry
    one value per approved entry for finer-grained reporting."""

    entries_approved: int = 0
    entries_rejected_net_rr_below_minimum: int = 0
    entries_rejected_exchange_filter_within_risk_budget: int = 0
    stop_loss_exits: int = 0
    take_profit_exits: int = 0
    gap_losses_exceeding_planned_risk: int = 0
    planned_risk_quote_total: Decimal = Decimal(0)
    planned_reward_quote_total: Decimal = Decimal(0)
    planned_risk_pct_values: tuple[float, ...] = ()
    planned_reward_pct_values: tuple[float, ...] = ()
    gross_reward_to_risk_values: tuple[float, ...] = ()
    net_reward_to_risk_values: tuple[float, ...] = ()
