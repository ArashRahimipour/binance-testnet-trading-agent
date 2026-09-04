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


# --- Review Finding 3: no same-close execution; fill depends on the NEXT candle's open. ---

# closes[6] is a bullish EMA(3,6) crossover (BUY signal); closes[10] is a
# bearish crossover (EXIT signal) - verified against ema() directly.
_BUY_THEN_EXIT_CLOSES = [100, 99, 98, 97, 96, 95, 100, 105, 110, 115, 60, 60]


def _candles_with_controlled_open(closes: list[float], open_overrides: dict[int, float]) -> list[Candle]:
    opens = [open_overrides.get(i, c) for i, c in enumerate(closes)]
    return [
        Candle(
            symbol="BTCUSDT",
            interval=INTERVAL,
            open_time_ms=START + i * STEP,
            close_time_ms=START + i * STEP + STEP - 1,
            open=Decimal(str(opens[i])),
            high=Decimal(str(max(opens[i], closes[i]) + 5)),
            low=Decimal(str(min(opens[i], closes[i]) - 5)),
            close=Decimal(str(closes[i])),
            volume=Decimal(1),
        )
        for i in range(len(closes))
    ]


def test_changing_next_candle_open_changes_the_fill_price():
    # The BUY signal fires using candle 6's close (100), but must fill no
    # earlier than candle 7's open - vary ONLY that open and the recorded
    # entry price must move with it.
    config = _config()
    candles_a = _candles_with_controlled_open(_BUY_THEN_EXIT_CLOSES, {7: 90})
    candles_b = _candles_with_controlled_open(_BUY_THEN_EXIT_CLOSES, {7: 110})

    result_a = run_backtest(candles_a, config, _filters())
    result_b = run_backtest(candles_b, config, _filters())

    assert len(result_a.trades) >= 1
    assert len(result_b.trades) >= 1
    assert result_a.trades[0].entry_price != result_b.trades[0].entry_price
    # Higher next-open must produce a higher (slippage-adjusted) fill.
    assert result_b.trades[0].entry_price > result_a.trades[0].entry_price


def test_no_trade_fills_at_the_signal_candles_own_close():
    # closes[6] == 100 is the signal candle's close - no recorded trade's
    # entry price may ever equal it, regardless of what candle 7's open is.
    config = _config()
    candles = _candles_with_controlled_open(_BUY_THEN_EXIT_CLOSES, {7: 100})
    result = run_backtest(candles, config, _filters())
    assert len(result.trades) >= 1
    signal_candle_close = Decimal(100)
    for trade in result.trades:
        assert trade.entry_price != signal_candle_close


def test_signal_on_final_candle_is_reported_as_unexecuted_not_filled():
    # Truncate right after the BUY signal candle (index 6) - there is no
    # candle 7 to resolve it against.
    config = _config()
    candles = _candles_with_controlled_open(_BUY_THEN_EXIT_CLOSES[:7], {})
    result = run_backtest(candles, config, _filters())
    assert result.trades == []  # never silently filled
    assert result.unexecuted_final_signal is not None
    assert "BUY" in result.unexecuted_final_signal
    assert any("unexecuted" in w for w in result.warnings)


# --- Review Finding 4: protective stop-loss, sized from risk budget / stop distance. ---


def test_stop_loss_closes_position_when_low_breaches_stop():
    # Entry fills at candle 7's open (90); default stop_distance_pct=0.05
    # -> stop = 90 * 0.95 = 85.5. Candle 8 is engineered to gap its low
    # well below that.
    closes = [100, 99, 98, 97, 96, 95, 100, 100, 100]
    candles = _candles_with_controlled_open(closes, {7: 90})
    # Force candle 8's low far below the stop by rebuilding it directly.
    candles[8] = Candle(
        symbol="BTCUSDT", interval=INTERVAL,
        open_time_ms=candles[8].open_time_ms, close_time_ms=candles[8].close_time_ms,
        open=Decimal(100), high=Decimal(101), low=Decimal(70), close=Decimal(100),
        volume=Decimal(1),
    )
    config = _config()
    result = run_backtest(candles, config, _filters())
    assert len(result.trades) == 1
    trade = result.trades[0]
    # Stop exit price must be at or below the stop level (85.5), never above it.
    assert trade.exit_price <= Decimal("85.5")


def test_risk_budget_sizing_produces_smaller_position_for_smaller_risk_pct():
    candles = _candles(_zigzag_closes())
    low_risk = run_backtest(candles, _config(max_risk_per_trade_pct=0.005), _filters())
    high_risk = run_backtest(candles, _config(max_risk_per_trade_pct=0.05), _filters())
    assert len(low_risk.trades) >= 1
    assert len(high_risk.trades) >= 1
    # Compare the first trade's quantity - a smaller risk budget must never
    # produce a larger position for the same stop distance.
    assert low_risk.trades[0].quantity <= high_risk.trades[0].quantity
