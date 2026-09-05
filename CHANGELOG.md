# Changelog

All notable changes to this project are documented here.

## [0.2.1] - Pre-real-evaluation code review correction

A code review of commit `85faa70` (the 0.2.0 research phase) found six
pre-evaluation design issues. This release fixes all six BEFORE the first
real candidate evaluation against the actual historical database, so the
corrections are not influenced by any result - **no real candidate result
was inspected before or during this release.** Adds 18 tests (419 -> 437
total). No PR was opened, no Testnet connection was made, and no order was
placed as part of this work.

### Fixed

- **Family A (`research/candidates/trend_regime.py`) was structurally
  over-filtered**: the old rule required the EMA separation/ATR threshold
  to already be satisfied on the SAME candle as the bullish crossover -
  but separation is normally near zero exactly at a crossover, so the
  filter could reject almost every real trend. Replaced with a causal,
  finite-state rule with no persisted mutable state (derived fresh from
  price history every call, since the same strategy instance is reused
  across independent evaluation blocks): a bullish trend STATE begins the
  moment fast EMA > slow EMA; while flat, entry fires only once the
  normalized EMA separation reaches the declared threshold - which may
  happen on a LATER candle than the crossover, never the same one only;
  at most one entry per bullish cycle even if the position is closed
  mid-cycle by the engine's own stop-loss; a bearish crossover always
  exits and resets the cycle. A1/A2/A3's parameters were NOT retuned -
  exactly the same three configurations, corrected logic only.
- **Renamed "walk-forward" to `blocked_chronological_evaluation`**
  (`research/walk_forward.py` -> `research/blocked_chronological_evaluation.py`,
  `FoldResult`/`FoldSpec`/`CandidateWalkForwardResult` -> `BlockResult`/
  `BlockSpec`/`CandidateBlockedChronologicalResult`, `fold_count` ->
  `block_count`, throughout modules, tests, CLI output, and docs): the
  prior name and its expanding-window terminology falsely implied
  per-window fitting or optimization. Every candidate's parameters are
  fixed in `candidate_registry.py` before this module ever runs and stay
  identical across every block - nothing is fitted, trained, or selected
  per block. What actually happens is a robustness check: the same one
  fixed candidate is independently re-run, unchanged, over successive
  non-overlapping chronological blocks of the same pre-cutoff data.
- **Reproducible candidate freezing with fail-closed fingerprints**
  (`research/fingerprint.py`, new; `research/freeze.py`, extended):
  `FrozenCandidateRecord` now also carries a deterministic strategy
  implementation fingerprint (hash of the candidate's own source module
  plus the shared causal indicator/strategy-contract modules it depends
  on), a candidate-registry fingerprint, a relevant config
  snapshot/fingerprint (interval, starting equity, fees, slippage,
  sizing, stop-loss, every risk limit), a symbol/exchange-filter
  snapshot/fingerprint, a best-effort source commit hash, the research
  cutoff and freeze boundary, and a scorecard-result fingerprint.
  `assert_frozen_candidate_matches_current_implementation` recomputes
  every fingerprint fresh and FAILS CLOSED
  (`FrozenCandidateImplementationDriftError`) the moment any one no
  longer matches - a frozen candidate is never silently reused once its
  implementation or configuration has drifted. `save_frozen_candidate`
  itself refuses (`FrozenCandidateVersionConflict`) to overwrite an
  existing frozen file with different fingerprints; the sanctioned
  response is `new_candidate_version_migration_id`, which mints a new
  candidate id rather than editing the old frozen record. No secrets or
  API credentials are ever fingerprinted or snapshotted - `AppConfig` has
  no field for them at all.
- **Strengthened, realized-PnL-only scorecard** (`research/scorecard.py`):
  survivor status is now scored on REALIZED closed-trade PnL only
  (normalized by each block's own starting equity), never on
  marked-to-market total return - an unfinished open position can no
  longer by itself make a block, or a candidate, pass. Thresholds
  strengthened and made explicit: >= 30 total closed trades across >= 4
  evaluated blocks, positive median block realized return, positive
  aggregate realized PnL, no materially negative block (worst block
  realized return >= -10%) even with a positive aggregate, max drawdown
  <= 15%, best-trade contribution <= 50% of a POSITIVE block's PnL only
  (an undefined ratio never auto-passes; a losing block's ratio is never
  counted as a dependence signal), and >= 60% of blocks with positive
  realized PnL. An explicit benchmark comparison (strategy return minus
  buy-and-hold, every block, plus the median across blocks) is now always
  reported - visibly, but never as a pass/fail input; beating buy-and-hold
  in every bullish block is never required, since absolute profitability
  and drawdown control remain the primary bar.
- **Cutoff boundary honesty** (`research/cutoff.py`): documented explicitly
  that `assert_pre_cutoff` is a plain runtime check, not an access-control
  mechanism, and that the shared low-level `run_segment` primitive is
  deliberately generic and NOT itself a security boundary (it is also the
  correct tool for legitimately replaying the already-consumed period for
  the frozen baseline). A new source-scanning architectural test
  (`tests/unit/test_research_cutoff.py`) proves, from the real source of
  the real functions, that every official candidate-scoring entry point
  (`run_candidate_blocked_chronological_evaluation`, the `research-backtest`
  CLI command) calls `assert_pre_cutoff`, and that `run_segment` does not.

## [0.2.0] - Leakage-resistant strategy-development research phase

The v0.1 `ema_crossover_v0_1_rejected` baseline was formally REJECTED
against its observed test window (headline return -1.22%, closed-trade
bootstrap mean -9.67% [95% CI -21.65%/+3.75%], max drawdown 14.07%, max
consecutive losses 7). Per that verdict, this release does not repair or
tune the rejected strategy - it builds the next research phase: a new
`trading-agent research-backtest` command with an immutable research
cutoff, three new candidate strategy families, and gap-aware walk-forward
development, entirely additive and never touching the existing `backtest`
command's frozen baseline behavior. Adds 64 tests (351 -> 415 total).

### Added

- **Immutable research cutoff** (`research/cutoff.py::RESEARCH_CUTOFF_MS`,
  2025-05-16T00:00:00Z): `assert_pre_cutoff` rejects any candidate
  development/scoring run touching a candle at or after it; the consumed
  2025-05-16..2026-09-04 period may only reproduce the frozen baseline's
  own report (`research/frozen_baseline.py::reproduce_frozen_baseline_report`,
  which takes no candidate parameter at all).
- **Frozen baseline preservation**: `ema_crossover_v0_1_rejected`
  (`FROZEN_BASELINE_STRATEGY_CONFIG`, ema_fast=20/ema_slow=50) and its
  rejection verdict are preserved exactly for regression comparison,
  ignoring whatever strategy config a caller happens to pass.
- **A narrow strategy interface** (`strategy/base.py::SignalGenerator`,
  `CandidateStrategy`): `backtest/engine.py::run_segment` (made public,
  previously `_run_segment`) is reused unchanged for every candidate, so a
  candidate object has no way to reach or influence the broker, fill
  assumptions, fees, slippage, position sizing, the risk engine, or
  accounting - it only ever returns a `Signal`.
- **Three candidate families** (`research/candidates/`), backed by new
  causal volatility indicators (`indicators/volatility.py`: ATR, rolling
  std/min/max):
  - Trend-following with a volatility/regime filter - an EMA crossover
    gated on the fast/slow spread exceeding an ATR multiple.
  - Breakout with volatility-normalized entry - a Donchian channel
    breakout gated on the breakout distance exceeding an ATR multiple.
  - Conservative mean reversion restricted to non-trending regimes - a
    Bollinger Band dip-buy gated on a separate trend-strength filter,
    which also forces an exit if a real trend emerges while holding.
- **A small, fixed, declared candidate search space**
  (`research/candidate_registry.py`): exactly nine configurations (three
  per family), a literal tuple written before any candidate ever sees
  data - never a grid search, genetic/Bayesian optimizer, or ML selection.
- **Gap-aware, chronological, expanding-window walk-forward development**
  (`research/walk_forward.py`): folds never cross a confirmed historical
  gap; each fold is its own independent call into `run_segment` with a
  warm-up-prefixed slice (warm-up candles never trade), so no position or
  risk state of any kind crosses a fold boundary. Every fold is reported,
  including zero-trade and skipped-too-small folds - never only the best.
- **A rule-based robustness scorecard** (`research/scorecard.py`): fixed,
  pre-declared pass/fail thresholds (positive median fold return, positive
  aggregate realized PnL, no materially negative fold even if the
  aggregate is positive, an acceptable max drawdown, limited dependence on
  the single best trade, stability across folds) produce exactly one of
  `REJECTED` / `RESEARCH_SURVIVOR` / `INSUFFICIENT_EVIDENCE` per candidate
  - never "profitable" or "approved for live trading" - plus an explicit
  multiple-testing warning against selection bias.
- **Freezing a survivor** (`research/freeze.py`): a `RESEARCH_SURVIVOR` is
  automatically frozen (candidate id/params/scorecard + a forward-only
  boundary); `validate_future_paper_test` rejects any attempt to test it
  again using a candle that predates that boundary - previously observed
  data (development or consumed) can never become a new "untouched"
  holdout for it.
- `trading-agent research-backtest`: wires all of the above together,
  printing every candidate's every fold (never hiding unsuccessful
  results) and the full scorecard.

### Unchanged, deliberately

- No EMA period, entry/exit rule, fee, slippage, stop-loss, sizing, or
  risk threshold was changed - this release only adds a new,
  leakage-resistant research pathway alongside the existing, untouched
  `backtest`/`research-backtest`-frozen-baseline behavior.
- This phase implements no Testnet BUY, production execution, scheduling,
  leverage, futures, short selling, forex, machine learning, news trading,
  or copy trading - it is backtest-only research.

## [0.1.6] - Extended backtest diagnostics after the first real holdout result

The first real run of v0.1.5's independent holdout evaluation against the
full stored dataset showed the strategy has not passed the profitability
gate (train +40.39%/16.43% max drawdown with 59 of 83 BUY signals rejected
by `MAX_DRAWDOWN_SHUTDOWN`; validation and test each ended with an open,
marked-to-market BTC position). Per that result, this release performs a
diagnostic and reporting improvement ONLY - no strategy parameter, entry/
exit rule, fee, slippage, stop-loss, sizing, or risk threshold was changed.
Adds 28 tests (323 -> 351 total).

### Added

- `metrics/extended_report.py::ExtendedDiagnosticsReport`, attached to
  every continuous-mode split, gap segment, and independent holdout
  window:
  - **Accounting identity** (`AccountingIdentity`): explicitly verifies
    `ending_equity = ending_cash + ending_base_quantity * final_mark_price`
    for that specific run and reports both sides, rather than assuming it.
  - **PnL breakdown** (`PnlBreakdown`): realized closed-trade PnL,
    unrealized PnL on an ending open position (marked at the final
    available price - no exit fee/slippage/exit price is ever invented for
    it), total mark-to-market PnL, entry/exit fees split apart (new
    `Trade.entry_fee_quote`/`exit_fee_quote` fields), a note on why
    backtest fees are always simulated estimates rather than
    exchange-derived, and total slippage cost (new
    `Trade.entry_reference_price`/`exit_reference_price` fields recording
    the pre-slippage reference price alongside the existing post-slippage
    fill price - additive instrumentation only, no fee/slippage
    calculation changed).
  - **Evidence-backed explanations** (`WindowExplanation`): why a window
    ends with an open position, why executed entries can exceed closed
    trade counts, and why trading stopped - naming the exact risk-gate
    reason code and activation evidence when a latched shutdown explains
    it, or noting the strategy simply produced no further signal when it
    doesn't.
  - **Time-based performance** (`TimeBasedPerformance`): CAGR, a monthly
    return series, % of positive months, the longest underwater period,
    an exposure-adjusted return, and a Calmar ratio - each documented with
    when it is mathematically undefined.
  - **Trade-distribution diagnostics** (`TradeDistribution`): median trade
    return, average/largest winner and loser, the best trade's
    contribution to total PnL and the result excluding it, consecutive
    win/loss streaks, and the holding-period distribution.
  - **A deterministic bootstrap confidence interval**
    (`BootstrapConfidenceInterval`): fixed-seed trade resampling with
    replacement, so the same trades always produce the same interval -
    always paired with a prominent caveat that it does not preserve
    chronological/market-regime ordering and is not evidence of future
    profitability.
  - **Chronological rolling-window diagnostics**
    (`RollingWindowDiagnostics`): fixed-size, non-overlapping groups of
    trades using the same already-configured strategy parameters
    throughout - purely for inspection; never ranks, optimizes, or
    selects among windows or configurations.
  - An **already-consumed warning** on the test window specifically, once
    its results have been reported, against reusing it as an untouched
    final holdout for any future strategy selection.
- `metrics/diagnostics.py`: `RunDiagnostics`/`ShutdownActivation` moved
  here from `backtest/engine.py` (still re-exported from there), plus new
  `OpenPositionInfo` - the still-open position's own entry economics at a
  run's end, carried without ever inventing an exit for it.
- `BacktestResult.extended_reports` (train/validation/test/overall, when a
  single segment ran) and `SegmentReport.extended`/`HoldoutWindowReport.
  extended` (every gap segment and holdout window) expose the above.

### Unchanged, deliberately

- EMA periods, entry/exit rules, fees, slippage, stop-loss, sizing, and
  risk thresholds were not touched - this release only adds diagnostics
  and reporting on top of results the engine already produces.
- The continuous operational simulation and independent holdout evaluation
  from v0.1.5 produce identical fills, fees, and equity curves; only new,
  additional reporting fields were layered on top.

## [0.1.5] - Backtest evaluation/reporting correctness fixes

The first real BTCUSDT backtest against the full stored 2020-01-01 to
2026-09-04 dataset reported 0 trades in both the validation and test
windows. Reading the previous engine's code showed why: train/validation/
test were post-hoc labels (global candle-index fractions) applied to ONE
continuous, unbroken simulation with no risk-state reset at those label
boundaries - so a risk shutdown latched during the "train"-labeled portion
mechanically persisted, unchanged, through everything labeled "validation"
and "test" after it. This release adds concrete, instrumented evidence for
that mechanism instead of leaving it as an inference, and adds a genuinely
independent evaluation mode alongside the preserved continuous simulation.
Adds 12 tests (311 -> 323 total).

### Added

- `backtest/engine.py::RunDiagnostics` (and `ShutdownActivation`): exact
  BUY/EXIT signal counts, executed entries vs. strategy exits vs.
  stop-loss exits (now counted separately via the new `Trade.exit_reason`
  field), every rejected entry grouped by its exact reason code, the
  first and last executed trade timestamps, the maximum-drawdown value
  AND timestamp, and for every risk-gate rejection reason that ever
  activated: the first activation's timestamp/equity/drawdown, how many
  otherwise-valid BUY signals it blocked, whether it remained latched to
  the end of the run, and the ending cash/asset-quantity/marked-to-market
  equity. Attached to every segment (`SegmentReport.diagnostics`) and to
  the top-level `BacktestResult` when exactly one segment ran.
- `backtest/engine.py::run_independent_holdout_evaluation`: a SEPARATE
  evaluation mode, clearly labeled "INDEPENDENT FIXED-PARAMETER HOLDOUT
  EVALUATION - NOT walk-forward optimization" everywhere it is printed.
  Train, validation, and test each run as an independent call to the same
  per-candle simulation loop, each starting from a fresh configured
  `starting_equity` and fresh risk state (peak equity, drawdown,
  cooldowns, day counters all reset). A window may look back at preceding
  candles from its own gap-free segment for indicator warm-up only - those
  warm-up candles never generate a trade, never contribute to the
  reported performance, never reach across a confirmed gap, and no candle
  beyond the window's own end is ever visible to it. A position or
  pending signal open at a window's end is reported but never carried
  into the next window. `HoldoutEvaluationResult`/`HoldoutWindowReport`
  report the exact calendar start/end of every window.
- `config.backtest.starting_equity` (default `50.0`): the quote-currency
  balance every simulation (a continuous segment, or one holdout window)
  starts from - previously a hardcoded `Decimal(50)` inside
  `backtest/engine.py`. `PerformanceReport` now carries both
  `starting_equity` and `ending_equity` explicitly.
- Buy-and-hold comparison rewrite (`metrics/performance.py::
  BuyAndHoldReport`, `compute_buy_and_hold_report`): computed over the
  EXACT same candle range as the report it is attached to (matching
  start/end timestamps, never a different window), starting from the same
  `starting_equity`, with one documented buy-side transaction cost applied
  at entry, its own maximum drawdown, marked to market at the final
  available candle's close, and never bridged across a confirmed gap.
- Gap-segment reporting overhaul: when more than one segment actually ran,
  `BacktestResult.reports` is now empty and each segment instead gets a
  full, independent `PerformanceReport` (`segments[i].performance`) - no
  naive concatenation of independently-restarted equity curves into an
  ordinary percentage return/drawdown/Sharpe/Sortino. The only
  cross-segment aggregate produced is the new, explicitly-labeled
  `AggregateTradeStats`, which contains ONLY figures that remain
  mathematically valid to sum/ratio across independent segments (total
  trades, total realized PnL in quote currency, overall win rate) and
  deliberately has no return/drawdown/Sharpe/Sortino field. The
  single-segment (no-confirmed-gap) case is unchanged: `reports` still
  carries the familiar `"train"/"validation"/"test"/"overall"` keys.

### Unchanged, deliberately

- Strategy parameters (EMA periods, stop distance, fees, slippage, risk
  limits) were not tuned or otherwise changed by this release - only
  evaluation and reporting were corrected, per the explicit constraint
  this round was done under.
- The continuous operational simulation (`run_backtest`) is preserved
  exactly as before for the common no-gap case (same trades, same equity
  curve) - only its diagnostics, starting-equity source, and buy-and-hold
  calculation changed.

## [0.1.4] - Historical-data gap handling

The first real multi-year download (`trading-agent fetch-data --start
2020-01-01 --end 2026-09-04`) failed with `expected 14400000ms between
candles, got 28800000ms between open_time_ms=1582099200000 and
1582128000000` - a genuine, permanent gap in Binance's own historical
record (one missing 4h BTCUSDT candle around 2020-02-19), correctly
detected by the strict validation this project has always used, but with
no way to proceed short of discarding the entire download. Historical
research and live/Testnet trading now have separate validation paths.
Adds 29 tests (282 -> 311 total).

### Added

- `data/gap_detection.py::partition_into_segments`: pure gap detection and
  segmentation. Still rejects a duplicate or out-of-order candle
  immediately, exactly like the original strict validation - only a GAP
  is now recorded (`GapRecord`: expected/previous/next open time,
  missing interval count) and starts a new contiguous segment, instead of
  raising.
- `data/historical_fetch.py::confirm_gaps`: for every detected gap, makes
  ONE focused, narrow-range retry for exactly the suspected missing
  interval(s) before ever concluding the exchange itself is missing the
  data - a gap can also result from this project's own pagination cursor
  math landing at an awkward page boundary, or a transient API response.
  `fetch_historical_range` (the `--start`/`--end` path) and the plain
  `--limit` path both go through this automatically.
- `data/storage.py::CandleStore.store_candles_and_gaps`: persists a
  download's candles and its confirmed-gap manifest (new `candle_gaps`
  table) in ONE transaction - a failure partway through rolls back both;
  neither write is ever committed without the other. Both are idempotent
  (`ON CONFLICT ... DO UPDATE`), so re-running the same download twice
  leaves the database exactly as running it once would.
- `config.backtest.gap_policy` (`"segment"` by default for this
  research-only command, `"reject"` to restore the original strict
  behavior) and `config.backtest.exclude_open_position_segments`
  (default `True`). In `"segment"` mode, `backtest/engine.py::
  run_backtest` splits a gapped series into independent contiguous
  segments, each run as its own fully independent backtest - fresh
  portfolio, fresh indicator warm-up (never reaching back across the
  gap), fresh day/cooldown state. A signal still queued at a non-final
  segment's end is cancelled, never carried into the next segment. A
  position still open at a gap-adjacent segment boundary is marked an
  unresolved research condition - no exit price is ever invented for it
  - and is, by default, excluded from the aggregate trade statistics.
  `BacktestResult` gains `gaps` and `segments` fields, and `warnings`
  explicitly states that results across gaps are not one continuous
  tradable equity history whenever any gap was found.
- `trading-agent fetch-data` now reports `Stored N completed candles with
  M confirmed historical gap(s). No candles were fabricated.` plus one
  line per confirmed gap; `trading-agent backtest` reports segment count,
  gap count, each segment's date range/candle/trade counts and
  exclusion status, and the continuity warning.

### Unchanged, deliberately

- `data/validation.py::validate_candle_sequence` - the ONLY validation
  `execution/live_runner.py` ever calls - was not modified at all, and
  live/Testnet signal generation still rejects any gap, duplicate, or
  out-of-order candle outright. Nothing in `data/gap_detection.py` or
  `data/historical_fetch.py`'s new gap-confirmation logic is imported by
  `live_runner.py` - proven at the source level by
  `tests/unit/test_backtest_engine.py::
  test_live_runner_never_references_gap_segmentation_machinery`.
- No OHLCV value is ever fabricated or interpolated anywhere in this
  change - a confirmed gap is recorded and preserved, never filled.

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
