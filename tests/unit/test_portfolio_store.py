from decimal import Decimal

from trading_agent.persistence.portfolio_store import PortfolioStore
from trading_agent.portfolio.state import PortfolioState
from trading_agent.strategy.base import PositionSide


def test_load_returns_none_when_uninitialized(tmp_path):
    with PortfolioStore(tmp_path / "portfolio.db") as store:
        assert store.load("BTCUSDT") is None


def test_save_and_load_round_trip(tmp_path):
    state = PortfolioState(
        quote_balance=Decimal("12.34"),
        base_balance=Decimal("0.00056789"),
        position_side=PositionSide.LONG,
        avg_entry_price=Decimal("50000.5"),
        realized_pnl_quote=Decimal("1.11"),
    )
    with PortfolioStore(tmp_path / "portfolio.db") as store:
        store.save("BTCUSDT", state, updated_at_ms=1_700_000_000_000)
        loaded = store.load("BTCUSDT")
    assert loaded == state


def test_save_overwrites_existing_row(tmp_path):
    with PortfolioStore(tmp_path / "portfolio.db") as store:
        store.save("BTCUSDT", PortfolioState.initial(Decimal(50)), updated_at_ms=1)
        updated = PortfolioState.initial(Decimal(50))
        store.save("BTCUSDT", updated, updated_at_ms=2)
        loaded = store.load("BTCUSDT")
    assert loaded == updated
