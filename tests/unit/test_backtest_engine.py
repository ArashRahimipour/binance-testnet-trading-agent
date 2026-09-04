from decimal import Decimal

import pytest

from tests.fixtures.exchange_info import make_exchange_info
from trading_agent.backtest.engine import run_backtest
from trading_agent.config.models import AppConfig
from trading_agent.data.models import Candle, interval_to_ms
from trading_agent.sizing.exchange_filters import SymbolFilters

INTERVAL = "4h"
STEP = interval_to_ms(INTERVAL)
START = 1_700_000_000_000


def _zigzag_closes(segment_len: int = 15, num_segments: int = 8, base: float = 30000.0) -> list[float]:
    closes: list[float] = []
    price = base
    direction = 1
    for _ in range(num_segments):
        for _ in range(segment_len):
            price += direction * 20.0
            closes.append(price)
        direction *= -1
    return closes


def _candles(closes: list[float]) -> list[Candle]:
    return [
        Candle(
            symbol="BTCUSDT",
            interval=INTERVAL,
            open_time_ms=START + i * STEP,
            close_time_ms=START + i * STEP + STEP - 1,
            open=Decimal(str(close)),
            high=Decimal(str(close + 5)),
            low=Decimal(str(close - 5)),
            close=Decimal(str(close)),
            volume=Decimal(1),
        )
        for i, close in enumerate(closes)
    ]


def _config(**risk_overrides) -> AppConfig:
    return AppConfig(
        mode="backtest",
        strategy={"ema_fast": 3, "ema_slow": 6},
        backtest={
            "train_fraction": 0.5,
            "validation_fraction": 0.25,
            "test_fraction": 0.25,
            "min_trades_for_significance": 20,
        },
        risk=risk_overrides,
    )


def _filters() -> SymbolFilters:
    return SymbolFilters.from_exchange_info(make_exchange_info(min_notional="1"))


def test_backtest_runs_and_produces_equity_curve():
    candles = _candles(_zigzag_closes())
    config = _config()
    result = run_backtest(candles, config, _filters())
    assert len(result.equity_curve) == len(candles) - config.strategy.ema_slow
    assert set(result.reports.keys()) == {"train", "validation", "test", "overall"}


def test_backtest_produces_at_least_one_trade_on_zigzag():
    candles = _candles(_zigzag_closes())
    config = _config()
    result = run_backtest(candles, config, _filters())
    assert result.reports["overall"].trade_count >= 1
    assert len(result.trades) == result.reports["overall"].trade_count


def test_low_trade_count_warning_present_for_small_series():
    candles = _candles(_zigzag_closes(segment_len=10, num_segments=4))
    config = _config()
    result = run_backtest(candles, config, _filters())
    assert result.reports["overall"].low_trade_count_warning is True
    assert any("below the configured significance threshold" in w for w in result.warnings)


def test_backtest_rejects_insufficient_candles():
    config = _config()
    too_few = _candles(_zigzag_closes(segment_len=2, num_segments=2))  # 4 candles < ema_slow+1=7
    with pytest.raises(ValueError):
        run_backtest(too_few, config, _filters())


def test_position_never_goes_negative_and_no_double_buy():
    candles = _candles(_zigzag_closes())
    config = _config()
    result = run_backtest(candles, config, _filters())
    for trade in result.trades:
        assert trade.quantity > 0
        assert trade.exit_time_ms > trade.entry_time_ms


def test_drawdown_shutdown_reduces_trades_when_very_strict():
    candles = _candles(_zigzag_closes())
    lenient = run_backtest(candles, _config(max_drawdown_pct=0.99), _filters())
    strict = run_backtest(candles, _config(max_drawdown_pct=0.0001), _filters())
    assert strict.reports["overall"].trade_count <= lenient.reports["overall"].trade_count
