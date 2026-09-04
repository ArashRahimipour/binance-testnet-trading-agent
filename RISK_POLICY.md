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
| Duplicate-order prevention | n/a (journal + pending-orders store + deterministic client order ID) | If an order for this exact signal (symbol + side + candle close time) was already submitted, refuse to submit it again. |

## Buy-only gates (block new risk-taking, never exits)

| Control | Config key | Behavior |
|---|---|---|
| Maximum drawdown | `max_drawdown_pct` | Once equity has fallen this far from its peak, no new positions are opened until a human reviews and (implicitly, by continuing to run) accepts the state. |
| Maximum daily loss | `max_daily_loss_pct` | Once today's realized loss (as a fraction of the day's starting equity) reaches this, no new positions today. |
| Maximum trades per day | `max_trades_per_day` | A hard cap on trade frequency, independent of signal quality. |
| Cooldown after a loss | `cooldown_bars_after_loss` | After a losing trade, wait this many completed candles before considering a new entry. |
| Minimum quote balance | `min_quote_balance` | Never spend the account down below this buffer. |
| Maximum position size | `max_position_pct` | A single position may never represent more than this fraction of equity - a notional ceiling, independent of risk-budget sizing. |
| Maximum risk per trade | `max_risk_per_trade_pct` | **Backtest only.** Drives risk-budget position sizing directly (see "Protective exits" below); currently unavailable in `testnet` mode. |
| Reconciliation-discrepancy block | n/a (`reconciliation_blocked` in `RiskContext`) | If a pending order from a previous cycle is still unresolved, or local balances don't match the exchange's free+locked balances, new entries are blocked until it's repaired - see "Reconciliation" below. |

### Protective exits: why `max_risk_per_trade_pct` now means what it says (and why Testnet entry is disabled)

An earlier revision of this project enforced `max_risk_per_trade_pct` as
just another notional-value cap, identical in kind to `max_position_pct`,
and said so explicitly rather than pretending otherwise. An independent
review correctly rejected that as not good enough: a cap on position size
is not a measure of risk, and calling it one - even with a disclaimer -
invites treating it as risk management it isn't.

The baseline strategy's only original exit mechanism is the trend-reversal
signal, which has no defined stop distance. Rather than leave that
unaddressed, the backtest now attaches a real protective stop to every
entry: a fixed percentage below the fill price
(`config.stop_loss.stop_distance_pct`), checked against every subsequent
candle's low. Position sizing is `risk_budget / stop_distance`, where
`risk_budget = equity * max_risk_per_trade_pct` - the actual amount lost
if the stop is hit, which is what makes the figure a genuine risk control
rather than a size proxy. A tight stop legitimately justifies a larger
notional position for the same risk budget; `max_position_pct` still caps
notional separately, on top of that. As always, a stop-sized trade that
can't clear the exchange's minimum notional is rejected, never enlarged
to force it through.

**This is backtest-only.** Before shipping it to Testnet, this project
checked whether the Spot Testnet reliably supports a native OCO /
`STOP_LOSS_LIMIT` order - the mechanism that would let a stop live on the
exchange itself rather than depend on this process staying alive to watch
for it. The production API documents OCO support, and the Testnet's own
changelog shows it evolving there too, but that same changelog shows the
OCO endpoint being deprecated and replaced within its tracked history, and
this project has no live network access in the environment it was
developed in to actually exercise it against a running Testnet.
Shipping unexercised order-signing code for the one feature whose entire
job is capital protection, without ever having run it, was judged not
worth the risk. So, in this revision:

- **Automatic entry (BUY) is disabled on Testnet entirely**
  (`execution/live_runner.py`) - a BUY signal there is logged and reported
  as `TESTNET_AUTO_ENTRY_DISABLED_PENDING_PROTECTIVE_ORDER`, never acted on.
- EXIT (closing an existing position) remains enabled on Testnet,
  consistent with the project-wide rule that de-risking is never blocked.
- The stop-loss is fully implemented and tested in the backtest.
- A Testnet-native protective order is a documented future design, not
  yet built: place the entry as today, then immediately place a real OCO
  (`TAKE_PROFIT_LIMIT` + `STOP_LOSS_LIMIT`, or a plain `STOP_LOSS` leg) via
  `POST /api/v3/orderList/oco`, verify its acknowledgment before
  considering the entry "protected," and treat an OCO placement failure as
  a reason to immediately flatten the just-opened position rather than
  leave it unprotected. That verification step - proving the exchange
  actually accepted and is holding the protective order - is exactly the
  live-network exercise this revision does not have the means to run, and
  is the concrete precondition for enabling entry on Testnet in a future
  revision.

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
retry after a submission failure reuses the exact same ID. **Any**
ambiguous network failure counts, not just a timeout - connection resets,
DNS failures, and other `requests.exceptions.RequestException` subtypes
all mean "we don't know whether Binance received this," and are all
treated identically (`execution/live_runner.py` catches the exception
base class, not just `Timeout`). Before ever retrying, the agent queries
Binance for that order (`execution/reconciliation.py`) and retries
**only** if Binance positively confirms the order was never received
(error code `-2013`, "Order does not exist"). Any other outcome -
confirmed placed, still open, or an unrecognized error - blocks the retry
rather than risking a duplicate order.

## Reconciliation

Two checks run on every Testnet cycle, before a new signal is ever
generated (`execution/startup_reconciliation.py`):

1. **Unresolved pending orders.** If a previous run crashed between
   submitting an order and recording its outcome, the durable
   `pending_orders` table (`persistence/pending_orders_store.py`) still
   has a record of it. This is resolved first - see ARCHITECTURE.md's
   "Crash recovery" section for the full state machine. An order still
   open after this blocks new entries this cycle (not exits).
2. **Balance reconciliation.** Local `quote_balance`/`base_balance` are
   compared against the exchange's actual free+locked balances (not just
   free - a resting order legitimately locks funds). An unexplained
   mismatch never gets silently absorbed into local state; it sets
   `reconciliation_blocked=True` for that cycle, which blocks new entries
   through the risk engine's buy-only gates. It never blocks an exit -
   Binance itself enforces the true balance server-side, so a stale local
   figure can make a sell request too small (safe) but never allow one
   that Binance would reject anyway.

## Order status handling

An order's actual reported status decides what happens to local state
(`execution/order_outcome.py`), never an assumption:

| Status | Effect |
|---|---|
| `NEW` | No portfolio change - nothing has executed yet. |
| `PARTIALLY_FILLED` | Only the newly confirmed `executed_qty` delta is applied; the order stays open for the next reconciliation pass. |
| `FILLED` | The full executed quantity is applied; the order is resolved. |
| `CANCELED` / `REJECTED` / `EXPIRED` with zero fill | No portfolio change. |
| `CANCELED` / `REJECTED` / `EXPIRED` with partial fill | Only the executed component is applied - never the originally requested quantity. |

The requested/intended quantity is never substituted for a missing or
zero `executed_qty` - only Binance's own reported numbers are ever applied,
and only the portion not already applied in a previous call, so
re-observing the same order (e.g. during reconciliation after a crash)
never double-counts a fill.

## Fee accounting

Realized P&L nets out **both** the entry and exit fee of a trade
(`portfolio/state.py`) - an earlier revision only subtracted the exit fee,
understating losses and overstating gains on every closed position. For
Testnet fills, the fee itself is parsed from Binance's actual per-fill
`commission`/`commissionAsset` data when available and entirely
quote-denominated; if any fill's commission is charged in a different
asset (e.g. a BNB discount) or no fill data is available at all, the fee
falls back to `cumulative_quote_qty * taker_fee_pct` and the trade/
portfolio is explicitly marked `pnl_is_estimated=True` rather than
presenting a guess as exact accounting (`execution/fees.py`).

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
