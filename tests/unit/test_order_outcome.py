"""Comprehensive tests for every Binance order status the dispatcher must
handle distinctly (review Finding 1)."""

from decimal import Decimal

from trading_agent.execution.order_outcome import apply_order_result
from trading_agent.execution.testnet_adapter import Fill, OrderResult
from trading_agent.portfolio.state import PortfolioState
from trading_agent.strategy.base import PositionSide

QUOTE_ASSET = "USDT"


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


def test_new_status_does_not_modify_portfolio():
    portfolio = _flat_portfolio()
    order = _order("NEW", "0", "0")
    outcome = apply_order_result(order, "BUY", QUOTE_ASSET, portfolio, Decimal(0), Decimal("0.001"))
    assert outcome.portfolio == portfolio
    assert outcome.newly_applied_qty == Decimal(0)
    assert outcome.is_terminal is False
    assert outcome.journal_event_type == "ORDER_OPEN"


def test_filled_buy_applies_full_executed_quantity_never_requested():
    portfolio = _flat_portfolio(Decimal(100))
    fills = [Fill(price=Decimal(50000), qty=Decimal("0.001"), commission=Decimal("0.05"), commission_asset="USDT")]
    order = _order("FILLED", "0.001", "50.0", fills=fills)
    outcome = apply_order_result(order, "BUY", QUOTE_ASSET, portfolio, Decimal(0), Decimal("0.001"))
    assert outcome.is_terminal is True
    assert outcome.newly_applied_qty == Decimal("0.001")
    assert outcome.portfolio.base_balance == Decimal("0.001")
    assert outcome.portfolio.pnl_is_estimated is False  # fully quote-denominated commission
    assert outcome.journal_event_type == "ORDER_FILLED"


def test_filled_with_zero_executed_qty_never_substitutes_requested_quantity():
    # Pathological but must be handled safely: a FILLED status with executed_qty=0
    # (should not happen in practice, but the dispatcher must never guess).
    portfolio = _flat_portfolio()
    order = _order("FILLED", "0", "0")
    outcome = apply_order_result(order, "BUY", QUOTE_ASSET, portfolio, Decimal(0), Decimal("0.001"))
    assert outcome.portfolio == portfolio  # unchanged - no quantity was ever substituted
    assert outcome.newly_applied_qty == Decimal(0)


def test_partially_filled_applies_only_confirmed_fill_and_stays_open():
    portfolio = _flat_portfolio(Decimal(100))
    fills = [Fill(price=Decimal(50000), qty=Decimal("0.0005"), commission=Decimal("0.025"), commission_asset="USDT")]
    order = _order("PARTIALLY_FILLED", "0.0005", "25.0", fills=fills)
    outcome = apply_order_result(order, "BUY", QUOTE_ASSET, portfolio, Decimal(0), Decimal("0.001"))
    assert outcome.is_terminal is False
    assert outcome.newly_applied_qty == Decimal("0.0005")
    assert outcome.portfolio.base_balance == Decimal("0.0005")
    assert outcome.journal_event_type == "ORDER_PARTIALLY_FILLED"


def test_partially_filled_second_pass_applies_only_the_new_delta():
    # First pass already applied 0.0005; the order has since filled more (now 0.0008 total).
    portfolio = PortfolioState.initial(Decimal(100))
    from trading_agent.portfolio.state import apply_buy

    portfolio = apply_buy(portfolio, Decimal("0.0005"), Decimal(50000), Decimal("0.025"))
    order = _order("PARTIALLY_FILLED", "0.0008", "40.0")  # cumulative, no fills array this time
    outcome = apply_order_result(order, "BUY", QUOTE_ASSET, portfolio, Decimal("0.0005"), Decimal("0.001"))
    assert outcome.newly_applied_qty == Decimal("0.0003")
    assert outcome.portfolio.base_balance == Decimal("0.0008")
    assert outcome.portfolio.pnl_is_estimated is True  # no fills for the incremental portion -> estimated


def test_canceled_with_zero_executed_qty_does_not_modify_portfolio():
    portfolio = _flat_portfolio()
    order = _order("CANCELED", "0", "0")
    outcome = apply_order_result(order, "BUY", QUOTE_ASSET, portfolio, Decimal(0), Decimal("0.001"))
    assert outcome.portfolio == portfolio
    assert outcome.is_terminal is True
    assert outcome.journal_event_type == "ORDER_CANCELED"


def test_rejected_with_zero_executed_qty_does_not_modify_portfolio():
    portfolio = _flat_portfolio()
    order = _order("REJECTED", "0", "0")
    outcome = apply_order_result(order, "BUY", QUOTE_ASSET, portfolio, Decimal(0), Decimal("0.001"))
    assert outcome.portfolio == portfolio
    assert outcome.is_terminal is True
    assert outcome.journal_event_type == "ORDER_REJECTED"


def test_expired_with_zero_executed_qty_does_not_modify_portfolio():
    portfolio = _flat_portfolio()
    order = _order("EXPIRED", "0", "0")
    outcome = apply_order_result(order, "BUY", QUOTE_ASSET, portfolio, Decimal(0), Decimal("0.001"))
    assert outcome.portfolio == portfolio
    assert outcome.is_terminal is True
    assert outcome.journal_event_type == "ORDER_EXPIRED"


def test_expired_with_partial_execution_applies_only_executed_component():
    portfolio = _flat_portfolio(Decimal(100))
    fills = [Fill(price=Decimal(50000), qty=Decimal("0.0003"), commission=Decimal("0.015"), commission_asset="USDT")]
    order = _order("EXPIRED", "0.0003", "15.0", fills=fills)
    outcome = apply_order_result(order, "BUY", QUOTE_ASSET, portfolio, Decimal(0), Decimal("0.001"))
    assert outcome.is_terminal is True
    assert outcome.newly_applied_qty == Decimal("0.0003")
    assert outcome.portfolio.base_balance == Decimal("0.0003")  # only the executed part, not the requested amount


def test_filled_sell_applies_full_quantity_and_reports_realized_pnl_delta():
    portfolio = _long_portfolio(quote=Decimal(50), base=Decimal("0.001"), avg_price=Decimal(50000))
    fills = [Fill(price=Decimal(55000), qty=Decimal("0.001"), commission=Decimal("0.055"), commission_asset="USDT")]
    order = _order("FILLED", "0.001", "55.0", fills=fills)
    outcome = apply_order_result(order, "SELL", QUOTE_ASSET, portfolio, Decimal(0), Decimal("0.001"))
    assert outcome.portfolio.position_side == PositionSide.FLAT
    assert outcome.realized_pnl_delta is not None
    assert outcome.realized_pnl_delta == Decimal("0.001") * (Decimal(55000) - Decimal(50000)) - Decimal("0.055")


def test_non_quote_commission_marks_pnl_estimated():
    portfolio = _flat_portfolio(Decimal(100))
    fills = [Fill(price=Decimal(50000), qty=Decimal("0.001"), commission=Decimal("0.0000015"), commission_asset="BNB")]
    order = _order("FILLED", "0.001", "50.0", fills=fills)
    outcome = apply_order_result(order, "BUY", QUOTE_ASSET, portfolio, Decimal(0), Decimal("0.001"))
    assert outcome.portfolio.pnl_is_estimated is True
    assert outcome.journal_payload["fee_is_estimated"] is True


def test_no_fills_falls_back_to_estimated_fee():
    portfolio = _flat_portfolio(Decimal(100))
    order = _order("FILLED", "0.001", "50.0")  # no fills array at all
    outcome = apply_order_result(order, "BUY", QUOTE_ASSET, portfolio, Decimal(0), Decimal("0.002"))
    assert outcome.portfolio.pnl_is_estimated is True
    # fallback fee = notional * fallback_fee_pct = 50.0 * 0.002 = 0.1
    assert Decimal(outcome.journal_payload["fee_quote"]) == Decimal("0.1")
