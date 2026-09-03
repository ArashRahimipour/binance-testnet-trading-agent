from decimal import Decimal

import pytest

from trading_agent.portfolio.state import (
    InvalidTransitionError,
    PortfolioState,
    apply_buy,
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
