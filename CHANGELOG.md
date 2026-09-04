# Changelog

All notable changes to this project are documented here.

## [0.1.3] - Read-only Testnet connectivity check

Added `trading-agent --mode testnet testnet-health`: a strictly read-only
diagnostic command for verifying Testnet connectivity and credentials
before relying on `run`. Adds 29 tests (253 -> 282 total).

### Added

- `execution/binance_signing.py`: shared, side-effect-free HMAC signing
  and clock-offset primitives, extracted from `execution/testnet_adapter.py`
  without changing its behavior, so both the order-capable adapter and the
  new read-only client build on the same signing logic without either
  depending on the other.
- `execution/testnet_readonly.py::ReadOnlyTestnetClient`: a Testnet client
  with no `place_market_order` method and no code path capable of issuing
  a POST/PUT/PATCH/DELETE - its one internal request method is hard-wired
  to `requests.Session.get`. Provides signed `get_account_balances()` and
  `get_open_orders()`; public server-time and exchange-info calls reuse
  the existing `BinancePublicMarketDataClient`.
- `execution/testnet_health.py::run_testnet_health_check`: fetches server
  time, synchronizes and reports clock offset, fetches and validates
  BTCUSDT exchange filters, performs one signed account-info GET, reports
  free/locked BTC and USDT balances (a nonzero BTC balance is information,
  not a failure), queries open BTCUSDT orders via GET, and - read-only,
  via `ExecutionStateStore.open_read_only()` - reports whether local
  execution state exists, compares it against Testnet balances only when
  it does, and reports any unresolved local pending orders without
  reconciling them. Fails closed on invalid credentials, excessive clock
  drift, or malformed/incomplete exchange filters. Every detail string is
  scrubbed for signatures and secret values before being stored or
  printed.
- `ExecutionStateStore.open_read_only()`: opens an existing execution-state
  database with SQLite's own `mode=ro` URI flag; returns `None` (creating
  nothing) if the file does not exist.
- `SCHEDULING_DESIGN.md`: a design-only specification (no implementation)
  for the overlap-guard mechanism required before any automatic scheduling
  of `run` is built - a single-instance process lock, a database-backed
  cycle lease with expiry, uniqueness by symbol and candle close time, and
  recovery from a process dying while holding the lease.

### Guarantees added (see SECURITY.md for the full list)

- `testnet-health` has no reference to `place_market_order` anywhere in
  its source or import graph - it does not import
  `execution/testnet_adapter.py` at all - proven at the source level in
  `tests/unit/test_testnet_health.py`, not just behaviorally.
- Never creates a local execution-state database or file that does not
  already exist, and never modifies one that does.
- Never prints API keys, secrets, signed query strings, headers, or
  signatures, in the report, CLI output, or an induced failure.

## [0.1.2] - Second independent review response

A second independent review of the round-1 fixes (commit `fde56d7`) found
seven further safety and correctness defects the round-1 test suite did
not detect - all in the round-1 fixes themselves. All seven were confirmed
and fixed, adding 20 tests (233 -> 253 total). Automatic Testnet entry
(BUY) remains disabled; no live/production execution exists anywhere in
this or any prior version.

### Fixed

- **Exactly-once persistence was not atomic**: round 1 applied a fill's
  outcome across two independently-committed SQLite files/stores
  (`portfolio_store.py`, `pending_orders_store.py`); a crash between those
  two commits could apply a fill's cost without recording it resolved, or
  the reverse, with no way to detect or repair the split afterward.
  Replaced both with a single `persistence/execution_store.py`
  (`ExecutionStateStore`): one SQLite connection, both tables, and one
  explicit `BEGIN IMMEDIATE`/`COMMIT`/`ROLLBACK` transaction
  (`apply_order_result_atomically`) that verifies, applies, and marks
  resolution together or not at all. `tests/unit/test_execution_store.py`
  proves this with fault injection at all three former crash boundaries,
  proving exactly-once-or-safely-pending, never zero-permanently or twice.
  See ARCHITECTURE.md's "Crash recovery" section for the full transaction
  boundary and the two operations still deliberately outside it (the
  exchange call itself, and the journal write).
- **Reconciliation mismatch was a buy-only gate**: an untrusted local
  balance could still be used to size a SELL, on the mistaken reasoning
  that Binance would reject an oversized sell anyway - but this project
  sizes a SELL from the *local* balance before ever asking Binance, so a
  mismatch in either direction (local exceeding exchange, or the reverse)
  was exactly as unsafe there as for a BUY. `reconciliation_blocked` is
  now a universal gate in `risk/engine.py`, checked before the BUY/EXIT
  branch is even reached; `execution/live_runner.py` additionally never
  calls the sell-sizing function at all while blocked, rather than relying
  on the risk gate alone. New tests cover both mismatch directions.
- **Partial-fill deltas were proportional estimates**: computing a fill's
  contribution by splitting a cumulative total proportionally across fills
  is exact only when all fills are seen in one pass - it produces the
  wrong average entry price once an order fills incrementally across
  separate reconciliation observations at different prices. Deltas are now
  computed directly from Binance's own cumulative `executed_qty`/
  `cumulative_quote_qty` fields (which are persisted per pending order),
  and a decrease in either field is rejected rather than silently applied.
  A new test drives two partial fills at different prices through separate
  reconciliation passes and asserts an exact resulting average price.
- **Commission was not asset-aware**: round 1 treated any non-quote-asset
  commission (e.g. Binance's default BTC-denominated fee on a BUY) as
  "unknown" and silently substituted an estimated quote-denominated fee
  while still crediting the full base quantity - both wrong and
  overconfident. Commission is now bucketed by the asset actually charged
  (quote adjusts quote flow, base adjusts base flow, any third asset like
  BNB is recorded separately and never touches quote/base balances); a
  base-asset commission reported on a SELL - which Binance's fee model
  does not support - is rejected rather than guessed at
  (`UnsupportedCommissionError`).
- **Backtest UTC-day ordering was backwards**: a day boundary was detected
  and its counters reset using the CURRENT candle's close price, and only
  AFTER resolving whatever signal had been queued from the previous
  candle - so a trade that executed on the first candle of a new day had
  its realized PnL attributed to (and then discarded from) the day that
  merely queued it, and the new day's starting equity was computed from a
  price already moved by that same day's own price action. Reordered to
  detect/initialize the new day FIRST, using that candle's OPEN price,
  before resolving any pending signal or checking the stop-loss.
  Regression tests reproduce both an EXIT and a losing stop misattributed
  across a day boundary in the old ordering, and confirm the new ordering
  correctly lets the loss block a later same-day trade via
  `max_daily_loss_pct`.
- **Stop-loss sizing ignored costs**: position size was `risk_budget /
  (entry_price - stop_price)` - the bare price gap, ignoring the same
  entry/exit slippage and taker fees every other simulated fill in this
  project pays, so an ordinary (non-gap) stop hit could lose more than the
  configured risk budget. `sizing/position_sizer.py::
  compute_risk_based_buy_quantity` now sizes against the full expected
  cost (price gap plus both legs' slippage and fees); a genuine gap
  through the stop can still exceed the budget, and that limitation is now
  explicitly tested and documented rather than merely implied.
- **Testnet capability was under-described**: "automatic entry is
  disabled" did not make clear that Testnet operation is observational, or
  that SELL exists only to close/recover an already-established, fully
  reconciled position rather than as a general trading path. Made explicit
  in the `run`/`status` CLI output and in README.md, RISK_POLICY.md, and
  SECURITY.md.

## [0.1.1] - Independent review response

An independent review of commit `bd5a49b` found ten safety and
backtesting defects the existing test suite did not detect. All ten were
confirmed and fixed, adding 67 tests (166 -> 233 total). Automatic
Testnet entry (BUY) remains disabled; no live/production execution exists
anywhere in this or any prior version.

### Fixed

- **Order status mishandling** (`execution/live_runner.py`): a NEW order
  (accepted, unfilled) could have the *requested* quantity substituted for
  its zero `executed_qty` and be journaled as `ORDER_FILLED`. Replaced with
  `execution/order_outcome.py`, a single dispatcher handling every Binance
  order status (NEW/PARTIALLY_FILLED/FILLED/CANCELED/REJECTED/EXPIRED)
  distinctly, applying only Binance's own reported executed quantity - the
  requested quantity is never substituted.
- **No crash recovery**: a process crash between submitting an order and
  persisting its outcome was unrecoverable and undetectable. Added a
  durable `pending_orders` table (`persistence/pending_orders_store.py`)
  written *before* the exchange call, and startup reconciliation
  (`execution/startup_reconciliation.py`) that resolves any unresolved
  order - exactly once, via the same status dispatcher - before a new
  signal is ever generated. See ARCHITECTURE.md's "Crash recovery" section
  for the full state machine.
- **Same-close backtest execution**: the backtest generated a signal from
  a candle's close and filled it at that same close - not a realistically
  obtainable fill. The engine now queues a signal at candle `i`'s close
  and resolves it (with adverse slippage and fees) no earlier than candle
  `i+1`'s open; a signal on the final candle is reported as unexecuted.
- **Notional allocation misrepresented as risk**: `max_risk_per_trade_pct`
  was a notional-value cap with a disclaimer, not a real risk figure.
  Investigated Testnet's native OCO/`STOP_LOSS_LIMIT` support and judged it
  not safely implementable in this revision without live-network testing
  this project's environment cannot perform (see RISK_POLICY.md's
  "Protective exits" section for the full reasoning). Implemented a real
  stop-loss in the backtest, sized via `risk_budget / stop_distance`
  (`sizing/position_sizer.py::compute_risk_based_buy_quantity`); disabled
  automatic BUY entry on Testnet entirely pending a verified
  exchange-resident protective order; kept `max_position_pct` as a
  separate, always-on notional ceiling.
- **Balance reconciliation only at cold start, free-only**: now runs every
  Testnet cycle and compares free+locked balances; an unexplained mismatch
  blocks new entries (never exits) rather than being silently absorbed.
- **Fee accounting**: realized PnL only ever subtracted the exit fee, never
  the entry fee, understating losses on every closed trade
  (`portfolio/state.py`). Now nets out both, parses Binance's actual
  per-fill commission when available and quote-denominated, and explicitly
  flags `pnl_is_estimated=True` when it has to fall back to an approximation.
- **Historical data capped at one page**: `fetch-data` could only fetch a
  single up-to-1000-candle request. Added paginated, retrying,
  rate-limit-aware, deduplicating, validating historical fetch
  (`data/historical_fetch.py`) and `--start`/`--end` CLI options suitable
  for downloading multiple years of history.
- **No server-time sync**: signed requests used the raw local clock with no
  drift detection. Added `TestnetBrokerAdapter.sync_time()`, which computes
  a bounded offset from the exchange's own server time (reusing the fetch
  `live_runner` already makes) and fails closed with `ClockDriftError` on
  excessive drift.
- **Timeout-only failure handling**: only `requests.Timeout` triggered
  reconcile-before-retry; a connection reset or DNS failure would propagate
  uncaught. Broadened to `requests.exceptions.RequestException`.
- **"Walk-forward" terminology**: renamed to "chronological holdout
  reporting" throughout, with an explicit statement that this is
  fixed-parameter reporting, not model selection or genuine rolling
  walk-forward re-optimization.

### Known limitations carried forward

- No Testnet-native protective order yet - automatic entry stays disabled
  on Testnet until one is implemented and verified live.
- Fee accounting still falls back to an estimate when per-fill commission
  data is unavailable or non-quote-denominated (now explicitly labeled
  `pnl_is_estimated`, rather than silently approximated as before).
- No continuous/daemon trading loop - the `run` command performs one
  decision cycle per invocation, intended to be driven by an external
  scheduler.

## [0.1.0] - Initial V0.1 prototype

Safe Binance Spot Testnet prototype. BTC/USDT spot, 4-hour candles, long-
or-cash only. Live trading is not implemented in this version.

### Added

- Typed, validated configuration (`config/`) with a two-value `Mode` enum
  (`backtest`, `testnet` - no `live`) and environment-only secrets.
- Read-only market-data client with an explicit host allowlist and no
  order-placing capability; fail-closed candle validation (staleness,
  duplicates, gaps, ordering); SQLite candle storage.
- Causal (no-look-ahead) EMA/SMA indicators and a baseline EMA-crossover
  trend-following strategy returning explicit BUY/EXIT/HOLD signals with
  reason codes and inputs.
- Exchange-filter-aware position sizing (Decimal-exact, rounds down,
  rejects rather than enlarges below-minimum trades) and a pure-function
  portfolio state machine.
- An independent risk engine covering max position size, max risk per
  trade, max daily loss, max drawdown, max trades/day, cooldown after a
  loss, stale-data rejection, duplicate-order prevention, minimum quote
  balance, and consecutive-API-error shutdown; a manual file-based kill
  switch; an independent order validator.
- A Testnet-only broker adapter (hard-coded host, HMAC-SHA256 signing),
  deterministic idempotent client order IDs, timeout/uncertain-order
  reconciliation, and a backtest broker with configurable fees/slippage.
- An append-only journal, a backtest performance-metrics module (return,
  drawdown, volatility, Sharpe/Sortino, win rate, profit factor, exposure,
  turnover, buy-and-hold comparison, low-trade-count warnings), and a
  chronological-holdout-reporting backtest engine with fixed-parameter train/validation/test
  splits.
- A CLI (`trading-agent`) with `config-check`, `fetch-data`, `backtest`,
  `run` (single testnet decision cycle), `status`, and a `kill-switch`
  command group.
- 166 offline tests (no real credentials or network access required) and
  a source-level proof that production Binance endpoints cannot be
  selected.
- Full documentation set: README, ARCHITECTURE, RISK_POLICY, STRATEGY,
  TESTING, SECURITY.

### Fixed

- Default configuration bug found during Phase 6 integration testing:
  `risk.max_risk_per_trade_pct` defaulted to 0.02 while
  `sizing.max_allocation_pct` defaulted to 0.90, causing every
  default-configuration BUY to be silently rejected by the risk engine.
  `max_risk_per_trade_pct` now defaults to 0.90 (documented in
  RISK_POLICY.md as a notional-based cap, since the baseline strategy has
  no stop-loss to measure true per-trade risk against).

### Known limitations

- Order fee for testnet fills is approximated as
  `cumulative_quote_qty * taker_fee_pct` rather than parsed from exact
  per-fill commission (which can be charged in a different asset).
- Cold-start portfolio reconciliation requires the testnet account to be
  flat; it will not guess a cost basis for a pre-existing position.
- No continuous/daemon trading loop - the `run` command performs one
  decision cycle per invocation, intended to be driven by an external
  scheduler.
