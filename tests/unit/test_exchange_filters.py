from decimal import Decimal

from tests.fixtures.exchange_info import make_exchange_info
from trading_agent.sizing.exchange_filters import (
    SymbolFilters,
    meets_lot_size,
    meets_min_notional,
    round_down_to_step,
    round_price,
    round_quantity,
)


def test_parses_filters_from_exchange_info():
    filters = SymbolFilters.from_exchange_info(make_exchange_info())
    assert filters.symbol == "BTCUSDT"
    assert filters.tick_size == Decimal("0.01000000")
    assert filters.step_size == Decimal("0.00001000")
    assert filters.min_notional == Decimal("5.00000000")


def test_parses_legacy_min_notional_filter():
    info = make_exchange_info()
    info["symbols"][0]["filters"][2] = {
        "filterType": "MIN_NOTIONAL",
        "minNotional": "10.00000000",
        "applyToMarket": True,
        "avgPriceMins": 5,
    }
    filters = SymbolFilters.from_exchange_info(info)
    assert filters.min_notional == Decimal("10.00000000")
    assert filters.apply_min_notional_to_market is True


def test_round_down_to_step_basic():
    assert round_down_to_step(Decimal("1.23456"), Decimal("0.001")) == Decimal("1.234")


def test_round_down_to_step_disabled_when_zero():
    assert round_down_to_step(Decimal("1.23456789"), Decimal(0)) == Decimal("1.23456789")


def test_round_quantity_never_rounds_up():
    filters = SymbolFilters.from_exchange_info(make_exchange_info(step_size="0.001"))
    rounded = round_quantity(Decimal("0.0019999"), filters)
    assert rounded == Decimal("0.001")
    assert rounded <= Decimal("0.0019999")


def test_round_price_to_tick():
    filters = SymbolFilters.from_exchange_info(make_exchange_info(tick_size="0.5"))
    assert round_price(Decimal("101.37"), filters) == Decimal("101.0")


def test_meets_min_notional_true_and_false():
    filters = SymbolFilters.from_exchange_info(make_exchange_info(min_notional="10"))
    assert meets_min_notional(Decimal(100), Decimal("0.2"), filters) is True  # notional 20
    assert meets_min_notional(Decimal(100), Decimal("0.05"), filters) is False  # notional 5


def test_meets_lot_size_bounds():
    filters = SymbolFilters.from_exchange_info(
        make_exchange_info(min_qty="0.01", max_qty="10")
    )
    assert meets_lot_size(Decimal("0.005"), filters) is False
    assert meets_lot_size(Decimal("0.01"), filters) is True
    assert meets_lot_size(Decimal(11), filters) is False
