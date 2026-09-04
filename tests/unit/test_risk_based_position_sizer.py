from decimal import Decimal

from tests.fixtures.exchange_info import make_exchange_info
from trading_agent.sizing.exchange_filters import SymbolFilters
from trading_agent.sizing.position_sizer import compute_risk_based_buy_quantity


def _filters(**kwargs) -> SymbolFilters:
    return SymbolFilters.from_exchange_info(make_exchange_info(**kwargs))


def test_sizes_from_risk_budget_over_stop_distance():
    # equity=1000, risk 2% = 20 budget. entry=50000, stop=47500 -> stop distance=2500.
    # No slippage/fees here -> quantity = 20 / 2500 = 0.008 (see the
    # cost-aware tests below for the round 2 Finding 6 behavior).
    filters = _filters(step_size="0.0001", min_notional="1")
    decision = compute_risk_based_buy_quantity(
        equity=Decimal(1000),
        entry_price=Decimal(50000),
        stop_price=Decimal(47500),
        max_risk_per_trade_pct=0.02,
        max_position_pct=0.90,
        taker_fee_pct=0.0,
        slippage_pct=0.0,
        filters=filters,
    )
    assert decision.approved is True
    assert decision.quantity == Decimal("0.008")


def test_tight_stop_allows_larger_notional_for_same_risk_budget():
    # Same risk budget (20), but a tighter stop distance (500 instead of 2500)
    # legitimately allows a larger quantity - proving risk, not notional, drives sizing.
    filters = _filters(step_size="0.0001", min_notional="1")
    wide_stop = compute_risk_based_buy_quantity(
        Decimal(1000), Decimal(50000), Decimal(47500), 0.02, 0.90, 0.0, 0.0, filters
    )
    tight_stop = compute_risk_based_buy_quantity(
        Decimal(1000), Decimal(50000), Decimal(49500), 0.02, 0.90, 0.0, 0.0, filters
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
        taker_fee_pct=0.0,
        slippage_pct=0.0,
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
        taker_fee_pct=0.0,
        slippage_pct=0.0,
        filters=filters,
    )
    assert decision.approved is False
    assert decision.reason_code == "BELOW_MIN_NOTIONAL"
    assert decision.quantity is None


def test_rejects_invalid_stop_distance_when_stop_at_or_above_entry():
    filters = _filters()
    decision = compute_risk_based_buy_quantity(
        Decimal(1000), Decimal(50000), Decimal(50000), 0.02, 0.90, 0.0, 0.0, filters
    )
    assert decision.approved is False
    assert decision.reason_code == "INVALID_STOP_DISTANCE"


def test_rejects_non_positive_prices():
    filters = _filters()
    decision = compute_risk_based_buy_quantity(Decimal(1000), Decimal(0), Decimal(0), 0.02, 0.90, 0.0, 0.0, filters)
    assert decision.approved is False
    assert decision.reason_code == "INVALID_PRICE"


# --- Round 2 finding #6: cost-aware sizing (entry/exit slippage + fees). ---


def test_cost_aware_sizing_produces_smaller_quantity_than_bare_price_gap():
    # Same entry/stop as test_sizes_from_risk_budget_over_stop_distance
    # (bare-gap quantity 0.008), but with realistic slippage and fees the
    # true expected loss per unit is larger, so the sized quantity must be
    # smaller - never larger, which would silently risk more than budgeted.
    filters = _filters(step_size="0.0001", min_notional="1")
    bare = compute_risk_based_buy_quantity(
        Decimal(1000), Decimal(50000), Decimal(47500), 0.02, 0.90, 0.0, 0.0, filters
    )
    cost_aware = compute_risk_based_buy_quantity(
        Decimal(1000), Decimal(50000), Decimal(47500), 0.02, 0.90,
        taker_fee_pct=0.001, slippage_pct=0.001, filters=filters,
    )
    assert cost_aware.approved is True
    assert cost_aware.quantity < bare.quantity


def test_expected_ordinary_stop_loss_stays_within_risk_budget():
    # Sizing so that filling exactly at the stop price - with the SAME
    # slippage/fee model the backtest broker actually applies - never loses
    # more than the configured risk budget. This is the "ordinary, non-gap"
    # case the docstring promises; the gap-through-stop case is covered in
    # tests/unit/test_backtest_engine.py.
    equity = Decimal(1000)
    max_risk_per_trade_pct = 0.02
    taker_fee_pct = 0.001
    slippage_pct = 0.001
    entry_price = Decimal(50000)
    stop_price = Decimal(47500)
    filters = _filters(step_size="0.00001", min_notional="1")

    decision = compute_risk_based_buy_quantity(
        equity, entry_price, stop_price, max_risk_per_trade_pct, 0.90,
        taker_fee_pct, slippage_pct, filters,
    )
    assert decision.approved is True

    effective_entry = entry_price * (1 + Decimal(str(slippage_pct)))
    effective_exit = stop_price * (1 - Decimal(str(slippage_pct)))
    entry_fee = decision.quantity * effective_entry * Decimal(str(taker_fee_pct))
    exit_fee = decision.quantity * effective_exit * Decimal(str(taker_fee_pct))
    expected_loss = decision.quantity * (effective_entry - effective_exit) + entry_fee + exit_fee

    assert expected_loss <= equity * Decimal(str(max_risk_per_trade_pct))
