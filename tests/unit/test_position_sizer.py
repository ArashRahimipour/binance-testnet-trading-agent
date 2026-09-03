from decimal import Decimal

from tests.fixtures.exchange_info import make_exchange_info
from trading_agent.sizing.exchange_filters import SymbolFilters
from trading_agent.sizing.position_sizer import compute_buy_quantity, compute_sell_quantity


def _filters(**kwargs) -> SymbolFilters:
    return SymbolFilters.from_exchange_info(make_exchange_info(**kwargs))


def test_buy_sizing_respects_allocation_and_buffer():
    filters = _filters()
    decision = compute_buy_quantity(
        available_quote_balance=Decimal(50),
        price=Decimal(50000),
        max_allocation_pct=0.9,
        min_quote_buffer=Decimal(5),
        filters=filters,
    )
    assert decision.approved is True
    # spendable = 45, budget = 40.5, qty = 40.5/50000 = 0.00081 -> rounds to step 0.00001
    assert decision.quantity == Decimal("0.00081")
    assert decision.quantity * Decimal(50000) <= Decimal("40.5")


def test_buy_sizing_rejects_when_below_min_quote_buffer():
    filters = _filters()
    decision = compute_buy_quantity(
        available_quote_balance=Decimal(4),
        price=Decimal(50000),
        max_allocation_pct=0.9,
        min_quote_buffer=Decimal(5),
        filters=filters,
    )
    assert decision.approved is False
    assert decision.reason_code == "BELOW_MIN_QUOTE_BUFFER"
    assert decision.quantity is None


def test_buy_sizing_rejects_when_below_min_notional_never_bumps_up():
    # Tiny balance: risk-compliant size is smaller than exchange minimum notional.
    filters = _filters(min_notional="10")
    decision = compute_buy_quantity(
        available_quote_balance=Decimal(6),
        price=Decimal(50000),
        max_allocation_pct=0.9,
        min_quote_buffer=Decimal(5),
        filters=filters,
    )
    assert decision.approved is False
    assert decision.reason_code == "BELOW_MIN_NOTIONAL"
    assert decision.quantity is None  # never enlarged to satisfy the exchange minimum


def test_buy_sizing_rejects_zero_or_negative_price():
    filters = _filters()
    decision = compute_buy_quantity(
        available_quote_balance=Decimal(50),
        price=Decimal(0),
        max_allocation_pct=0.9,
        min_quote_buffer=Decimal(5),
        filters=filters,
    )
    assert decision.approved is False
    assert decision.reason_code == "INVALID_PRICE"


def test_sell_sizing_never_exceeds_held_quantity():
    filters = _filters(step_size="0.001")
    decision = compute_sell_quantity(
        held_quantity=Decimal("0.0019999"), price=Decimal(50000), filters=filters
    )
    assert decision.approved is True
    assert decision.quantity <= Decimal("0.0019999")
    assert decision.quantity == Decimal("0.001")


def test_sell_sizing_rejects_dust_below_min_lot():
    filters = _filters(step_size="0.001", min_qty="0.001")
    decision = compute_sell_quantity(
        held_quantity=Decimal("0.0005"), price=Decimal(50000), filters=filters
    )
    assert decision.approved is False
    assert decision.reason_code in {"BELOW_MIN_LOT_SIZE", "NO_SELLABLE_QUANTITY"}


def test_sell_sizing_rejects_below_min_notional():
    filters = _filters(min_notional="1000", step_size="0.001", min_qty="0.001")
    decision = compute_sell_quantity(
        held_quantity=Decimal("0.001"), price=Decimal(50000), filters=filters
    )
    assert decision.approved is False
    assert decision.reason_code == "BELOW_MIN_NOTIONAL"
