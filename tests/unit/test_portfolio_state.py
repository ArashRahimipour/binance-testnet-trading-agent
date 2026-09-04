from decimal import Decimal

import pytest

from trading_agent.portfolio.state import (
    InvalidTransitionError,
    PortfolioState,
    UnsupportedCommissionError,
    apply_buy,
    apply_fill_delta,
    apply_sell,
)
from trading_agent.strategy.base import PositionSide


def test_initial_state_is_flat():
    state = PortfolioState.initial(Decimal(50))
    assert state.position_side == PositionSide.FLAT
    assert state.base_balance == Decimal(0)
    assert state.equity(Decimal(50000)) == Decimal(50)


def test_apply_buy_moves_quote_to_base():
    state = PortfolioState.initial(Decimal(50))
    new_state = apply_buy(state, quantity=Decimal("0.0008"), price=Decimal(50000), fee_quote=Decimal("0.04"))
    assert new_state.position_side == PositionSide.LONG
    assert new_state.base_balance == Decimal("0.0008")
    assert new_state.quote_balance == Decimal(50) - (Decimal("0.0008") * Decimal(50000) + Decimal("0.04"))


def test_apply_buy_rejects_double_buy_while_already_long():
    state = apply_buy(
        PortfolioState.initial(Decimal(50)), Decimal("0.0008"), Decimal(50000), Decimal(0)
    )
    with pytest.raises(InvalidTransitionError):
        apply_buy(state, Decimal("0.0001"), Decimal(50000), Decimal(0))


def test_apply_buy_rejects_cost_exceeding_balance():
    state = PortfolioState.initial(Decimal(50))
    with pytest.raises(InvalidTransitionError):
        apply_buy(state, quantity=Decimal(1), price=Decimal(50000), fee_quote=Decimal(0))


def test_apply_sell_rejects_when_flat():
    state = PortfolioState.initial(Decimal(50))
    with pytest.raises(InvalidTransitionError):
        apply_sell(state, quantity=Decimal("0.001"), price=Decimal(50000), fee_quote=Decimal(0))


def test_apply_sell_rejects_quantity_above_held_balance():
    state = apply_buy(
        PortfolioState.initial(Decimal(50)), Decimal("0.0008"), Decimal(50000), Decimal(0)
    )
    with pytest.raises(InvalidTransitionError):
        apply_sell(state, quantity=Decimal("0.001"), price=Decimal(50000), fee_quote=Decimal(0))


def test_apply_sell_full_position_returns_to_flat_and_realizes_pnl():
    state = apply_buy(
        PortfolioState.initial(Decimal(50)), Decimal("0.001"), Decimal(50000), Decimal(0)
    )
    sold = apply_sell(state, quantity=Decimal("0.001"), price=Decimal(55000), fee_quote=Decimal(0))
    assert sold.position_side == PositionSide.FLAT
    assert sold.base_balance == Decimal(0)
    assert sold.avg_entry_price is None
    assert sold.realized_pnl_quote == Decimal("0.001") * (Decimal(55000) - Decimal(50000))


def test_apply_sell_partial_keeps_position_long():
    state = apply_buy(
        PortfolioState.initial(Decimal(100)), Decimal("0.002"), Decimal(50000), Decimal(0)
    )
    sold = apply_sell(state, quantity=Decimal("0.001"), price=Decimal(55000), fee_quote=Decimal(0))
    assert sold.position_side == PositionSide.LONG
    assert sold.base_balance == Decimal("0.001")
    assert sold.avg_entry_price == Decimal(50000)


def test_realized_pnl_nets_out_entry_fee_on_full_close():
    # Buy 0.001 BTC @ 50000 with a 1.0 quote entry fee, sell @ 55000 with a 1.0 exit fee.
    # Gross gain = 0.001 * (55000-50000) = 5.0. Net PnL must subtract BOTH fees: 5.0 - 1.0 - 1.0 = 3.0.
    state = apply_buy(
        PortfolioState.initial(Decimal(100)), Decimal("0.001"), Decimal(50000), Decimal("1.0")
    )
    assert state.open_position_entry_fee == Decimal("1.0")
    sold = apply_sell(state, quantity=Decimal("0.001"), price=Decimal(55000), fee_quote=Decimal("1.0"))
    assert sold.realized_pnl_quote == Decimal("3.0")
    assert sold.open_position_entry_fee == Decimal(0)


def test_realized_pnl_allocates_entry_fee_proportionally_on_partial_close():
    # Buy 0.002 BTC @ 50000 with a 2.0 entry fee. Sell half (0.001) - half the entry fee (1.0)
    # should be allocated to this partial close, the other half stays attributed to the remainder.
    state = apply_buy(
        PortfolioState.initial(Decimal(200)), Decimal("0.002"), Decimal(50000), Decimal("2.0")
    )
    sold = apply_sell(state, quantity=Decimal("0.001"), price=Decimal(50000), fee_quote=Decimal(0))
    # gross gain on the half sold = 0, minus allocated entry fee 1.0 = -1.0
    assert sold.realized_pnl_quote == Decimal("-1.0")
    assert sold.open_position_entry_fee == Decimal("1.0")  # remaining half still attributed
    assert sold.position_side == PositionSide.LONG


def test_pnl_is_estimated_flag_propagates_from_entry_fee():
    state = apply_buy(
        PortfolioState.initial(Decimal(100)),
        Decimal("0.001"),
        Decimal(50000),
        Decimal("1.0"),
        fee_is_estimated=True,
    )
    assert state.pnl_is_estimated is True
    sold = apply_sell(state, quantity=Decimal("0.001"), price=Decimal(55000), fee_quote=Decimal(0))
    assert sold.pnl_is_estimated is False  # position fully closed, flag resets


def test_apply_fill_delta_buy_with_quote_commission():
    state = PortfolioState.initial(Decimal(100))
    result = apply_fill_delta(
        state, "BUY", delta_base_qty=Decimal("0.001"), delta_quote_qty=Decimal(50),
        commission_quote=Decimal("0.05"), commission_base=Decimal(0),
    )
    assert result.base_balance == Decimal("0.001")
    assert result.quote_balance == Decimal(100) - Decimal(50) - Decimal("0.05")
    assert result.avg_entry_price == Decimal(50000)  # unaffected by a quote commission


def test_apply_fill_delta_buy_with_base_commission_reduces_received_quantity():
    state = PortfolioState.initial(Decimal(100))
    result = apply_fill_delta(
        state, "BUY", delta_base_qty=Decimal("0.001"), delta_quote_qty=Decimal(50),
        commission_quote=Decimal(0), commission_base=Decimal("0.00001"),
    )
    net_qty = Decimal("0.001") - Decimal("0.00001")
    assert result.base_balance == net_qty
    assert result.quote_balance == Decimal(100) - Decimal(50)  # full quote paid regardless
    assert result.avg_entry_price == Decimal(50) / net_qty  # higher effective cost per unit held


def test_apply_fill_delta_buy_with_third_asset_commission_does_not_touch_balances():
    state = PortfolioState.initial(Decimal(100))
    result = apply_fill_delta(
        state, "BUY", delta_base_qty=Decimal("0.001"), delta_quote_qty=Decimal(50),
        commission_quote=Decimal(0), commission_base=Decimal(0),
    )
    # Third-asset (e.g. BNB) commission never reaches this function at all -
    # the caller (fees.py/order_outcome.py) keeps it out of quote/base bucket
    # computation entirely, so passing zero for both here IS the correct
    # representation of "commission paid in a third asset".
    assert result.base_balance == Decimal("0.001")
    assert result.quote_balance == Decimal(100) - Decimal(50)


def test_apply_fill_delta_buy_base_commission_consuming_entire_quantity_raises():
    state = PortfolioState.initial(Decimal(100))
    with pytest.raises(InvalidTransitionError):
        apply_fill_delta(
            state, "BUY", delta_base_qty=Decimal("0.001"), delta_quote_qty=Decimal(50),
            commission_quote=Decimal(0), commission_base=Decimal("0.001"),
        )


def test_apply_fill_delta_sell_with_quote_commission():
    state = apply_buy(PortfolioState.initial(Decimal(50)), Decimal("0.001"), Decimal(50000), Decimal(0))
    result = apply_fill_delta(
        state, "SELL", delta_base_qty=Decimal("0.001"), delta_quote_qty=Decimal(55),
        commission_quote=Decimal("0.055"), commission_base=Decimal(0),
    )
    assert result.position_side == PositionSide.FLAT
    assert result.quote_balance == state.quote_balance + Decimal(55) - Decimal("0.055")


def test_apply_fill_delta_sell_with_base_commission_fails_closed():
    state = apply_buy(PortfolioState.initial(Decimal(50)), Decimal("0.001"), Decimal(50000), Decimal(0))
    with pytest.raises(UnsupportedCommissionError):
        apply_fill_delta(
            state, "SELL", delta_base_qty=Decimal("0.001"), delta_quote_qty=Decimal(55),
            commission_quote=Decimal(0), commission_base=Decimal("0.00001"),
        )


def test_apply_fill_delta_buy_increases_existing_position_with_weighted_avg_price():
    state = apply_buy(PortfolioState.initial(Decimal(200)), Decimal("0.001"), Decimal(50000), Decimal(0))
    result = apply_fill_delta(
        state, "BUY", delta_base_qty=Decimal("0.001"), delta_quote_qty=Decimal(60),
        commission_quote=Decimal(0), commission_base=Decimal(0),
    )
    assert result.base_balance == Decimal("0.002")
    # (50000*0.001 + 60000*0.001) / 0.002 = 55000
    assert result.avg_entry_price == Decimal(55000)


def test_pnl_is_estimated_flag_propagates_from_exit_fee():
    state = apply_buy(
        PortfolioState.initial(Decimal(100)), Decimal("0.002"), Decimal(50000), Decimal(0)
    )
    sold = apply_sell(
        state,
        quantity=Decimal("0.001"),
        price=Decimal(55000),
        fee_quote=Decimal(0),
        fee_is_estimated=True,
    )
    assert sold.pnl_is_estimated is True  # partial close, position still open -> flag carries forward
