"""Proofs for backtest/risk_reward.py: the fixed minimum 1:2 planned
reward/risk policy - a user-mandated pre-real-evaluation risk policy (see
that module's docstring), applied identically to every research candidate.

The take-profit target is SOLVED ALGEBRAICALLY so the NET (cost-adjusted)
reward/risk ratio is exactly 2.0 before tick rounding - this means a
normal trade with realistic nonzero fees/slippage CAN be approved (the
policy is not a universal rejection), and the GROSS ratio comes out AT OR
ABOVE 2.0, exactly 2.0 only when costs are zero.
"""

from decimal import Decimal

from tests.fixtures.exchange_info import make_exchange_info
from trading_agent.backtest.risk_reward import (
    EXCHANGE_FILTER_REJECTION_REASONS,
    FIXED_MAX_RISK_PER_TRADE_PCT,
    MIN_NET_REWARD_TO_RISK,
    RR_APPROVED,
    RR_REJECTED_BELOW_MIN_NOTIONAL,
    build_realized_plan,
    plan_risk_reward_entry,
)
from trading_agent.sizing.exchange_filters import SymbolFilters

STARTING_EQUITY = Decimal(50)
ENTRY_PRICE = Decimal(100)
STOP_DISTANCE_PCT = 0.05


def _filters(**overrides) -> SymbolFilters:
    return SymbolFilters.from_exchange_info(make_exchange_info(**overrides))


# --- A normal trade with realistic costs is approvable (not a universal rejection). ---


def test_a_normal_trade_with_nonzero_fees_and_slippage_can_be_approved():
    plan = plan_risk_reward_entry(
        STARTING_EQUITY, STARTING_EQUITY, ENTRY_PRICE, STOP_DISTANCE_PCT,
        taker_fee_pct=0.001, slippage_pct=0.0005, filters=_filters(min_notional="0.01"),
    )
    assert plan.approved
    assert plan.reason_code == RR_APPROVED
    assert plan.net_reward_to_risk is not None
    assert plan.net_reward_to_risk >= float(MIN_NET_REWARD_TO_RISK)


def test_gross_rr_exceeds_2r_when_costs_are_nonzero():
    plan = plan_risk_reward_entry(
        STARTING_EQUITY, STARTING_EQUITY, ENTRY_PRICE, STOP_DISTANCE_PCT,
        taker_fee_pct=0.001, slippage_pct=0.0005, filters=_filters(min_notional="0.01"),
    )
    assert plan.approved
    assert plan.gross_reward_to_risk is not None
    assert plan.gross_reward_to_risk > float(MIN_NET_REWARD_TO_RISK)


def test_net_rr_stays_at_the_fixed_2r_floor_regardless_of_costs():
    zero_cost = plan_risk_reward_entry(
        STARTING_EQUITY, STARTING_EQUITY, ENTRY_PRICE, STOP_DISTANCE_PCT,
        taker_fee_pct=0.0, slippage_pct=0.0, filters=_filters(min_notional="0.01"),
    )
    with_cost = plan_risk_reward_entry(
        STARTING_EQUITY, STARTING_EQUITY, ENTRY_PRICE, STOP_DISTANCE_PCT,
        taker_fee_pct=0.001, slippage_pct=0.0005, filters=_filters(min_notional="0.01"),
    )
    assert zero_cost.approved and with_cost.approved
    assert zero_cost.net_reward_to_risk is not None and with_cost.net_reward_to_risk is not None
    assert abs(zero_cost.net_reward_to_risk - float(MIN_NET_REWARD_TO_RISK)) < 1e-6
    assert with_cost.net_reward_to_risk >= float(MIN_NET_REWARD_TO_RISK) - 1e-6
    # Gross rises to compensate for costs; net stays pinned at the fixed floor.
    assert with_cost.gross_reward_to_risk > zero_cost.gross_reward_to_risk


def test_zero_cost_case_produces_exactly_gross_and_net_2r():
    plan = plan_risk_reward_entry(
        STARTING_EQUITY, STARTING_EQUITY, ENTRY_PRICE, STOP_DISTANCE_PCT,
        taker_fee_pct=0.0, slippage_pct=0.0, filters=_filters(min_notional="0.01"),
    )
    assert plan.approved
    assert plan.gross_reward_to_risk == float(MIN_NET_REWARD_TO_RISK)
    assert plan.net_reward_to_risk == float(MIN_NET_REWARD_TO_RISK)


# --- $50 sizing: net planned risk/reward figures. ---


def test_net_planned_risk_is_at_most_half_a_dollar_on_50_dollar_account():
    plan = plan_risk_reward_entry(
        STARTING_EQUITY, STARTING_EQUITY, ENTRY_PRICE, STOP_DISTANCE_PCT,
        taker_fee_pct=0.001, slippage_pct=0.0005, filters=_filters(min_notional="0.01"),
    )
    assert plan.approved
    assert plan.planned_risk_quote is not None
    assert plan.planned_risk_quote <= Decimal("0.50")
    assert plan.planned_risk_pct is not None
    assert plan.planned_risk_pct <= FIXED_MAX_RISK_PER_TRADE_PCT + 1e-9


def test_net_planned_reward_is_at_least_one_dollar_when_full_risk_budget_is_used():
    # A generous quote balance and permissive filters let the full 1% risk
    # budget be used (not capped by affordability or lot-size rounding).
    plan = plan_risk_reward_entry(
        STARTING_EQUITY, STARTING_EQUITY, ENTRY_PRICE, STOP_DISTANCE_PCT,
        taker_fee_pct=0.0, slippage_pct=0.0, filters=_filters(min_notional="0.0000001", step_size="0.00000001"),
    )
    assert plan.approved
    assert plan.planned_risk_quote is not None and plan.planned_reward_quote is not None
    assert plan.planned_risk_quote >= Decimal("0.49")  # close to the full $0.50 budget
    assert plan.planned_reward_quote >= Decimal("1.00") - Decimal("0.02")


# --- Fee reserve / affordability / exchange filters. ---


def test_fees_are_reserved_and_never_cause_overspending():
    tight_quote_balance = Decimal("1.00")
    plan = plan_risk_reward_entry(
        STARTING_EQUITY, tight_quote_balance, ENTRY_PRICE, STOP_DISTANCE_PCT,
        taker_fee_pct=0.001, slippage_pct=0.0005, filters=_filters(min_notional="0.01"),
    )
    if plan.approved:
        assert plan.quantity is not None
        effective_entry = ENTRY_PRICE * Decimal("1.0005")
        cost_with_fee = plan.quantity * effective_entry * Decimal("1.001")
        assert cost_with_fee <= tight_quote_balance


def test_exchange_minimum_notional_rejects_rather_than_increases_risk():
    plan = plan_risk_reward_entry(
        STARTING_EQUITY, STARTING_EQUITY, ENTRY_PRICE, STOP_DISTANCE_PCT,
        taker_fee_pct=0.0, slippage_pct=0.0, filters=_filters(min_notional="100000"),
    )
    assert not plan.approved
    assert plan.reason_code == RR_REJECTED_BELOW_MIN_NOTIONAL
    assert plan.reason_code in EXCHANGE_FILTER_REJECTION_REASONS
    assert plan.quantity is None


def test_invalid_inputs_are_rejected_without_raising():
    filters = _filters()
    assert not plan_risk_reward_entry(Decimal(0), STARTING_EQUITY, ENTRY_PRICE, 0.05, 0.001, 0.0005, filters).approved
    assert not plan_risk_reward_entry(STARTING_EQUITY, Decimal(0), ENTRY_PRICE, 0.05, 0.001, 0.0005, filters).approved
    assert not plan_risk_reward_entry(STARTING_EQUITY, STARTING_EQUITY, Decimal(0), 0.05, 0.001, 0.0005, filters).approved
    assert not plan_risk_reward_entry(STARTING_EQUITY, STARTING_EQUITY, ENTRY_PRICE, 0.0, 0.001, 0.0005, filters).approved


# --- Tick rounding must never push net R/R below the 2.0 floor. ---


def test_target_tick_rounding_cannot_reduce_net_rr_below_2r():
    # A coarse tick size relative to the price forces meaningful rounding -
    # net R/R must still come out at or above 2.0 (rounding UP protects it).
    coarse_filters = _filters(tick_size="1", min_notional="0.01")
    plan = plan_risk_reward_entry(
        STARTING_EQUITY, STARTING_EQUITY, ENTRY_PRICE, STOP_DISTANCE_PCT,
        taker_fee_pct=0.001, slippage_pct=0.0005, filters=coarse_filters,
    )
    assert plan.approved
    assert plan.net_reward_to_risk is not None
    assert plan.net_reward_to_risk >= float(MIN_NET_REWARD_TO_RISK)
    # The rounded target must land exactly on a $1 tick boundary.
    assert plan.target_price is not None
    assert plan.target_price == plan.target_price.to_integral_value()


# --- Realized plan (anchored to the actual fill, never the reference price). ---


def test_build_realized_plan_anchors_stop_and_target_to_the_real_fill_price():
    plan = plan_risk_reward_entry(
        STARTING_EQUITY, STARTING_EQUITY, ENTRY_PRICE, STOP_DISTANCE_PCT,
        taker_fee_pct=0.0, slippage_pct=0.0, filters=_filters(min_notional="0.01"),
    )
    assert plan.approved
    # A fill BELOW the reference price (favourable for a long entry - the
    # quantity was fixed pre-fill, so a lower entry means proportionally
    # LESS dollar risk than budgeted) - anchoring is demonstrated without
    # tripping the revalidation's risk-budget check, which a higher fill
    # price would (see test_post_fill_revalidation_fails_closed_... below:
    # this policy sizes tightly to exactly the 1% budget, so ANY fill
    # price above the reference breaches it by construction).
    fill_price = Decimal(99)
    realized = build_realized_plan(
        plan, fill_price, STOP_DISTANCE_PCT, 0.0, 0.0, STARTING_EQUITY, _filters(min_notional="0.01"),
    )
    assert realized.approved
    assert realized.stop_price == fill_price * Decimal("0.95")
    assert realized.quantity == plan.quantity  # quantity is decided pre-fill and never changes here
    assert realized.gross_reward_to_risk == float(MIN_NET_REWARD_TO_RISK)  # zero cost: gross == net == 2.0
    assert realized.net_reward_to_risk == float(MIN_NET_REWARD_TO_RISK)


def test_post_fill_revalidation_fails_closed_when_the_fill_moves_against_the_plan():
    # Simulate a fill price dramatically worse than what the plan assumed
    # (e.g. execution slippage far beyond the modeled estimate) - the
    # realized plan must be rejected rather than silently accepted.
    plan = plan_risk_reward_entry(
        STARTING_EQUITY, STARTING_EQUITY, ENTRY_PRICE, STOP_DISTANCE_PCT,
        taker_fee_pct=0.001, slippage_pct=0.0005, filters=_filters(min_notional="0.01"),
    )
    assert plan.approved
    # Simulate a much higher effective cost structure discovered only at
    # fill time: since quantity is fixed pre-fill, a dramatically larger
    # fee inflates the recomputed planned NET loss (quantity * loss_per_unit)
    # far past the original 1%-of-equity risk budget - the revalidation's
    # risk check must catch this and fail closed.
    adversarial_fee = 0.20  # 20% "fee" - unrealistic, but proves the safety net independent of realism
    realized = build_realized_plan(
        plan, ENTRY_PRICE, STOP_DISTANCE_PCT, adversarial_fee, 0.0005, STARTING_EQUITY, _filters(min_notional="0.01"),
    )
    assert not realized.approved
    assert realized.reason_code == "RR_REJECTED_POST_FILL_REVALIDATION_FAILED"
