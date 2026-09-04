# Changelog

All notable changes to this project are documented here.

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
