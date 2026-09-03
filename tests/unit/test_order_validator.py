from decimal import Decimal

from tests.fixtures.exchange_info import make_exchange_info
from trading_agent.execution.order_validator import validate_order
from trading_agent.risk.limits import TradeIntent
from trading_agent.sizing.exchange_filters import SymbolFilters
from trading_agent.strategy.base import SignalType


def _filters(**kwargs) -> SymbolFilters:
    return SymbolFilters.from_exchange_info(make_exchange_info(**kwargs))


def test_validated_order_rounds_quantity_down():
    filters = _filters(step_size="0.001")
    intent = TradeIntent(SignalType.BUY, "BTCUSDT", Decimal("0.0019999"), Decimal(50000))
    result = validate_order(intent, filters)
    assert result.approved is True
    assert result.validated_quantity == Decimal("0.001")


def test_rejects_below_min_notional_independently_of_sizer():
    filters = _filters(min_notional="1000")
    intent = TradeIntent(SignalType.BUY, "BTCUSDT", Decimal("0.001"), Decimal(50000))
    result = validate_order(intent, filters)
    assert result.approved is False
    assert result.reason_code == "BELOW_MIN_NOTIONAL"
    assert result.validated_quantity is None


def test_rejects_below_min_lot_size():
    filters = _filters(min_qty="0.01")
    intent = TradeIntent(SignalType.BUY, "BTCUSDT", Decimal("0.001"), Decimal(50000))
    result = validate_order(intent, filters)
    assert result.approved is False
    assert result.reason_code == "BELOW_MIN_LOT_SIZE"


def test_rejects_symbol_mismatch():
    filters = _filters(symbol="ETHUSDT")
    intent = TradeIntent(SignalType.BUY, "BTCUSDT", Decimal("0.001"), Decimal(50000))
    result = validate_order(intent, filters)
    assert result.approved is False
    assert result.reason_code == "SYMBOL_MISMATCH"


def test_rejects_zero_quantity_after_rounding():
    filters = _filters(step_size="0.01")
    intent = TradeIntent(SignalType.BUY, "BTCUSDT", Decimal("0.001"), Decimal(50000))
    result = validate_order(intent, filters)
    assert result.approved is False
    assert result.reason_code == "ZERO_QUANTITY_AFTER_ROUNDING"
