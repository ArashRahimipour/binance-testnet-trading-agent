# Baseline Strategy

**This is an intentionally simple, explainable research baseline, not a
final strategy and not a claim of profitability.** It exists to exercise
the full pipeline (data -> signal -> sizing -> risk -> execution) with
something a human can verify by eye, not to be the best possible trading
idea.

## Rules

Implemented in `src/trading_agent/strategy/trend_baseline.py` as
`EmaCrossoverTrendStrategy`. Evaluated only on **completed** 4-hour
candles (never a still-forming candle):

- **BUY** when the fast EMA crosses from at-or-below the slow EMA to
  strictly above it, and the agent is not already long.
- **EXIT** when the fast EMA crosses from at-or-above the slow EMA to
  strictly below it, and the agent currently holds a position.
- **HOLD** otherwise - including when a crossover condition is met but
  acting on it would mean buying while already long, or selling while
  already flat (there is nothing to sell).

Default parameters (`config/default.yaml`, under `strategy:`):

```yaml
strategy:
  name: ema_crossover_trend
  ema_fast: 20
  ema_slow: 50
```

Both periods are declared in configuration, not hard-coded, and
`ema_fast` must be strictly less than `ema_slow` (enforced by
`config/models.py`).

## Every signal carries a reason and its inputs

`Signal` (`src/trading_agent/strategy/base.py`) always includes a
`reason_code` (e.g. `BULLISH_EMA_CROSSOVER`, `HOLD_ALREADY_LONG`,
`HOLD_NO_CROSSOVER`) and the exact numeric inputs behind the decision
(both EMA values, previous and current, the close price, the position
state). Nothing about a signal is opaque.

## How look-ahead bias is prevented

Two separate mechanisms, at two different layers:

1. **Data layer**: `data/ingestion.py` filters out any candle whose close
   time is not strictly before the exchange's own server time. The
   strategy never receives a still-forming candle in the first place.
2. **Indicator layer**: EMA is computed with `adjust=False`
   (`indicators/moving_averages.py`), making it a strictly causal
   recursive filter - `EMA[k]` is a function only of `price[0..k]`,
   never of anything later. `tests/unit/test_indicators.py::test_ema_is_causal`
   and `tests/unit/test_strategy_trend_baseline.py::test_no_lookahead_*`
   verify this directly: computing a signal on a truncated series and on
   the full series truncated to the same point produces identical results.

The backtest engine (`backtest/engine.py`) reinforces this structurally:
at step `i` it only ever passes `candles[:i+1]` to the strategy.

## Execution timing: no same-close fills

A signal detected from candle `i`'s close cannot realistically be filled
at that same close price in the same instant - you cannot react to a
candle closing and get a fill before the next one opens. So the backtest
queues an actionable signal at the end of processing candle `i` and only
resolves it (sizing, risk-checking, validating, and filling with adverse
slippage and fees) at candle `i+1`'s open. A signal generated on the very
last candle in a series has no `i+1` to resolve against and is reported
as unexecuted in `BacktestResult.unexecuted_final_signal` and the run's
warnings - never silently filled or dropped.
`tests/unit/test_backtest_engine.py::test_changing_next_candle_open_changes_the_fill_price`
and `::test_no_trade_fills_at_the_signal_candles_own_close` verify this
directly.

## Protective stop-loss (backtest only)

Every backtest entry carries a stop price a fixed percentage below its
fill price (`config.stop_loss.stop_distance_pct`), checked against every
subsequent candle's low; a breach closes the full position intrabar at
the worse of the stop price or that candle's open (modeling gap risk).
Position size is `risk_budget / total_loss_per_unit`, where
`total_loss_per_unit` includes entry/exit slippage and fees, not just the
bare entry-to-stop price gap - so an ordinary (non-gap) stop hit stays
within the risk budget, though a real gap through the stop can still
exceed it - see RISK_POLICY.md's "Protective exits" section for the full
rationale and for why this is not yet live on Testnet.

## Fees and slippage in simulation

`BacktestBroker` (`execution/backtest_broker.py`) applies a configurable
taker fee and slippage to every simulated fill, and slippage always works
**against** the trader (buys fill higher, sells fill lower than the
reference price) - the backtest is never allowed to look better than a
conservative real execution would.

## Guards against double-buying and overselling

- The strategy itself returns HOLD (not BUY) when a bullish crossover
  occurs while already long, and HOLD (not EXIT) when a bearish crossover
  occurs while already flat.
- The position sizer never sizes a sell above the quantity actually held
  (`sizing/position_sizer.py::compute_sell_quantity` rounds down and
  rejects any request to sell more than is available).
- `portfolio/state.py::apply_buy`/`apply_sell` raise
  `InvalidTransitionError` if a buy is attempted while already long, or a
  sell for more than the held balance is attempted - a second, structural
  line of defense independent of the strategy's own logic.

## What this strategy is not

- It is not optimized, curve-fit, or selected because it produced the
  best historical return - see `backtest/engine.py`'s docstring and
  RISK_POLICY.md.
- Its only *signal-driven* exit is the trend-reversal crossover; the
  backtest also attaches a protective stop-loss (see above), but that
  stop is not yet live on Testnet - see RISK_POLICY.md's "Protective
  exits" section.
- It is long-or-cash only, matching the project's V0.1 constraints - no
  shorting, no leverage, no margin.
