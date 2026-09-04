# Changelog

All notable changes to this project are documented here.

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
  walk-forward backtest engine with chronological train/validation/test
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
