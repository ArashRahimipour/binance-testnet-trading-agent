from decimal import Decimal

import pytest

from tests.fixtures.exchange_info import make_exchange_info
from trading_agent.backtest.engine import run_backtest
from trading_agent.config.models import AppConfig, StopLossConfig
from trading_agent.data.models import Candle, interval_to_ms
from trading_agent.journal.journal import Journal
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


# --- Round 2 finding #6: cost-aware stop sizing. ---


def _entry_candles_with_stop_candle(low_ordinary_or_gap: str) -> list[Candle]:
    """BUY signal at index 6, entry fills at candle 7's open (90). Candle
    7's own wick is kept safely above the stop so the position survives to
    candle 8 - isolating the stop's behavior from the entry candle's own
    default wick (which would otherwise breach it immediately, same-candle
    as entry). Candle 8 then either touches the stop ordinarily (open
    unchanged, only the low wicks down to it) or gaps below it entirely
    (open itself already under the stop) depending on the mode requested.
    """
    closes = [100, 99, 98, 97, 96, 95, 100, 100, 100 if low_ordinary_or_gap == "ordinary" else 50]
    opens = {7: 90, 8: 100 if low_ordinary_or_gap == "ordinary" else 50}
    lows = {7: 89, 8: 85 if low_ordinary_or_gap == "ordinary" else 40}
    highs = {7: 101, 8: 101 if low_ordinary_or_gap == "ordinary" else 55}
    return [
        Candle(
            symbol="BTCUSDT", interval=INTERVAL,
            open_time_ms=START + i * STEP, close_time_ms=START + i * STEP + STEP - 1,
            open=Decimal(str(opens.get(i, c))), high=Decimal(str(highs.get(i, max(opens.get(i, c), c) + 1))),
            low=Decimal(str(lows.get(i, min(opens.get(i, c), c) - 1))), close=Decimal(str(c)), volume=Decimal(1),
        )
        for i, c in enumerate(closes)
    ]


def test_ordinary_stop_hit_stays_within_risk_budget():
    # An ordinary (non-gap) stop touch - the candle's open is unaffected,
    # only its low wicks down to the stop - must lose no more than
    # equity * max_risk_per_trade_pct, including realistic slippage and fees.
    candles = _entry_candles_with_stop_candle("ordinary")
    config = _config(max_risk_per_trade_pct=0.02, max_position_pct=0.9)
    result = run_backtest(candles, config, _filters())
    assert len(result.trades) == 1
    starting_equity = Decimal(50)  # run_backtest's fixed starting balance
    risk_budget = starting_equity * Decimal(str(config.risk.max_risk_per_trade_pct))
    assert -result.trades[0].pnl_quote <= risk_budget


def test_gap_through_stop_can_exceed_risk_budget():
    # Documented limitation: no position sizing can bound a real gap. Here
    # candle 8 OPENS already below the stop - the fill (worse of stop price
    # or that open) is far below what sizing assumed, and the loss should
    # clearly exceed the risk budget it was sized against.
    candles = _entry_candles_with_stop_candle("gap")
    config = _config(max_risk_per_trade_pct=0.02, max_position_pct=0.9)
    result = run_backtest(candles, config, _filters())
    assert len(result.trades) == 1
    starting_equity = Decimal(50)
    risk_budget = starting_equity * Decimal(str(config.risk.max_risk_per_trade_pct))
    assert -result.trades[0].pnl_quote > risk_budget


# --- Round 2 finding #5: UTC-day boundary ordering. ---
#
# A signal queued on the last candle of one UTC day only fills on the next
# candle's open - which may be the first candle of the NEXT day. The day
# must be detected/initialized (using that candle's OPEN, not its close)
# BEFORE the queued signal is resolved, so the resulting trade's count and
# PnL are attributed to the day it actually executes in. Getting this
# backwards has two independently observable effects, each isolated below:
# resolving before the roll misattributes a crossover EXIT's loss to the
# day it was decided on (and discards it from the new day entirely); and
# computing the new day's starting equity from the candle's CLOSE instead
# of its OPEN materially changes the % impact of a loss that happens
# within that very first candle (e.g. a stop-loss gap). Both are tuned so
# a big-enough daily loss should trip `max_daily_loss_pct` and block a
# later same-day BUY - proving the misattributed/mis-based version would
# wrongly let it through.
#
# All day-boundary candles below use a day-aligned local START (0) with 4h
# candles (exactly 6 per UTC day), so day boundaries fall at indices
# 6, 12, 18, ... - independent of the module-level START used elsewhere in
# this file (which is not day-aligned).

_DAY_STEP = interval_to_ms(INTERVAL)


def _day_aligned_candles(closes: list[float], open_overrides: dict[int, float]) -> list[Candle]:
    opens = [open_overrides.get(i, c) for i, c in enumerate(closes)]
    return [
        Candle(
            symbol="BTCUSDT", interval=INTERVAL,
            open_time_ms=i * _DAY_STEP, close_time_ms=i * _DAY_STEP + _DAY_STEP - 1,
            open=Decimal(str(opens[i])), high=Decimal(str(max(opens[i], c) + 1)),
            low=Decimal(str(min(opens[i], c) - 1)), close=Decimal(str(c)), volume=Decimal(1),
        )
        for i, c in enumerate(closes)
    ]


def _tight_daily_loss_config() -> AppConfig:
    return AppConfig(
        mode="backtest",
        strategy={"ema_fast": 3, "ema_slow": 6},
        backtest={
            "train_fraction": 0.98, "validation_fraction": 0.01, "test_fraction": 0.01,
            "min_trades_for_significance": 20,
        },
        risk={
            "max_daily_loss_pct": 0.05, "max_drawdown_pct": 0.99,
            "max_trades_per_day": 999, "cooldown_bars_after_loss": 0,
        },
    )


def test_exit_resolved_on_first_candle_of_new_utc_day_is_attributed_to_new_day(tmp_path):
    # BUY signal at index 6 fills at candle 7's open; bearish EXIT crossover
    # signal at index 11 (last candle of day 1) fills at candle 12's open -
    # the first candle of day 2 - at a steep loss. stop_distance_pct is
    # widened so the backtest's own stop-loss never fires first, isolating
    # this scenario to the crossover-EXIT resolution-ordering bug alone.
    closes = [100, 99, 98, 97, 96, 95, 100, 105, 110, 115, 115, 60, 60, 50, 40, 30, 90, 90]
    candles = _day_aligned_candles(closes, {7: 100, 12: 10})
    config = _tight_daily_loss_config().model_copy(update={"stop_loss": StopLossConfig(stop_distance_pct=0.9)})

    with Journal(tmp_path / "journal.db") as journal:
        result = run_backtest(candles, config, _filters(), journal)
        risk_decisions = journal.entries_by_type("RISK_DECISION")

    assert len(result.trades) == 1
    assert result.trades[0].pnl_quote < 0  # the EXIT closed at a steep loss
    buy_decisions = [e for e in risk_decisions if e["payload"]["signal_type"] == "buy"]
    assert len(buy_decisions) == 2  # the opening buy, then the later same-day re-entry attempt
    # If the EXIT's loss were discarded by a day-rollover that ran BEFORE
    # resolving it (the bug), this later same-day BUY would be approved.
    assert buy_decisions[-1]["payload"]["approved"] is False
    assert buy_decisions[-1]["payload"]["reason_code"] == "MAX_DAILY_LOSS_SHUTDOWN"


def test_losing_stop_at_first_candle_of_new_utc_day_is_attributed_to_new_day(tmp_path):
    # BUY at index 6 fills at candle 7's open (100). Candle 12 (the first
    # candle of day 2) gaps down through the 5%-below-entry stop (~95.05)
    # intrabar before recovering to close far higher (300) - a case
    # engineered so that basing the new day's starting equity on this
    # candle's CLOSE (post-recovery, ~140 equity) rather than its OPEN
    # (pre-move, ~45 equity) changes whether the stop's loss even reads as
    # exceeding max_daily_loss_pct. A later same-day bullish re-cross (index
    # 16, filling at candle 17's open) must be blocked - proving the loss
    # was correctly attributed to (and sized against) the new day.
    closes = [100, 99, 98, 97, 96, 95, 100, 101, 102, 103, 104, 105, 300, 60, 40, 30, 200, 200]
    candles = _day_aligned_candles(closes, {7: 100, 12: 90})
    candles[12] = Candle(
        symbol="BTCUSDT", interval=INTERVAL,
        open_time_ms=candles[12].open_time_ms, close_time_ms=candles[12].close_time_ms,
        open=Decimal(90), high=Decimal(305), low=Decimal(80), close=Decimal(300), volume=Decimal(1),
    )
    config = _tight_daily_loss_config()

    with Journal(tmp_path / "journal.db") as journal:
        result = run_backtest(candles, config, _filters(), journal)
        risk_decisions = journal.entries_by_type("RISK_DECISION")

    assert len(result.trades) == 1
    assert result.trades[0].pnl_quote < 0  # the stop closed at a loss
    buy_decisions = [e for e in risk_decisions if e["payload"]["signal_type"] == "buy"]
    assert len(buy_decisions) == 2
    # If the day's starting equity were based on this candle's CLOSE (after
    # its own recovery) instead of its OPEN, the same loss would read as a
    # smaller percentage and this later same-day BUY would wrongly pass.
    assert buy_decisions[-1]["payload"]["approved"] is False
    assert buy_decisions[-1]["payload"]["reason_code"] == "MAX_DAILY_LOSS_SHUTDOWN"


def test_risk_budget_sizing_produces_smaller_position_for_smaller_risk_pct():
    candles = _candles(_zigzag_closes())
    low_risk = run_backtest(candles, _config(max_risk_per_trade_pct=0.005), _filters())
    high_risk = run_backtest(candles, _config(max_risk_per_trade_pct=0.05), _filters())
    assert len(low_risk.trades) >= 1
    assert len(high_risk.trades) >= 1
    # Compare the first trade's quantity - a smaller risk budget must never
    # produce a larger position for the same stop distance.
    assert low_risk.trades[0].quantity <= high_risk.trades[0].quantity
