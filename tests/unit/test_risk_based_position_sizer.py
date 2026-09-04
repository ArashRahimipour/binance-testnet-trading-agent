from decimal import Decimal

from tests.fixtures.exchange_info import make_exchange_info
from trading_agent.sizing.exchange_filters import SymbolFilters
from trading_agent.sizing.position_sizer import compute_risk_based_buy_quantity


def _filters(**kwargs) -> SymbolFilters:
    return SymbolFilters.from_exchange_info(make_exchange_info(**kwargs))


def test_sizes_from_risk_budget_over_stop_distance():
    # equity=1000, risk 2% = 20 budget. entry=50000, stop=47500 -> stop distance=2500.
    # quantity = 20 / 2500 = 0.008
    filters = _filters(step_size="0.0001", min_notional="1")
    decision = compute_risk_based_buy_quantity(
        equity=Decimal(1000),
        entry_price=Decimal(50000),
        stop_price=Decimal(47500),
        max_risk_per_trade_pct=0.02,
        max_position_pct=0.90,
        filters=filters,
    )
    assert decision.approved is True
    assert decision.quantity == Decimal("0.008")


def test_tight_stop_allows_larger_notional_for_same_risk_budget():
    # Same risk budget (20), but a tighter stop distance (500 instead of 2500)
    # legitimately allows a larger quantity - proving risk, not notional, drives sizing.
    filters = _filters(step_size="0.0001", min_notional="1")
    wide_stop = compute_risk_based_buy_quantity(
        Decimal(1000), Decimal(50000), Decimal(47500), 0.02, 0.90, filters
    )
    tight_stop = compute_risk_based_buy_quantity(
        Decimal(1000), Decimal(50000), Decimal(49500), 0.02, 0.90, filters
    )
    assert tight_stop.quantity > wide_stop.quantity


def test_position_cap_limits_size_even_with_very_tight_stop():
    # An extremely tight stop would imply a huge risk-based quantity; the
    # separate max_position_pct notional ceiling must still cap it.
    filters = _filters(step_size="0.00001", min_notional="1", max_qty="9000")
    decision = compute_risk_based_buy_quantity(
        equity=Decimal(1000),
        entry_price=Decimal(50000),
        stop_price=Decimal(49999),  # stop distance = 1 -> risk-based qty would be 20
        max_risk_per_trade_pct=0.02,
        max_position_pct=0.10,  # cap notional at 100 -> max quantity = 100/50000 = 0.002
        filters=filters,
    )
    assert decision.approved is True
    assert decision.quantity == Decimal("0.002")


def test_rejects_when_below_min_notional_never_bumps_up():
    filters = _filters(step_size="0.0001", min_notional="1000")
    decision = compute_risk_based_buy_quantity(
        equity=Decimal(50),
        entry_price=Decimal(50000),
        stop_price=Decimal(47500),
        max_risk_per_trade_pct=0.02,
        max_position_pct=0.90,
        filters=filters,
    )
    assert decision.approved is False
    assert decision.reason_code == "BELOW_MIN_NOTIONAL"
    assert decision.quantity is None


def test_rejects_invalid_stop_distance_when_stop_at_or_above_entry():
    filters = _filters()
    decision = compute_risk_based_buy_quantity(
        Decimal(1000), Decimal(50000), Decimal(50000), 0.02, 0.90, filters
    )
    assert decision.approved is False
    assert decision.reason_code == "INVALID_STOP_DISTANCE"


def test_rejects_non_positive_prices():
    filters = _filters()
    decision = compute_risk_based_buy_quantity(Decimal(1000), Decimal(0), Decimal(0), 0.02, 0.90, filters)
    assert decision.approved is False
    assert decision.reason_code == "INVALID_PRICE"
