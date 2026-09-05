"""Proofs for backtest/risk_reward.py: the fixed minimum 1:2 planned
reward/risk policy - a user-mandated pre-real-evaluation risk policy (see
that module's docstring), applied identically to every research candidate.
"""

from decimal import Decimal

from tests.fixtures.exchange_info import make_exchange_info
from trading_agent.backtest.risk_reward import (
    EXCHANGE_FILTER_REJECTION_REASONS,
    FIXED_MAX_RISK_PER_TRADE_PCT,
    GROSS_REWARD_TO_RISK_MULTIPLE,
    MIN_NET_REWARD_TO_RISK,
    RR_APPROVED,
    RR_REJECTED_BELOW_MIN_NOTIONAL,
    RR_REJECTED_NET_REWARD_TO_RISK_BELOW_MINIMUM,
    build_realized_plan,
    plan_risk_reward_entry,
)
from trading_agent.sizing.exchange_filters import SymbolFilters

STARTING_EQUITY = Decimal(50)
ENTRY_PRICE = Decimal(100)
STOP_DISTANCE_PCT = 0.05


def _filters(**overrides) -> SymbolFilters:
    return SymbolFilters.from_exchange_info(make_exchange_info(**overrides))


# --- Exactly 2R acceptance / gross vs. net. ---


def test_plan_is_approved_with_exactly_2r_net_when_costs_are_zero():
    plan = plan_risk_reward_entry(
        STARTING_EQUITY, STARTING_EQUITY, ENTRY_PRICE, STOP_DISTANCE_PCT,
        taker_fee_pct=0.0, slippage_pct=0.0, filters=_filters(),
    )
    assert plan.approved
    assert plan.reason_code == RR_APPROVED
    assert plan.gross_reward_to_risk == GROSS_REWARD_TO_RISK_MULTIPLE
    # Zero costs: net == gross == exactly the minimum - the >= boundary is inclusive.
    assert plan.net_reward_to_risk == GROSS_REWARD_TO_RISK_MULTIPLE
    assert plan.net_reward_to_risk >= MIN_NET_REWARD_TO_RISK


def test_plan_rejects_below_2r_net_when_costs_are_present():
    plan = plan_risk_reward_entry(
        STARTING_EQUITY, STARTING_EQUITY, ENTRY_PRICE, STOP_DISTANCE_PCT,
        taker_fee_pct=0.001, slippage_pct=0.0005, filters=_filters(),
    )
    assert not plan.approved
    assert plan.reason_code == RR_REJECTED_NET_REWARD_TO_RISK_BELOW_MINIMUM
    assert plan.net_reward_to_risk is not None
    assert plan.net_reward_to_risk < MIN_NET_REWARD_TO_RISK
    assert plan.quantity is None


def test_fee_and_slippage_reduce_gross_2r_below_net_2r():
    # Same entry/stop, only cost parameters differ - isolates the erosion mechanism.
    zero_cost = plan_risk_reward_entry(
        STARTING_EQUITY, STARTING_EQUITY, ENTRY_PRICE, STOP_DISTANCE_PCT,
        taker_fee_pct=0.0, slippage_pct=0.0, filters=_filters(),
    )
    with_cost = plan_risk_reward_entry(
        STARTING_EQUITY, STARTING_EQUITY, ENTRY_PRICE, STOP_DISTANCE_PCT,
        taker_fee_pct=0.001, slippage_pct=0.0005, filters=_filters(),
    )
    assert zero_cost.gross_reward_to_risk == with_cost.gross_reward_to_risk == GROSS_REWARD_TO_RISK_MULTIPLE
    assert zero_cost.net_reward_to_risk == GROSS_REWARD_TO_RISK_MULTIPLE
    assert with_cost.net_reward_to_risk is not None and with_cost.net_reward_to_risk < zero_cost.net_reward_to_risk
    assert zero_cost.approved and not with_cost.approved


# --- $50 sizing / fixed 1% risk budget. ---


def test_planned_risk_is_one_percent_of_equity_on_50_dollar_account():
    plan = plan_risk_reward_entry(
        STARTING_EQUITY, STARTING_EQUITY, ENTRY_PRICE, STOP_DISTANCE_PCT,
        taker_fee_pct=0.0, slippage_pct=0.0, filters=_filters(min_notional="0.01"),
    )
    assert plan.approved
    assert plan.planned_risk_pct is not None
    assert abs(plan.planned_risk_pct - FIXED_MAX_RISK_PER_TRADE_PCT) < 1e-9
    assert plan.planned_risk_quote is not None
    # $50 * 1% == $0.50, up to lot-size rounding.
    assert abs(plan.planned_risk_quote - Decimal("0.50")) < Decimal("0.02")


def test_fees_are_reserved_and_never_cause_overspending():
    # Quote balance deliberately smaller than what the pure risk-based
    # quantity would want, so the affordability cap - not the risk budget -
    # decides sizing; the fee reserve must still leave room for the entry fee.
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
    # min_notional far beyond what a 1%-of-$50 risk budget could ever satisfy.
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


# --- Realized plan (anchored to the actual fill, never the reference price). ---


def test_build_realized_plan_anchors_stop_and_target_to_the_real_fill_price():
    plan = plan_risk_reward_entry(
        STARTING_EQUITY, STARTING_EQUITY, ENTRY_PRICE, STOP_DISTANCE_PCT,
        taker_fee_pct=0.0, slippage_pct=0.0, filters=_filters(min_notional="0.01"),
    )
    assert plan.approved
    # A materially different fill price than the reference (e.g. slippage moved it).
    fill_price = Decimal(101)
    realized = build_realized_plan(plan, fill_price, STOP_DISTANCE_PCT, 0.0, 0.0, STARTING_EQUITY)
    assert realized.stop_price == fill_price * Decimal("0.95")
    assert realized.target_price == fill_price * Decimal("1.10")
    assert realized.quantity == plan.quantity  # quantity is decided pre-fill and never changes here
    assert realized.gross_reward_to_risk == GROSS_REWARD_TO_RISK_MULTIPLE
