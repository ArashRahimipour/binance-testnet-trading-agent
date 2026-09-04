"""Comprehensive tests for every Binance order status the dispatcher must
handle distinctly (review Finding 1), rewritten for the round-2 pure
function `compute_order_application`, which derives deltas from Binance's
cumulative `executed_qty`/`cumulative_quote_qty` fields and buckets
commission by the asset it was actually charged in (Findings 3 and 4)."""

from decimal import Decimal

import pytest

from trading_agent.execution.order_outcome import (
    InconsistentExecutionReportError,
    compute_order_application,
)
from trading_agent.execution.testnet_adapter import Fill, OrderResult
from trading_agent.portfolio.state import PortfolioState, apply_buy
from trading_agent.strategy.base import PositionSide

QUOTE_ASSET = "USDT"
BASE_ASSET = "BTC"


def _order(status: str, executed_qty: str, cumulative_quote_qty: str, fills=None) -> OrderResult:
    return OrderResult(
        order_id=1,
        client_order_id="ta-abc",
        status=status,
        executed_qty=Decimal(executed_qty),
        cumulative_quote_qty=Decimal(cumulative_quote_qty),
        transact_time_ms=0,
        fills=fills or [],
        raw={},
    )


def _flat_portfolio(quote=Decimal(100)) -> PortfolioState:
    return PortfolioState.initial(quote)


def _long_portfolio(quote=Decimal(50), base=Decimal("0.001"), avg_price=Decimal(50000)) -> PortfolioState:
    return PortfolioState(
        quote_balance=quote,
        base_balance=base,
        position_side=PositionSide.LONG,
        avg_entry_price=avg_price,
        realized_pnl_quote=Decimal(0),
    )


def _apply(order, side, portfolio, applied_qty=Decimal(0), applied_quote=Decimal(0),
           applied_comm_quote=Decimal(0), applied_comm_base=Decimal(0), applied_comm_other=None,
           fallback_fee_pct=Decimal("0.001")):
    return compute_order_application(
        order, side, QUOTE_ASSET, BASE_ASSET, portfolio,
        applied_qty, applied_quote, applied_comm_quote, applied_comm_base,
        applied_comm_other or {}, fallback_fee_pct,
    )


def test_new_status_does_not_modify_portfolio():
    portfolio = _flat_portfolio()
    order = _order("NEW", "0", "0")
    result = _apply(order, "BUY", portfolio)
    assert result.portfolio == portfolio
    assert result.new_applied_executed_qty == Decimal(0)
    assert result.is_terminal is False
    assert result.journal_event_type == "ORDER_OPEN"


def test_filled_buy_applies_full_executed_quantity_never_requested():
    portfolio = _flat_portfolio(Decimal(100))
    fills = [Fill(price=Decimal(50000), qty=Decimal("0.001"), commission=Decimal("0.05"), commission_asset="USDT")]
    order = _order("FILLED", "0.001", "50.0", fills=fills)
    result = _apply(order, "BUY", portfolio)
    assert result.is_terminal is True
    assert result.new_applied_executed_qty == Decimal("0.001")
    assert result.portfolio.base_balance == Decimal("0.001")
    assert result.portfolio.pnl_is_estimated is False  # fully quote-denominated commission
    assert result.journal_event_type == "ORDER_FILLED"


def test_filled_with_zero_executed_qty_never_substitutes_requested_quantity():
    # Pathological but must be handled safely: a FILLED status with executed_qty=0
    # (should not happen in practice, but the dispatcher must never guess).
    portfolio = _flat_portfolio()
    order = _order("FILLED", "0", "0")
    result = _apply(order, "BUY", portfolio)
    assert result.portfolio == portfolio  # unchanged - no quantity was ever substituted
    assert result.new_applied_executed_qty == Decimal(0)


def test_partially_filled_applies_only_confirmed_fill_and_stays_open():
    portfolio = _flat_portfolio(Decimal(100))
    fills = [Fill(price=Decimal(50000), qty=Decimal("0.0005"), commission=Decimal("0.025"), commission_asset="USDT")]
    order = _order("PARTIALLY_FILLED", "0.0005", "25.0", fills=fills)
    result = _apply(order, "BUY", portfolio)
    assert result.is_terminal is False
    assert result.new_applied_executed_qty == Decimal("0.0005")
    assert result.portfolio.base_balance == Decimal("0.0005")
    assert result.journal_event_type == "ORDER_PARTIALLY_FILLED"


def test_partially_filled_second_pass_applies_only_the_new_delta_from_cumulative_fields():
    # First pass already applied 0.0005 @ 50000; the order has since filled
    # more (now 0.0008 total @ an average that makes cumulative_quote=40.0).
    portfolio = apply_buy(PortfolioState.initial(Decimal(100)), Decimal("0.0005"), Decimal(50000), Decimal("0.025"))
    order = _order("PARTIALLY_FILLED", "0.0008", "40.0")  # cumulative, no fills array this time
    result = _apply(order, "BUY", portfolio, applied_qty=Decimal("0.0005"), applied_quote=Decimal("25.0"))
    assert result.new_applied_executed_qty == Decimal("0.0008")
    assert result.portfolio.base_balance == Decimal("0.0008")
    assert result.portfolio.pnl_is_estimated is True  # no fills for the incremental portion -> estimated


def test_decreasing_executed_qty_is_rejected_not_silently_applied():
    portfolio = _flat_portfolio(Decimal(100))
    order = _order("PARTIALLY_FILLED", "0.0003", "15.0")
    with pytest.raises(InconsistentExecutionReportError):
        _apply(order, "BUY", portfolio, applied_qty=Decimal("0.0005"), applied_quote=Decimal("25.0"))


def test_canceled_with_zero_executed_qty_does_not_modify_portfolio():
    portfolio = _flat_portfolio()
    order = _order("CANCELED", "0", "0")
    result = _apply(order, "BUY", portfolio)
    assert result.portfolio == portfolio
    assert result.is_terminal is True
    assert result.journal_event_type == "ORDER_CANCELED"


def test_rejected_with_zero_executed_qty_does_not_modify_portfolio():
    portfolio = _flat_portfolio()
    order = _order("REJECTED", "0", "0")
    result = _apply(order, "BUY", portfolio)
    assert result.portfolio == portfolio
    assert result.is_terminal is True
    assert result.journal_event_type == "ORDER_REJECTED"


def test_expired_with_zero_executed_qty_does_not_modify_portfolio():
    portfolio = _flat_portfolio()
    order = _order("EXPIRED", "0", "0")
    result = _apply(order, "BUY", portfolio)
    assert result.portfolio == portfolio
    assert result.is_terminal is True
    assert result.journal_event_type == "ORDER_EXPIRED"


def test_never_submitted_with_zero_executed_qty_does_not_modify_portfolio():
    # Internal-only status synthesized by startup_reconciliation.py when a
    # NOT_FOUND reconciliation confirms an order never reached the exchange.
    portfolio = _flat_portfolio()
    order = _order("NEVER_SUBMITTED", "0", "0")
    result = _apply(order, "BUY", portfolio)
    assert result.portfolio == portfolio
    assert result.is_terminal is True
    assert result.journal_event_type == "ORDER_NEVER_SUBMITTED"


def test_expired_with_partial_execution_applies_only_executed_component():
    portfolio = _flat_portfolio(Decimal(100))
    fills = [Fill(price=Decimal(50000), qty=Decimal("0.0003"), commission=Decimal("0.015"), commission_asset="USDT")]
    order = _order("EXPIRED", "0.0003", "15.0", fills=fills)
    result = _apply(order, "BUY", portfolio)
    assert result.is_terminal is True
    assert result.new_applied_executed_qty == Decimal("0.0003")
    assert result.portfolio.base_balance == Decimal("0.0003")  # only the executed part, not the requested amount


def test_filled_sell_applies_full_quantity_and_reports_realized_pnl_delta():
    portfolio = _long_portfolio(quote=Decimal(50), base=Decimal("0.001"), avg_price=Decimal(50000))
    fills = [Fill(price=Decimal(55000), qty=Decimal("0.001"), commission=Decimal("0.055"), commission_asset="USDT")]
    order = _order("FILLED", "0.001", "55.0", fills=fills)
    result = _apply(order, "SELL", portfolio)
    assert result.portfolio.position_side == PositionSide.FLAT
    assert result.realized_pnl_delta is not None
    assert result.realized_pnl_delta == Decimal("0.001") * (Decimal(55000) - Decimal(50000)) - Decimal("0.055")


def test_base_asset_commission_on_buy_reduces_received_quantity():
    # Finding 4: base-asset commission must reduce the base qty actually
    # credited, not be silently converted into an estimated quote fee.
    portfolio = _flat_portfolio(Decimal(100))
    fills = [Fill(price=Decimal(50000), qty=Decimal("0.001"), commission=Decimal("0.000001"), commission_asset=BASE_ASSET)]
    order = _order("FILLED", "0.001", "50.0", fills=fills)
    result = _apply(order, "BUY", portfolio)
    assert result.portfolio.base_balance == Decimal("0.000999")
    assert result.portfolio.pnl_is_estimated is False  # confirmed fill, exact asset-aware accounting
    assert result.new_applied_commission_base == Decimal("0.000001")


def test_third_asset_commission_is_recorded_but_never_touches_quote_or_base():
    portfolio = _flat_portfolio(Decimal(100))
    fills = [Fill(price=Decimal(50000), qty=Decimal("0.001"), commission=Decimal("0.0000015"), commission_asset="BNB")]
    order = _order("FILLED", "0.001", "50.0", fills=fills)
    result = _apply(order, "BUY", portfolio)
    assert result.portfolio.base_balance == Decimal("0.001")
    assert result.portfolio.quote_balance == Decimal(50)
    assert result.new_applied_commission_other == {"BNB": Decimal("0.0000015")}


def test_no_fills_falls_back_to_estimated_quote_fee():
    portfolio = _flat_portfolio(Decimal(100))
    order = _order("FILLED", "0.001", "50.0")  # no fills array at all
    result = _apply(order, "BUY", portfolio, fallback_fee_pct=Decimal("0.002"))
    assert result.portfolio.pnl_is_estimated is True
    # fallback fee = notional * fallback_fee_pct = 50.0 * 0.002 = 0.1
    assert Decimal(result.journal_payload["commission_quote"]) == Decimal("0.1")
