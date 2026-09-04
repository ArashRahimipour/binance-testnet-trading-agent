from decimal import Decimal

from trading_agent.persistence.risk_state_store import RiskState, RiskStateStore


def test_load_returns_none_when_uninitialized(tmp_path):
    with RiskStateStore(tmp_path / "risk.db") as store:
        assert store.load("BTCUSDT") is None


def test_save_and_load_round_trip(tmp_path):
    state = RiskState(
        day_key="2026-09-04",
        daily_start_equity=Decimal(50),
        daily_realized_pnl_pct=-0.01,
        trades_today=2,
        peak_equity=Decimal(55),
        cooldown_bars_remaining=1,
        consecutive_api_errors=0,
    )
    with RiskStateStore(tmp_path / "risk.db") as store:
        store.save("BTCUSDT", state)
        loaded = store.load("BTCUSDT")
    assert loaded.day_key == state.day_key
    assert loaded.daily_start_equity == state.daily_start_equity
    assert loaded.trades_today == 2
    assert loaded.peak_equity == Decimal(55)


def test_initial_factory():
    state = RiskState.initial(Decimal(50), "2026-09-04")
    assert state.daily_start_equity == Decimal(50)
    assert state.peak_equity == Decimal(50)
    assert state.trades_today == 0
    assert state.cooldown_bars_remaining == 0
