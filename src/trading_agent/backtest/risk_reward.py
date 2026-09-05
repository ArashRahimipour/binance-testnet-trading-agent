"""Fixed minimum 1:2 planned reward/risk policy - a USER-MANDATED
pre-real-evaluation risk policy, applied identically to every research
candidate's simulated entries, decided and fixed BEFORE any real candidate
evaluation and never tuned from a result.

Account baseline (fixed, not configurable per candidate):
  - starting equity: whatever `config.backtest.starting_equity` is (e.g.
    $50) - this module reads it from the caller, it is not hardcoded here.
  - maximum planned NET loss per trade: `FIXED_MAX_RISK_PER_TRADE_PCT` (1%)
    of CURRENT equity at decision time - e.g. $0.50 on $50 equity - AFTER
    estimated entry fee, stop-exit fee, and adverse slippage.
  - minimum planned NET reward: `MIN_NET_REWARD_TO_RISK` (2.0) times that
    planned NET loss, AFTER estimated entry fee, target-exit fee, and
    adverse slippage - e.g. at least $1.00 on a $0.50 planned risk.
  - no leverage (sizing never exceeds available quote balance; fees are
    reserved so a fill can never overspend it - see the affordability
    cap below).
  - one open position maximum (enforced structurally by the engine's
    single-position `PortfolioState`, unchanged by this module).
  - the exchange's minimum notional/lot-size is NEVER satisfied by
    increasing the risk-based quantity - `plan_risk_reward_entry` only ever
    rounds the QUANTITY down and REJECTS (`RR_REJECTED_BELOW_MIN_LOT_SIZE` /
    `RR_REJECTED_BELOW_MIN_NOTIONAL`) a trade that fails a filter after
    rounding, exactly like every other sizing function in this project
    (`sizing/position_sizer.py`).

HOW THE STOP AND TARGET ARE PLANNED (corrected design - see CHANGELOG for
why the prior "target = 2x stop distance" design was wrong): the stop
remains a fixed price distance below entry (`stop_distance_pct`,
`config.stop_loss.stop_distance_pct` - the SAME field the pre-existing
stop-only policy uses, not a new tunable). The TARGET is not a fixed
multiple of that distance - it is SOLVED ALGEBRAICALLY (`_solve_target_price`)
so that the NET (cost-adjusted, after estimated entry fee, target-exit fee,
and adverse slippage) planned reward is exactly `MIN_NET_REWARD_TO_RISK`
(2.0) times the NET planned risk. This is an exact Decimal computation -
no floating point, no tolerance. Consequently the GROSS (pre-cost)
reward/risk ratio comes out AT OR ABOVE 2.0 - exactly 2.0 when fees and
slippage are both zero, and strictly ABOVE 2.0 whenever either is
positive (the target has to reach slightly further to still deliver a net
2R after round-trip costs eat into it). Both the gross and net ratios are
reported on every entry (`gross_reward_to_risk`, `net_reward_to_risk`).

This means realistic, nonzero fees and slippage do NOT make this policy
reject every entry - a normal trade is approved once its stop distance
produces a large enough per-unit price move that costs remain a small
enough fraction of it. Only a stop distance so tight that this project's
own PRICE_FILTER tick size cannot represent a target close enough to the
algebraic solution (see rounding, below) actually fails on cost grounds.

TICK ROUNDING (`_round_target_up_to_tick`): the exact algebraic target is
rounded to the exchange's price tick size in the direction that PRESERVES
the 2.0 net minimum - i.e. UP for a long position's take-profit level
(a higher target can only ever increase the reward). This is a narrow,
deliberate, and clearly-scoped exception to `sizing/exchange_filters.py`'s
project-wide "round down only" convention: that convention exists to keep
QUANTITY (and hence risk) from ever being enlarged past a budget, which is
the opposite problem from rounding a REWARD target in the direction that
protects a stated minimum guarantee. The net ratio is still recomputed
from the ROUNDED target and re-checked against `MIN_NET_REWARD_TO_RISK`
after rounding (never assumed) - the entry is REJECTED
(`RR_REJECTED_NET_REWARD_TO_RISK_BELOW_MINIMUM`) if, in some edge case
(e.g. the rounded target would exceed the symbol's max price), rounding
cannot preserve it. This is a real, testable code path, not a placebo.

The plan is computed, and separately VALIDATED, twice per entry:
  1. `plan_risk_reward_entry` - BEFORE the fill, from the reference price
     (the next candle's open, per this project's existing next-open-fill
     rule). Decides quantity and whether to approve the entry at all.
  2. `build_realized_plan` - AFTER the simulated fill, from the REAL fill
     price (not the reference price, and not the earlier signal's close).
     Re-solves the target from the fill price and RE-VALIDATES both
     conditions (planned NET loss <= 1% of the ORIGINAL pre-entry equity;
     planned NET reward/risk >= 2.0) from scratch. If EITHER fails, the
     returned plan has `approved=False`
     (`RR_REJECTED_POST_FILL_REVALIDATION_FAILED`) and
     `backtest/engine.py::run_segment` FAILS CLOSED: the position is never
     created at all (the simulated buy fill is computed but never applied
     to the portfolio) rather than left open without a validated plan.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import ROUND_CEILING, Decimal

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

#: The single, fixed minimum NET reward/risk ratio this policy enforces -
#: used both as the multiplier when SOLVING for the target price and as
#: the gate every entry (pre-fill and post-fill) must clear. Never lowered
#: to make a trade pass; never raised past what "the fixed 1:2 policy"
#: means. See module docstring for why the GROSS ratio can be, and often
#: is, above this value while the NET ratio is held at exactly this floor.
MIN_NET_REWARD_TO_RISK = Decimal("2.0")

RR_APPROVED = "RR_APPROVED"
RR_REJECTED_INVALID_PRICE = "RR_REJECTED_INVALID_PRICE"
RR_REJECTED_INVALID_EQUITY = "RR_REJECTED_INVALID_EQUITY"
RR_REJECTED_INSUFFICIENT_QUOTE_BALANCE = "RR_REJECTED_INSUFFICIENT_QUOTE_BALANCE"
RR_REJECTED_INVALID_STOP_DISTANCE = "RR_REJECTED_INVALID_STOP_DISTANCE"
RR_REJECTED_BELOW_MIN_LOT_SIZE = "RR_REJECTED_BELOW_MIN_LOT_SIZE"
RR_REJECTED_BELOW_MIN_NOTIONAL = "RR_REJECTED_BELOW_MIN_NOTIONAL"
RR_REJECTED_INVALID_TARGET = "RR_REJECTED_INVALID_TARGET"
RR_REJECTED_NET_REWARD_TO_RISK_BELOW_MINIMUM = "RR_REJECTED_NET_REWARD_TO_RISK_BELOW_MINIMUM"
RR_REJECTED_POST_FILL_REVALIDATION_FAILED = "RR_REJECTED_POST_FILL_REVALIDATION_FAILED"

#: Reason codes meaning "this trade could only have been made possible by
#: risking more than the fixed budget, or by pricing outside what the
#: exchange's own PRICE_FILTER/LOT_SIZE/NOTIONAL filters allow" - i.e. an
#: exchange constraint, not this policy's 2R gate, is what blocked it.
#: Kept as an explicit set so callers/reports can separate "rejected for
#: R/R" from "rejected because risk-safe sizing cannot meet exchange
#: filters" per the reporting requirement.
EXCHANGE_FILTER_REJECTION_REASONS = frozenset(
    {RR_REJECTED_BELOW_MIN_LOT_SIZE, RR_REJECTED_BELOW_MIN_NOTIONAL, RR_REJECTED_INVALID_TARGET}
)


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
    #: The reward/risk ratio BEFORE fees and slippage - always >= 2.0,
    #: exactly 2.0 only when fees and slippage are both zero.
    gross_reward_to_risk: float | None
    #: The reward/risk ratio AFTER fees and slippage - always >= 2.0 for
    #: an APPROVED plan (this is the figure actually gated on, via exact
    #: Decimal comparison - this float is for reporting only).
    net_reward_to_risk: float | None


def _to_decimal(value: float) -> Decimal:
    return Decimal(str(value))


def _loss_per_unit(effective_entry: Decimal, stop_price: Decimal, fee: Decimal, slippage: Decimal) -> Decimal:
    """NET (cost-inclusive) planned loss per unit of quantity if the stop
    is hit under ordinary (non-gap) conditions - identical formula to
    `sizing/position_sizer.py::compute_risk_based_buy_quantity`'s existing
    loss calculation. `effective_entry` must ALREADY be the slippage-
    adjusted price actually paid (or, pre-fill, the same estimate
    `broker/backtest_broker.py::simulate_buy` will independently compute
    from the same reference price and slippage rate) - callers must never
    apply `(1 + slippage)` to it again here, or slippage is double-counted."""
    effective_stop_exit = stop_price * (1 - slippage)
    return (effective_entry - effective_stop_exit) + effective_entry * fee + effective_stop_exit * fee


def _gain_per_unit(effective_entry: Decimal, target_price: Decimal, fee: Decimal, slippage: Decimal) -> Decimal:
    """NET (cost-inclusive) planned gain per unit of quantity if the
    target is hit - the symmetric mirror of `_loss_per_unit`: fees and
    adverse slippage REDUCE a gain exactly as they INCREASE a loss. See
    `_loss_per_unit` for the `effective_entry` contract."""
    effective_target_exit = target_price * (1 - slippage)
    return (effective_target_exit - effective_entry) - effective_entry * fee - effective_target_exit * fee


def _solve_target_price(effective_entry: Decimal, loss_per_unit: Decimal, fee: Decimal, slippage: Decimal) -> Decimal:
    """Solve, EXACTLY (Decimal, no floating point), for the target price
    T such that `_gain_per_unit(effective_entry, T, fee, slippage) ==
    MIN_NET_REWARD_TO_RISK * loss_per_unit` - i.e. the NET planned reward
    is exactly the fixed minimum multiple of the NET planned risk, before
    any tick-size rounding. Derivation (E = effective_entry):

        gain_per_unit(T) = T*(1-s)*(1-f) - E*(1+f)
        required_reward  = MIN_NET_REWARD_TO_RISK * loss_per_unit
        => T = (required_reward + E*(1+f)) / ((1-s)*(1-f))

    See `_loss_per_unit` for the `effective_entry` contract.
    """
    required_reward = MIN_NET_REWARD_TO_RISK * loss_per_unit
    numerator = required_reward + effective_entry * (1 + fee)
    denominator = (1 - slippage) * (1 - fee)
    return numerator / denominator


def _round_target_up_to_tick(target_price: Decimal, filters: SymbolFilters) -> Decimal | None:
    """Round `target_price` to the exchange's price tick size in the
    direction that can only ever INCREASE the reward (ceiling) - the
    conservative direction for protecting the stated minimum net
    reward/risk guarantee (see module docstring). Returns None if no
    valid tick-aligned price at or above `target_price` fits within the
    symbol's max price - a genuine "exchange filter" NO TRADE, not a
    silent clamp."""
    tick = filters.tick_size
    if tick > 0:
        steps = (target_price / tick).to_integral_value(rounding=ROUND_CEILING)
        rounded = (steps * tick).quantize(tick, rounding=ROUND_CEILING)
    else:
        rounded = target_price
    if filters.max_price > 0 and rounded > filters.max_price:
        return None
    return rounded


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
    """Decide whether a BUY may proceed under the fixed 1:2 NET policy,
    and if so, how much to buy and where the take-profit target must sit.
    `reference_price` is the PRE-fill reference (the next candle's open) -
    this plan is provisional and MUST be replaced by `build_realized_plan`
    once the real fill price is known; only `quantity` and the
    approve/reject decision made here are final."""
    if reference_price <= 0:
        return _rejected(RR_REJECTED_INVALID_PRICE)
    if equity <= 0:
        return _rejected(RR_REJECTED_INVALID_EQUITY)
    if quote_balance <= 0:
        return _rejected(RR_REJECTED_INSUFFICIENT_QUOTE_BALANCE)
    if not (0 < stop_distance_pct < 1):
        return _rejected(RR_REJECTED_INVALID_STOP_DISTANCE)

    fee = _to_decimal(taker_fee_pct)
    slippage = _to_decimal(slippage_pct)
    stop_price = reference_price * (1 - _to_decimal(stop_distance_pct))
    # The PRE-fill estimate of the slippage-adjusted price actually paid -
    # identical formula to `execution/backtest_broker.py::BacktestBroker.
    # simulate_buy`, so this matches the real fill exactly in this
    # project's deterministic fill model. Passed to `_loss_per_unit`/
    # `_gain_per_unit`/`_solve_target_price` as `effective_entry` - never
    # re-multiplied by `(1 + slippage)` again inside them.
    effective_entry = reference_price * (1 + slippage)

    loss_per_unit = _loss_per_unit(effective_entry, stop_price, fee, slippage)
    if loss_per_unit <= 0:
        return _rejected(RR_REJECTED_INVALID_STOP_DISTANCE, stop_price=stop_price)

    # Fees must be RESERVED, never causing overspending: the affordability
    # cap divides available quote balance by (effective, slippage-adjusted
    # entry price) inflated by the entry fee rate, so quantity * that
    # denominator never exceeds `quote_balance` - no leverage, ever.
    risk_budget = equity * _to_decimal(FIXED_MAX_RISK_PER_TRADE_PCT)
    risk_based_quantity = risk_budget / loss_per_unit
    affordability_cap_quantity = quote_balance / (effective_entry * (1 + fee))
    raw_quantity = min(risk_based_quantity, affordability_cap_quantity)
    quantity = round_quantity(raw_quantity, filters)

    # Never increase risk to satisfy Binance's minimum notional/lot-size -
    # round the QUANTITY down only, and REJECT rather than bump up. This
    # is a "risk-safe sizing cannot meet exchange filters" NO TRADE,
    # reported separately from a below-2R rejection.
    if quantity <= 0 or not meets_lot_size(quantity, filters):
        return _rejected(RR_REJECTED_BELOW_MIN_LOT_SIZE, stop_price=stop_price)
    if not meets_min_notional(reference_price, quantity, filters):
        return _rejected(RR_REJECTED_BELOW_MIN_NOTIONAL, stop_price=stop_price)

    raw_target = _solve_target_price(effective_entry, loss_per_unit, fee, slippage)
    target_price = _round_target_up_to_tick(raw_target, filters)
    if target_price is None or target_price <= reference_price:
        return _rejected(RR_REJECTED_INVALID_TARGET, stop_price=stop_price)

    gain_per_unit = _gain_per_unit(effective_entry, target_price, fee, slippage)
    planned_risk_quote = quantity * loss_per_unit
    planned_reward_quote = quantity * gain_per_unit
    gross_reward_to_risk = float((target_price - reference_price) / (reference_price - stop_price))
    net_reward_to_risk = float(planned_reward_quote / planned_risk_quote) if planned_risk_quote > 0 else None
    planned_risk_pct = float(planned_risk_quote / equity)
    planned_reward_pct = float(planned_reward_quote / equity)

    # The gate itself: EXACT Decimal cross-multiplication, never a float
    # division or an epsilon tolerance - tick rounding above is expected
    # to keep this satisfied, but it is always re-checked, never assumed.
    if planned_reward_quote < MIN_NET_REWARD_TO_RISK * planned_risk_quote:
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
    filters: SymbolFilters,
) -> RiskRewardPlan:
    """Re-solve and RE-VALIDATE the plan from the REAL simulated entry
    fill price - never from the earlier reference price or the signal's
    close. `plan` must be an APPROVED plan (quantity already decided);
    the quantity itself never changes here, only the price anchor and the
    approve/reject decision, which is checked FRESH from scratch:

      - planned NET loss must still be <= 1% of `equity` (the SAME
        pre-entry equity `plan_risk_reward_entry` was given - not equity
        after the fill);
      - planned NET reward/risk must still be >= `MIN_NET_REWARD_TO_RISK`.

    If EITHER check fails, the returned plan has `approved=False`
    (`RR_REJECTED_POST_FILL_REVALIDATION_FAILED`) and the caller
    (`backtest/engine.py::run_segment`) MUST fail closed: never create the
    position at all. This is the plan that, when approved, gets persisted
    with the open position and checked on every subsequent candle.
    """
    if not plan.approved or plan.quantity is None:
        raise ValueError("build_realized_plan requires an approved plan with a decided quantity")

    fee = _to_decimal(taker_fee_pct)
    slippage = _to_decimal(slippage_pct)
    quantity = plan.quantity
    stop_price = fill_price * (1 - _to_decimal(stop_distance_pct))

    loss_per_unit = _loss_per_unit(fill_price, stop_price, fee, slippage)
    if loss_per_unit <= 0:
        return replace(plan, approved=False, reason_code=RR_REJECTED_POST_FILL_REVALIDATION_FAILED, stop_price=stop_price, target_price=None)

    raw_target = _solve_target_price(fill_price, loss_per_unit, fee, slippage)
    target_price = _round_target_up_to_tick(raw_target, filters)
    if target_price is None or target_price <= fill_price:
        return replace(plan, approved=False, reason_code=RR_REJECTED_POST_FILL_REVALIDATION_FAILED, stop_price=stop_price, target_price=None)

    gain_per_unit = _gain_per_unit(fill_price, target_price, fee, slippage)
    planned_risk_quote = quantity * loss_per_unit
    planned_reward_quote = quantity * gain_per_unit
    risk_budget = equity * _to_decimal(FIXED_MAX_RISK_PER_TRADE_PCT)

    gross_reward_to_risk = float((target_price - fill_price) / (fill_price - stop_price))
    net_reward_to_risk = float(planned_reward_quote / planned_risk_quote) if planned_risk_quote > 0 else None
    planned_risk_pct = float(planned_risk_quote / equity) if equity > 0 else None
    planned_reward_pct = float(planned_reward_quote / equity) if equity > 0 else None

    risk_within_budget = planned_risk_quote <= risk_budget
    reward_meets_minimum = planned_reward_quote >= MIN_NET_REWARD_TO_RISK * planned_risk_quote

    if not (risk_within_budget and reward_meets_minimum):
        return replace(
            plan, approved=False, reason_code=RR_REJECTED_POST_FILL_REVALIDATION_FAILED,
            stop_price=stop_price, target_price=target_price,
            planned_risk_quote=planned_risk_quote, planned_risk_pct=planned_risk_pct,
            planned_reward_quote=planned_reward_quote, planned_reward_pct=planned_reward_pct,
            gross_reward_to_risk=gross_reward_to_risk, net_reward_to_risk=net_reward_to_risk,
        )

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
    #: A post-fill revalidation failure (see `build_realized_plan`) - the
    #: buy fill was computed but the position was never created. Expected
    #: to be rare (this project's fill model is deterministic, so the
    #: pre-fill and post-fill plans normally agree exactly), but is a
    #: real, always-checked fail-closed safety net, not a placebo.
    entries_rejected_post_fill_revalidation: int = 0
    stop_loss_exits: int = 0
    take_profit_exits: int = 0
    gap_losses_exceeding_planned_risk: int = 0
    planned_risk_quote_total: Decimal = Decimal(0)
    planned_reward_quote_total: Decimal = Decimal(0)
    planned_risk_pct_values: tuple[float, ...] = ()
    planned_reward_pct_values: tuple[float, ...] = ()
    gross_reward_to_risk_values: tuple[float, ...] = ()
    net_reward_to_risk_values: tuple[float, ...] = ()
    #: Per-approved-entry PLANNED risk/reward in QUOTE currency (exact
    #: Decimal, one value per approved entry, same chronological order as
    #: `planned_risk_pct_values`/the entry-approval sequence itself) - pure
    #: read-only instrumentation added for post-mortem reporting (`research/
    #: post_mortem.py`'s per-trade R-multiples). Records values `build_
    #: realized_plan` already computes; adds no new computation, decision,
    #: or gate of any kind.
    planned_risk_quote_values: tuple[Decimal, ...] = ()
    planned_reward_quote_values: tuple[Decimal, ...] = ()
