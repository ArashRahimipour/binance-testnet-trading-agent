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
| Duplicate-order prevention | n/a (journal + execution store + deterministic client order ID) | If an order for this exact signal (symbol + side + candle close time) was already submitted, refuse to submit it again. |
| Reconciliation-discrepancy block | n/a (`reconciliation_blocked` in `RiskContext`) | If a pending order from a previous cycle is still unresolved, or local balances don't match the exchange's free+locked balances, ALL new order submission is blocked (BUY and EXIT alike) until it's repaired - see "Reconciliation" below. Round 2 correction: this was previously a buy-only gate, which left an EXIT/SELL sizeable from an untrusted local balance. |

## Buy-only gates (block new risk-taking, never exits)

| Control | Config key | Behavior |
|---|---|---|
| Maximum drawdown | `max_drawdown_pct` | Once equity has fallen this far from its peak, no new positions are opened until a human reviews and (implicitly, by continuing to run) accepts the state. |
| Maximum daily loss | `max_daily_loss_pct` | Once today's realized loss (as a fraction of the day's starting equity) reaches this, no new positions today. |
| Maximum trades per day | `max_trades_per_day` | A hard cap on trade frequency, independent of signal quality. |
| Cooldown after a loss | `cooldown_bars_after_loss` | After a losing trade, wait this many completed candles before considering a new entry. |
| Minimum quote balance | `min_quote_balance` | Never spend the account down below this buffer. |
| Maximum position size | `max_position_pct` | A single position may never represent more than this fraction of equity - a notional ceiling, independent of risk-budget sizing. |
| Maximum risk per trade | `max_risk_per_trade_pct` | **Backtest only.** Drives cost-aware risk-budget position sizing directly (see "Protective exits" below); currently unavailable in `testnet` mode. |

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
candle's low. Position sizing is `risk_budget / total_loss_per_unit`, where
`risk_budget = equity * max_risk_per_trade_pct` and `total_loss_per_unit`
is the actual expected cost of an ordinary stop hit - not just the bare
`entry_price - stop_price` gap, but that gap plus the same entry/exit
slippage and taker fees every other simulated fill in this project pays
(round 2 finding #6; see `sizing/position_sizer.py::
compute_risk_based_buy_quantity`). Sizing against the bare price gap alone
understated the true cost and could size a position that lost more than
the configured budget even on an ordinary, non-gapped stop touch. A tight
stop legitimately justifies a larger notional position for the same risk
budget; `max_position_pct` still caps notional separately, on top of that.
As always, a stop-sized trade that can't clear the exchange's minimum
notional is rejected, never enlarged to force it through.

**This still does not, and cannot, bound a gap.** If the market gaps
through the stop before an exit can fill at or near it, the backtest fills
at the worse of the stop price or the next candle's open (modeling this
conservatively rather than hiding it - see `backtest/engine.py`'s
`_execute_stop_exit`), and the realized loss can exceed the risk budget it
was sized against. `tests/unit/test_backtest_engine.py` has one regression
test proving an ordinary stop touch stays within budget and a second
proving a genuine gap can exceed it - this is a disclosed limitation of a
fixed-percentage stop, not something any position sizing formula can fully
prevent.

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

- **Testnet operation is OBSERVATIONAL, not a general trading path**
  (round 2 finding #7). Every cycle evaluates HOLD normally. A BUY signal
  is always suppressed (`execution/live_runner.py`) - logged and reported
  as `TESTNET_AUTO_ENTRY_DISABLED_PENDING_PROTECTIVE_ORDER`, never acted
  on - so **the agent cannot initiate a position on Testnet at all**.
- **SELL exists only to close (or help recover) a position that already
  exists and has been fully reconciled against the exchange** - never to
  open or add to one, and never as a general trading path. This is
  consistent with the project-wide rule that de-risking is never blocked,
  but it is not "trading" in any normal sense: there is no way for this
  agent, running against Testnet today, to ever hold a position it did not
  already hold before the run started (or that a human placed manually).
- The stop-loss is fully implemented and cost-aware, but only in the
  backtest.
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
   `pending_orders` table (`persistence/execution_store.py`'s
   `ExecutionStateStore`) still has a record of it. This is resolved
   first, through the SAME atomic transaction that would apply a fresh
   fill - see ARCHITECTURE.md's "Crash recovery" section for the full
   state machine and transaction boundary. An order still open after this
   blocks ALL new orders this cycle - BUY and EXIT alike.
2. **Balance reconciliation.** Local `quote_balance`/`base_balance` are
   compared against the exchange's actual free+locked balances (not just
   free - a resting order legitimately locks funds). An unexplained
   mismatch never gets silently absorbed into local state; it sets
   `reconciliation_blocked=True` for that cycle, which is a UNIVERSAL gate
   in the risk engine - it blocks ALL new order submission, BUY and EXIT
   alike (`risk/engine.py::_check_universal_gates`).

   **Round 2 correction (finding #2):** an earlier revision checked this
   flag only in the buy-only gates, on the reasoning that "Binance itself
   enforces the true balance server-side, so a stale local figure can only
   make a sell request too small, never one Binance would reject anyway."
   That reasoning is wrong: this project sizes a SELL from the LOCAL
   `base_balance` (`sizing/position_sizer.py::compute_sell_quantity`)
   *before* ever asking Binance, so an untrusted local balance is exactly
   as unsafe for sizing a SELL as a BUY - either direction of mismatch
   (local `base_balance` larger than the exchange's, or smaller) means the
   agent does not actually know how much it holds, and sizing a real order
   from that number regardless of direction is a bug, not a matter of only
   one direction being "safe." `execution/live_runner.py` enforces this
   twice, deliberately redundantly: the risk engine's universal gate would
   reject the resulting intent regardless, but `run_testnet_cycle` also
   never calls `compute_sell_quantity` at all while blocked, so a real
   local balance is never even read for sizing purposes in that state.
   `tests/unit/test_live_runner.py` has explicit regression tests for both
   mismatch directions (local BTC exceeding the exchange's, and the
   exchange's exceeding local), each asserting no order is ever placed.

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
| `NEVER_SUBMITTED` (internal only) | Synthesized by `startup_reconciliation.py` when a confirmed `-2013` NOT_FOUND result closes out a pending order that never reached Binance at all. Not a real Binance status. |

The requested/intended quantity is never substituted for a missing or
zero `executed_qty` - only Binance's own reported numbers are ever applied,
and only the portion not already applied in a previous call, computed
directly from Binance's own cumulative `executed_qty`/`cumulative_quote_qty`
fields (round 2 finding #3) rather than a proportional estimate - which
matters once a single order fills in multiple pieces at different prices
across separate reconciliation passes, since only the exact cumulative
totals give an exact result. Binance's cumulative fields are also checked
to never decrease between observations; if they do, that report is
rejected (`InconsistentExecutionReportError`) rather than silently
applied. So re-observing the same order (e.g. during reconciliation after
a crash) never double-counts a fill, and never under-counts one either.

## Fee accounting

Realized P&L nets out **both** the entry and exit fee of a trade
(`portfolio/state.py`) - an earlier revision only subtracted the exit fee,
understating losses and overstating gains on every closed position. For
Testnet fills, commission is parsed from Binance's actual per-fill
`commission`/`commissionAsset` data and bucketed by the asset it was
actually charged in (`execution/fees.py::compute_commission_buckets`,
`portfolio/state.py::apply_fill_delta` - round 2 finding #4):

- **Quote-asset commission** (e.g. USDT on a BTCUSDT trade) reduces the
  quote proceeds of a SELL / increases the quote cost of a BUY.
- **Base-asset commission** (e.g. BTC on a BUY - Binance's default,
  no-discount case) reduces the base quantity actually received; it is
  never silently converted into an estimated quote-denominated fee while
  still crediting the full base quantity, which is the previous revision's
  bug. On a SELL, Binance's documented fee model charges commission from
  the received quote asset (or BNB), never from the base asset being
  sold - a reported base-asset commission on a SELL therefore has no
  well-defined effect and is rejected (`UnsupportedCommissionError`)
  rather than guessed at.
- **Any third asset** (a BNB fee-discount payment) is recorded separately
  per-asset for the journal and never touches the quote/base balances this
  project tracks - BTC/USDT accounting stays exact regardless.
- Only when NO fill data is available at all (a reconciliation-sourced
  order result - `GET /api/v3/order` never returns a `fills` array, only
  the immediate response to placing an order does) does the fee fall back
  to a `cumulative_quote_qty * taker_fee_pct` estimate, and only then is
  the trade/portfolio marked `pnl_is_estimated=True` - never for a
  confirmed fill in any asset, since asset-aware accounting is exact in
  every one of those cases.

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
