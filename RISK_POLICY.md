# Risk Policy

This document explains every configurable risk control in
`config/default.yaml` under `risk:`, why it exists, and how it is enforced
in code (`src/trading_agent/risk/engine.py`).

## Universal gates (block both BUY and EXIT)

These fire regardless of trade direction, because trading on bad data or
during instability is unsafe no matter which way the trade goes.

| Control | Config key | Behavior |
|---|---|---|
| Manual kill switch | n/a (flag file) | Engaging it halts **all** order submission. See "Why exits are also blocked" below. |
| Consecutive API-error shutdown | `max_consecutive_api_errors` | After this many consecutive API errors, no further orders are attempted until investigated. |
| Stale-data rejection | `stale_data_max_age_seconds` | If the most recent completed candle is older than this, refuse to trade - the agent may be looking at a market that has moved on without it. |
| Duplicate-order prevention | n/a (journal + deterministic client order ID) | If an order for this exact signal (symbol + side + candle close time) was already submitted, refuse to submit it again. |

## Buy-only gates (block new risk-taking, never exits)

| Control | Config key | Behavior |
|---|---|---|
| Maximum drawdown | `max_drawdown_pct` | Once equity has fallen this far from its peak, no new positions are opened until a human reviews and (implicitly, by continuing to run) accepts the state. |
| Maximum daily loss | `max_daily_loss_pct` | Once today's realized loss (as a fraction of the day's starting equity) reaches this, no new positions today. |
| Maximum trades per day | `max_trades_per_day` | A hard cap on trade frequency, independent of signal quality. |
| Cooldown after a loss | `cooldown_bars_after_loss` | After a losing trade, wait this many completed candles before considering a new entry. |
| Minimum quote balance | `min_quote_balance` | Never spend the account down below this buffer. |
| Maximum position size | `max_position_pct` | A single position may never represent more than this fraction of equity. |
| Maximum risk per trade | `max_risk_per_trade_pct` | See below - this is not stop-distance risk. |

### Why "maximum risk per trade" is not what it sounds like

The baseline strategy (STRATEGY.md) has no stop-loss price - its only
exit mechanism is the trend-reversal signal itself. A textbook "risk per
trade" (position size scaled so that a stop-loss hit loses exactly X% of
equity) requires a stop distance that simply does not exist here. Rather
than fabricate one, `max_risk_per_trade_pct` is enforced as another cap
on the trade's notional value as a fraction of equity - functionally
similar to `max_position_pct`, and defaulted to the same value. Lower it
independently of `max_position_pct` if you want a stricter per-trade cap;
just understand it is not measuring stop-based risk. A future version
that adds a real stop-loss mechanism should redefine this control against
the actual stop distance.

### Why exits are also blocked by the kill switch (but not by the other buy-only gates)

Portfolio-performance gates (drawdown, daily loss, trade count, cooldown,
minimum balance) never block an EXIT: closing a position reduces risk, and
refusing to let the agent de-risk because it "used up" a daily allowance
would directly contradict capital preservation. The kill switch is
different - it is a deliberately blunt, manually-triggered "stop touching
this account" control. A kill switch that still allows exits under some
conditions is harder to reason about and easier to get subtly wrong; if
you engage it, expect to manage any open position manually via the
Testnet UI. See `src/trading_agent/risk/kill_switch.py`.

## Exchange-filter enforcement (position sizing and order validation)

Because eventual experimental capital may be as little as USD 50,
exchange minimums matter a great deal. `SymbolFilters`
(`src/trading_agent/sizing/exchange_filters.py`) is parsed from a live
`exchangeInfo` response and covers `PRICE_FILTER` (tick size),
`LOT_SIZE`/`MARKET_LOT_SIZE` (step size, min/max quantity), and
`NOTIONAL`/`MIN_NOTIONAL` (minimum order value). Both the position sizer
and the order validator:

- Round price and quantity **down** to the nearest valid tick/step - never up.
- **Reject** a trade whose risk-compliant size rounds down below the
  exchange's minimum, rather than enlarging it to meet that minimum. A
  rejected trade (`BELOW_MIN_NOTIONAL` / `BELOW_MIN_LOT_SIZE`) is a
  correct, expected outcome at very small account sizes - it means the
  configured risk budget for this trade is smaller than the exchange
  will accept, not a bug to work around by increasing size.

The order validator re-derives this compliance independently of the
sizer, immediately before submission, against a freshly fetched
`SymbolFilters` - catching the case where filters changed between sizing
and submission.

## Fail-closed data handling

Missing, stale, duplicated, or out-of-order candle data blocks trading
rather than being patched or estimated (`data/validation.py`). There is
no "best guess and continue" path anywhere in the data pipeline.

## Order retries and duplicates

Client order IDs are deterministic (`execution/client_order_id.py`): a
hash of symbol, side, and the signal candle's close time. This means a
retry after a timeout reuses the exact same ID. Before ever retrying, the
agent queries Binance for that order (`execution/reconciliation.py`) and
retries **only** if Binance positively confirms the order was never
received (error code `-2013`, "Order does not exist"). Any other outcome
- confirmed placed, or an unrecognized error - blocks the retry rather
than risking a duplicate order.

## Kill switch

A file-based flag (`src/trading_agent/risk/kill_switch.py`), checked on
every trade decision. Engage/disengage/check status via:

```bash
trading-agent kill-switch engage --reason "..."
trading-agent kill-switch status
trading-agent kill-switch disengage
```

## What this policy does not claim

None of these controls make the strategy profitable, and none of them are
a substitute for the operator watching what the agent actually does,
especially early on. They exist to bound the damage a bug, a data
anomaly, or an adverse market can do to a small experimental account -
not to guarantee an outcome.
