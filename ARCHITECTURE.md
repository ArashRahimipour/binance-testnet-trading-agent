# Architecture

## Guiding rule

**The strategy never calls a broker.** Every proposed trade flows through
a fixed pipeline, and each stage can only narrow or reject what came
before it - none can enlarge a trade past what an earlier stage allowed:

```
completed candles
      |
      v
  strategy (Phase 2)              -> Signal(BUY | EXIT | HOLD, reason, inputs)
      |
      v
  position sizer (Phase 3)        -> quantity, rounded DOWN to exchange filters,
      |                               rejected (not enlarged) if below minimums
      v
  risk engine (Phase 4)           -> approve / reject against every configured limit
      |                               (independent of the strategy and the sizer)
      v
  order validator (Phase 4/5)     -> re-derives filter compliance independently,
      |                               immediately before submission
      v
  broker adapter (Phase 5)        -> BacktestBroker (simulated fill) or
                                      TestnetBrokerAdapter (real Testnet order)
```

Every stage writes what it decided and why to the journal (Phase 6), so
the full chain from candle to (non-)order is reconstructable after the
fact.

## Module map

| Module | Responsibility |
|---|---|
| `config/` | Typed, validated settings (`AppConfig`, `Mode`) and secrets (`Secrets`), loaded from YAML + environment |
| `data/` | Candle model, read-only market-data client, completed-candle filtering, fail-closed validation (live/Testnet), gap-tolerant historical fetch with confirmation retries and segmentation (research-only), SQLite candle + gap-manifest storage |
| `indicators/` | Causal EMA/SMA (no look-ahead) |
| `strategy/` | `Signal` contract and the baseline EMA-crossover strategy - pure functions of (candles, position) |
| `sizing/` | Exchange filter parsing/rounding and fixed-fractional position sizing |
| `portfolio/` | Pure-function portfolio state machine (buy/sell transitions) and its SQLite persistence |
| `risk/` | The independent `RiskEngine`, its data contracts, and the kill switch |
| `execution/` | Order validator, idempotent client order IDs, the Testnet-only broker adapter, the strictly read-only Testnet client and health check, shared signing primitives, order-outcome dispatch, timeout/uncertain-order and startup reconciliation, fee computation, the backtest broker, and the single-cycle live runner |
| `persistence/` | The unified `ExecutionStateStore` (portfolio state + pending orders, one atomic transaction boundary - see below) and the live risk-tracking state store |
| `journal/` | Append-only audit trail of every decision |
| `metrics/` | Backtest performance report computation |
| `backtest/` | The backtest engine tying the above together: a continuous operational simulation AND an independent fixed-parameter holdout evaluation - see below |
| `shadow/` | Forward-only, real-market shadow observation of one frozen research candidate (`multitimeframe_breakout_E1_round3`) - reuses `backtest/engine.py::run_segment` unmodified as its simulation core; never places an order - see below |
| `cli/` | The `trading-agent` command-line entry point |
| `logging_setup.py` | Structured JSON logging with mandatory secret redaction |

## The broker abstraction

There is no shared "Broker" base class inherited by both adapters, by
design: `BacktestBroker` and `TestnetBrokerAdapter` have similar shapes
(`simulate_buy`/`simulate_sell` vs. `place_market_order`) but deliberately
different signatures, because a real order and a simulated fill carry
different information (a real order has an exchange order ID and an
uncertain-until-confirmed status; a simulated fill does not). Both are
driven through the same risk engine and order validator, which is what
actually matters for keeping strategy logic broker-agnostic.

A future adapter for a different venue (or, in a much later version, a
different account tier) would be a new class implementing the same shape,
not a configuration option on `TestnetBrokerAdapter` - see SECURITY.md for
why that separation is load-bearing, not just tidiness.

## Shared signing primitives, and the strictly read-only client

`execution/binance_signing.py` holds pure, side-effect-free computation
only - HMAC request signing, clock-offset math, the common
`BinanceApiError`/`ClockDriftError` types, and the `TESTNET_HOST`
constant. It makes no HTTP calls and has no order-placing capability of
any kind. `TestnetBrokerAdapter` (order-capable) and
`execution/testnet_readonly.py::ReadOnlyTestnetClient` (GET-only, used by
`testnet-health` below) both build on these shared primitives, but
**neither imports the other**: this is what lets the read-only client -
and everything built on it - prove it has zero reference to
`place_market_order` anywhere in its own source or import graph, rather
than merely by convention (see `tests/unit/test_testnet_health.py`).
`ReadOnlyTestnetClient`'s one internal request method is hard-wired to
`requests.Session.get`, with no `method` parameter anywhere that could
turn a call into a POST/PUT/PATCH/DELETE - the "GET only" guarantee is
structural, not just a documented intent.

`execution/testnet_health.py` (`trading-agent --mode testnet
testnet-health`) is the CLI-facing orchestration built on
`ReadOnlyTestnetClient`: server time, clock sync, BTCUSDT exchange filter
validation, one signed `/api/v3/account` GET, one `/api/v3/openOrders`
GET, and a read-only report of local execution state (via
`ExecutionStateStore.open_read_only()` - see below) if any exists. Every
detail string it produces is scrubbed for anything shaped like a
signature or the literal secret values before being stored or printed,
and it never calls `str()` on an exception type that could embed a full
signed URL (e.g. a `requests` connection error) - see SECURITY.md for the
complete list of guarantees this command carries.

## Two separate candle-validation paths: live/Testnet vs. historical research

`data/validation.py::validate_candle_sequence` is the ONLY validation
`execution/live_runner.py` ever calls, and it is completely unmodified by
anything below: any gap, duplicate, or out-of-order candle in a live/
Testnet fetch raises immediately and blocks the cycle. Nothing in
`data/gap_detection.py` or `data/historical_fetch.py` is imported by
`live_runner.py` - `tests/unit/test_backtest_engine.py::
test_live_runner_never_references_gap_segmentation_machinery` proves this
at the source level.

A multi-year historical download is a different problem: the exchange's
own record can have a genuine, permanent gap (discovered in production -
see CHANGELOG.md), and discarding an entire multi-year download because
of one missing candle is worse than handling it explicitly. So a second,
deliberately separate path exists for research data only:

1. `data/gap_detection.py::partition_into_segments` - pure, no I/O. Still
   raises immediately on a duplicate or out-of-order candle (never
   tolerated, either path), but a GAP is recorded as a `GapRecord` and
   starts a new contiguous segment instead of raising.
2. `data/historical_fetch.py::confirm_gaps` - for every gap `partition_
   into_segments` finds, makes ONE focused, narrow-range re-query for
   exactly the suspected missing interval(s) before ever concluding the
   exchange itself is missing the data (a gap can also result from this
   project's own pagination cursor math landing awkwardly at a page
   boundary, or a transient API response - never assume the worse
   explanation first). Recovered candles are merged in and the series is
   re-partitioned; whatever gap(s) remain after that are CONFIRMED.
   `fetch_historical_range` (the `--start`/`--end` paginated path) calls
   this automatically; the plain recent-`--limit` path does too.
3. `persistence` (`data/storage.py::CandleStore.store_candles_and_gaps`)
   persists the candles and the confirmed-gap manifest (`candle_gaps`
   table) in ONE transaction - a failure partway through rolls back both,
   never leaving candles committed without their gap record or the
   reverse. Both writes are idempotent (`ON CONFLICT ... DO UPDATE`,
   keyed by symbol/interval/timestamp), so re-running the same download
   twice leaves the database exactly as running it once would.
4. `backtest/engine.py::run_backtest` reads `config.backtest.gap_policy`
   ("segment" by default for this research-only command, "reject" to
   restore the original strict behavior) and, in "segment" mode, runs
   each contiguous segment as its own fully independent backtest - fresh
   portfolio (from `config.backtest.starting_equity`), fresh indicator
   warm-up, fresh day/cooldown state. A signal still queued at a
   non-final segment's end is cancelled, not carried into the next
   segment; a position still open at a gap-adjacent segment boundary is
   marked an unresolved research condition (no exit price is ever
   invented for it) and is, by default, excluded from the aggregate
   trade statistics. When more than one segment actually ran, the equity
   curves are NEVER naively concatenated into one "overall"
   return/drawdown/Sharpe/Sortino - each segment gets its own complete,
   independent `PerformanceReport` (`segments[i].performance`), plus an
   explicitly-labeled `AggregateTradeStats` containing only the
   trade-level figures that remain mathematically valid to sum across
   independently-restarted segments (total trades, total realized PnL,
   overall win rate - never a percentage return or drawdown); see the
   engine's module docstring and `BacktestResult.warnings`.

At no point does anything in this path fabricate, interpolate, or guess
an OHLCV value - a confirmed gap is recorded and preserved, never filled.

### Optional 1-minute-based gap recovery (`data/gap_recovery.py`)

A confirmed gap above is preserved, never filled - but a genuinely
confirmed 1h gap can sometimes still be reconstructed from Binance's own
official 1-minute klines, IF all 60 expected 1-minute candles for that
hour actually exist. `data/gap_recovery.py` is a separate, OPTIONAL tool
for exactly this, deliberately decoupled from the always-automatic
`confirm_gaps` path above (a human decides whether and when to run it,
never automatic):

1. For each missing hour of each confirmed gap, fetch Binance's 1-minute
   klines for exactly `[hour, hour + 1h)`, reusing `data/boundary.py`'s
   same two-layer exclusive-end guarantee `data/historical_fetch.py`
   uses for its own paginated fetches.
2. Classify the hour into exactly one of four outcomes:
   `FULLY_RECOVERABLE` (all 60 present, continuous, hour-aligned, and -
   if Binance's own native 1h kline for that hour is unexpectedly also
   available - matching it), `PARTIALLY_RECOVERABLE` (some but not all
   60), `GENUINE_NO_DATA` (zero found - a real exchange-side outage), or
   `UNRESOLVED` (a validation failure, or a candle at/after the immutable
   research cutoff, which is never even fetched).
3. A `FULLY_RECOVERABLE` hour is reconstructed by aggregating the 60 real
   1-minute candles exactly as Binance's own klines are built:
   `open`=first open, `high`=max high, `low`=min low, `close`=last close,
   `volume`=sum of volumes - never interpolated, never fabricated.
4. `run_gap_forensics` is purely read-only. Only `apply_gap_recovery`
   writes anything, and only ever candles this same run already
   classified `FULLY_RECOVERABLE` - atomically (one `CandleStore.
   store_candles_and_gaps` transaction, using its `stale_gap_expected_
   open_times` parameter to delete the old, now-inaccurate gap row in the
   same transaction) and idempotently (a second pass re-derives and
   re-asserts the identical result).
5. This module never runs a candidate, a signal, or the backtest engine
   - the only candidate-related fact it ever reads is
   `MultiTimeframeBreakoutStrategy().min_required_candles` (a static,
   side-effect-free constant), used purely to answer "how many complete
   Round-3 fixed-duration blocks would exist after recovery" with the
   exact same candle-counting arithmetic `research/
   fixed_duration_evaluation.py` uses - never to score, rank, or alter
   any historical verdict.

`cli/main.py::research-gap-audit` prints this analysis read-only;
`research-gap-recover --confirm` is the only command that stores
anything, and only with that explicit flag.

## Data flow for a single decision cycle (testnet mode)

1. Fetch server time; call `TestnetBrokerAdapter.sync_time()` with it so
   every signed request uses a bounded clock offset rather than the raw
   local clock (fails closed with `ClockDriftError` on excessive drift).
2. Fetch the latest completed candles from the Testnet's public
   market-data endpoints (no API key needed for this) and validate the
   series (fail closed on staleness/gaps/duplicates).
3. Load or cold-start-reconcile portfolio state.
4. **Resolve any pending order left over from a previous - possibly
   crashed - run** (see "Crash recovery" below) BEFORE generating a new
   signal. An order still open after this blocks ALL new orders this
   cycle - BUY and EXIT alike, since neither can be safely sized while an
   earlier order's outcome is still unknown.
5. **Reconcile local balances against the exchange's free+locked
   balances**, every cycle, not just at cold start. A mismatch blocks ALL
   new orders (round 2 correction: an untrusted local balance is exactly
   as unsafe for sizing an EXIT/SELL as a BUY - see RISK_POLICY.md's
   "Reconciliation" section); it never overwrites local state with a guess.
6. Generate a signal from the completed candles only. A BUY signal is
   currently always suppressed on Testnet (see "Protective exits" in
   RISK_POLICY.md); EXIT proceeds.
7. If EXIT: size it, risk-check it, validate it, durably record the
   intent (see below), submit it via the Testnet-only adapter, reconcile
   before any retry on an ambiguous network failure, apply the order's
   *actual* status via `order_outcome.py` (never substituting the
   requested quantity), and update persisted state.
8. Record every step in the journal.

The backtest engine (`backtest/engine.py`) walks a similar pipeline
candle-by-candle over historical data, swapping the broker for
`BacktestBroker` and adding the queued-signal/next-open execution and
stop-loss mechanics described in its module docstring. It exposes two
independent entry points built on the SAME per-candle loop
(`_run_segment`):

- `run_backtest` - the continuous operational simulation: risk state
  (peak equity, drawdown, cooldowns, daily counters) runs uninterrupted
  for as long as a contiguous segment's data allows. `RunDiagnostics`
  (returned per segment, and at the top level when exactly one segment
  ran) makes every rejected entry's exact reason code, every risk
  shutdown's first-activation equity/drawdown/timestamp and whether it
  stayed latched, and the first/last executed trade timestamps directly
  inspectable - this is what lets a claim like "the drawdown shutdown
  explains zero validation-window trades" be demonstrated with evidence
  rather than inferred from the numbers alone.
- `run_independent_holdout_evaluation` - runs train/validation/test as
  three SEPARATE calls to `_run_segment`, each given a fresh
  `starting_equity` and a warm-up-prefixed slice of its own segment (never
  reaching into a different segment across a gap, never seeing a candle
  past its own window's end). Because each window is its own independent
  `_run_segment` call, no risk state of any kind - and no open position or
  pending signal - ever carries from one window into the next. This is
  explicitly labeled "INDEPENDENT FIXED-PARAMETER HOLDOUT EVALUATION - NOT
  walk-forward optimization" everywhere it is printed, since the strategy
  parameters are identical and fixed across all three windows; only the
  starting balance and risk state are reset.

`metrics/extended_report.py` computes a further, purely read-only layer of
diagnostics from a window's already-produced trades/equity curve - never
re-simulating or tuning anything: an explicit accounting-identity check
(`ending_equity = ending_cash + ending_base_quantity * final_mark_price`),
a PnL breakdown (realized/unrealized/total, entry/exit fees, slippage cost,
never inventing an exit for a still-open position), evidence-backed
explanations for an ending open position / entries exceeding closed trades
/ why trading stopped, time-based performance (CAGR, monthly returns,
longest underwater period, exposure-adjusted return, Calmar ratio),
trade-distribution statistics, a deterministic fixed-seed bootstrap
confidence interval (with a permanent caveat that it does not preserve
market-regime ordering), and chronological rolling-window diagnostics that
never rank or select a configuration. `metrics/diagnostics.py` holds
`RunDiagnostics`/`ShutdownActivation`/`OpenPositionInfo` - split out from
`backtest/engine.py` specifically so `extended_report.py` can depend on
them without a circular import.

## Research phase (`research/`): leakage-resistant candidate development

`backtest/engine.py::run_segment` was made public (previously `_run_segment`)
specifically so this package can reuse it unchanged - every candidate goes
through the identical broker/fill/fee/slippage/sizing/risk-engine/
accounting path the frozen baseline uses, via the narrow
`strategy/base.py::SignalGenerator` protocol (`generate_signal(candles,
current_position) -> Signal`, nothing else) - a candidate has no way to
reach or influence any of that.

- `research/cutoff.py` - `RESEARCH_CUTOFF_MS` (2025-05-16T00:00:00Z,
  immutable) and `assert_pre_cutoff`, called at the entry point of every
  development/scoring function so a caller mistake can never silently
  score a candidate on already-observed data.
- `research/frozen_baseline.py` - `ema_crossover_v0_1_rejected`, frozen
  exactly as evaluated when rejected; `reproduce_frozen_baseline_report`
  is the only function permitted to touch the consumed 2025-05-16..
  2026-09-04 period, and takes no candidate parameter at all.
- `research/candidates/` - three families (trend+regime filter,
  volatility-normalized breakout, conservative mean reversion restricted
  to non-trending regimes), each a `SignalGenerator` plus its own
  `min_required_candles`. `indicators/volatility.py` (ATR, rolling std/
  min/max, all causal) backs all three.
- `research/candidate_registry.py` - exactly nine configurations (three
  per family), a literal tuple declared before any evaluation runs - never
  a grid search, optimizer, or ML selection.
- `research/blocked_chronological_evaluation.py` - renamed from
  "walk_forward.py" as a pre-real-evaluation code review correction: this
  is NOT expanding-window walk-forward optimization, since nothing here
  fits, trains, or re-selects anything per block - every candidate's
  parameters are fixed in `candidate_registry.py` before this module ever
  runs and stay identical across every block. Gap-aware
  (`data/gap_detection.py::partition_into_segments` - a block never
  crosses a confirmed gap), chronological, non-overlapping blocks per
  segment. Each block is its own independent `run_segment` call with a
  warm-up-prefixed slice (the exact mechanism
  `run_independent_holdout_evaluation` already proved), so no position or
  risk state of any kind crosses a block boundary. Every block is
  appended to the result, including skipped and zero-trade ones - nothing
  is ever trimmed to "the best block." Only the official entry points
  (this module's `run_candidate_blocked_chronological_evaluation` and the
  `research-backtest` CLI command) are required to fail closed at the
  cutoff - the shared low-level `run_segment` primitive is generic and
  accepts any candles given to it, so it is NOT itself a security
  boundary (see `research/cutoff.py`'s own docstring).
- `research/scorecard.py` - a fixed, pre-declared, rule-based pass/fail
  test (never a ranking) producing exactly one of `REJECTED` /
  `RESEARCH_SURVIVOR` / `INSUFFICIENT_EVIDENCE` per candidate, plus a
  multiple-testing warning sized to the actual candidate count. Scored on
  REALIZED closed-trade PnL only (normalized by each block's own starting
  equity), never on marked-to-market total return, so an unfinished open
  position can never by itself make a block - or a candidate - pass;
  marked-to-market return and its excess over buy-and-hold are still
  reported per block for visibility only.
- `research/post_mortem.py` - a READ-ONLY, deterministic post-mortem report
  over an already-completed blocked chronological evaluation result (the
  `research-postmortem` CLI command). Runs no new simulation and touches
  no database; it is pure aggregation math over `Trade`/`RiskRewardDiagnostics`
  data a run already produced. Per candidate: trade counts/win rate, PnL
  statistics (including expected value in quote/%/R-multiples and profit
  factor), an exit-reason breakdown (adding a gap-through-stop subset of
  stop-loss), the realized R-multiple distribution, planned-vs-realized
  R/R, fee/slippage totals, best-trade/best-3/best-5% exclusion analysis
  (reported specifically for breakout-family candidates), PnL
  concentration, chronological stability (per-block, per-year, streaks,
  underwater period, half-split), and the risk/reward policy's own
  rejection/1%-compliance rollup. Per-trade R-multiples require each
  closed trade's own planned quote risk, recovered by correlating
  `BlockResult.trades` with `RiskRewardDiagnostics`'s per-approved-entry
  value tuples (both chronological, at most one open trailing entry per
  block - see the module docstring for why this correlation is exact).
  Every aggregate PnL figure carries `EQUITY_ACCOUNTING_NOTE` - a SUM of
  independently-restarted $50 blocks, never a continuous equity curve.
  NEVER ranks or selects a candidate; each ends with exactly one
  evidence-only diagnosis (`broad positive expectancy` /
  `concentrated/fragile positive expectancy` / `negative expectancy` /
  `insufficient evidence`) from one declared, fixed rule.
- `research/fixed_duration_evaluation.py` - a SIBLING block-construction
  method to `blocked_chronological_evaluation.py` (never modified, never
  monkey-patched), added after a post-round-1 finding: the original
  method's `block_count=5` splits every gap-free segment by CANDLE COUNT,
  so a tiny fragment segment gets the same five voting blocks as a
  multi-year dominant one. This module instead gives each segment as many
  COMPLETE, non-overlapping 365-day blocks as its own post-warm-up
  duration allows - zero for a segment too short (`InsufficientDurationFragment`,
  never five negative zero-trade votes), with any sub-365-day tail
  reported separately (`LeftoverPartialWindow`) and excluded from every
  pass/fail calculation. Reuses the SAME `run_segment` primitive, fixed
  1:2 risk/reward policy, and warm-up-never-trades mechanism; `CandidateFixedDurationResult.
  as_blocked_chronological_result()` lets the UNMODIFIED `scorecard.score_candidate`
  be reused verbatim. Its DEFAULT mode anchors each candidate's block 0
  independently at ITS OWN `min_required_candles` - correct only when
  comparing a candidate against itself, WRONG for comparing two DIFFERENT
  candidates on "identical dates" (a real defect caught in commit
  50a5a5b: D1's ~220-candle warm-up vs B1's ~21 anchored them on different
  calendar dates despite a claimed identical-dates comparison). Passing
  an explicit `FixedDurationBlockSchedule` (via `build_fixed_duration_schedule`,
  ANCHORED using the LARGEST warm-up requirement among every candidate it
  will be applied to) instead fixes every candidate's block windows to
  the SAME timestamps - fail-closed on insufficient warm-up, a window
  that can no longer resolve to a complete candle range (e.g. a gap),
  wrong window duration, overlap, or a cutoff touch.
- `research/sensitivity_comparison.py` - for a candidate, reproduces
  `round_1_original_evaluation` (calling the original, unmodified
  evaluation + scorecard functions directly - a label, never a
  recomputation that could differ) alongside a `duration_normalized_sensitivity`
  scorecard from the new fixed-duration blocks. The sensitivity side is
  explicitly non-binding: it never changes an original verdict and never
  retroactively creates a survivor.
- `research/candidates/breakout_regime_gate.py` (`BreakoutWithBullishRegimeGateStrategy`,
  round-2 candidate family D) - an explicitly RESULT-INFORMED hypothesis:
  round 1 showed `breakout_B1` sustained losses in the 2021-2022 regime.
  Delegates B1's own breakout/channel-breakdown signal verbatim (identical
  parameters) and adds exactly one causal gate in front of a would-be BUY:
  close above a 200-period EMA AND that EMA above its own value 20
  completed candles earlier. Read-only instance counters
  (`breakout_signals_evaluated`/`breakout_signals_blocked_by_regime_gate`)
  record gate activity for reporting only, never consulted by the signal
  decision. `research/candidate_registry_round2.py` declares exactly this
  one candidate, `ROUND_NUMBER=2`, `CUMULATIVE_CANDIDATE_CONFIGURATIONS_EXAMINED=10`,
  and a permanent multiple-testing warning disclosing this is not an
  untouched, pre-registered test. `research/round2_report.py` evaluates it
  ONLY on pre-cutoff data via the fixed-duration blocks, scored against
  the SAME unmodified scorecard thresholds, reused with `research/post_mortem.py`
  for its detailed report, and builds ONE shared `FixedDurationBlockSchedule`
  (anchored at `max(d1_min_required, b1_min_required)`) passed to BOTH D1
  and the original `breakout_B1` so both trade IDENTICAL block window
  timestamps despite their very different warm-up requirements - a
  runtime assertion (`_assert_identical_trading_windows`) re-verifies this
  on every call rather than merely trusting it. Never alters B1's own
  round-1 status.
- `research/candidates/multitimeframe_breakout.py` (`MultiTimeframeBreakoutStrategy`,
  round-3 candidate family E) - D1's own round-2 result (OFFICIAL
  REJECTED) motivates a round-3 hypothesis: a weekly regime gate above
  D1/B1's own 4h breakout+EMA200 setup, confirmed on a 1h timeframe.
  Weekly and 4h candles are never fetched or stored separately - they are
  derived, on every call, purely by aggregating the already-causal,
  already gap-validated 1h candles the call receives
  (`_aggregate_completed_buckets`: a bucket is included only with
  EXACTLY its expected hourly candles, contiguously spaced - a gap or
  misalignment simply produces no bucket, never a fabricated partial
  one). A confirmed 4h setup arms a 4-completed-1h-candle entry window
  (identical parameters/arithmetic to `breakout_regime_gate.py`, reusing
  the SAME `ema`/`atr`/`rolling_max` primitives) and expires unrenewed;
  the FIRST 1h candle within the window closing above both the
  triggering breakout level and its own open produces the one entry that
  setup will ever produce (a pure function of price history, generalizing
  `trend_regime.py`'s own one-entry-per-cycle pattern to a 4-candle
  window). Participates in the SAME unmodified risk/reward policy and
  engine execution as every other candidate. `research/
  candidate_registry_round3.py` declares exactly this one candidate,
  `ROUND_NUMBER=3`, `CUMULATIVE_CANDIDATE_CONFIGURATIONS_EXAMINED=11`,
  `REQUIRED_MARKET_INTERVAL="1h"`, and a warning disclosing that round 1's
  nine candidates and round 2's D1 (preserved unchanged) were already
  observed. `research/round3_report.py` + `cli/main.py::research-round3`
  evaluate it ONLY on pre-cutoff 1h data via the SAME unmodified
  fixed-duration blocks and scorecard, overriding the market interval to
  "1h" and reading only `interval="1h"` rows from the candle database
  (multiple intervals of the same symbol already coexist there, keyed by
  `(symbol, interval, open_time_ms)` - see `data/storage.py`).
- `research/fingerprint.py` - deterministic SHA-256 fingerprints for
  reproducible candidate freezing: a strategy-implementation fingerprint
  (the candidate's own source module plus the shared causal
  indicator/strategy-contract modules), a SEPARATE execution-semantics
  fingerprint (backtest engine, simulated broker, portfolio/accounting,
  risk engine, sizing, exchange-filter validation, order validation,
  performance/PnL calculations - everything that can change a simulated
  result once a Signal is emitted, kept apart from the strategy fingerprint
  so a drift report can name which layer changed), a candidate-registry
  fingerprint, a config fingerprint/snapshot (interval, starting equity,
  fees, slippage, sizing, stop-loss, every risk limit - no secrets, since
  `AppConfig` has no field for them), a symbol/exchange-filter fingerprint/
  snapshot, and a scorecard-result fingerprint.
- `research/freeze.py` - freezes a `RESEARCH_SURVIVOR` (candidate id/
  params/scorecard/every fingerprint above + a forward-only boundary +
  candidate version + best-effort source commit hash) and rejects, via
  `validate_future_paper_test`, any attempt to "test" it again on a candle
  that predates that boundary. `assert_frozen_candidate_matches_current_
  implementation` recomputes every fingerprint fresh and FAILS CLOSED
  (`FrozenCandidateImplementationDriftError`) on any mismatch; the
  sanctioned response to an intentional change is
  `new_candidate_version_migration_id` (mint a new id, never edit or
  silently overwrite the old frozen record - `save_frozen_candidate`
  itself refuses to overwrite a differing one).
- `backtest/risk_reward.py` - the fixed minimum 1:2 planned reward/risk
  policy, a user-mandated pre-real-evaluation risk policy wired into
  `backtest/engine.py::run_segment` via `use_fixed_risk_reward_policy`
  (always enabled for every research candidate via
  `run_candidate_blocked_chronological_evaluation`, never for the frozen
  baseline). Every planned BUY is sized so its planned NET loss (after
  entry fee, stop-exit fee, and adverse slippage) is at most 1% of current
  equity; the take-profit target is SOLVED ALGEBRAICALLY (exact Decimal
  arithmetic, no float tolerance) so the planned NET reward comes out to
  exactly the fixed 2.0 minimum before tick rounding - the GROSS
  (pre-cost) ratio is therefore cost-adjusted upward (exactly 2.0 only
  when costs are zero, slightly above otherwise) while the NET ratio
  stays pinned at the fixed floor, and both are reported per entry. The
  target is then rounded to the exchange's price tick in the direction
  that can only increase the reward, and the net ratio is re-checked
  against the rounded target before approval. An entry is rejected
  outright only if a valid tick-aligned target still can't clear 2.0
  net, or if a risk-safe size cannot satisfy the exchange's minimum
  notional/lot-size. After the simulated fill, the plan is rebuilt and
  RE-VALIDATED from the real fill price (both the 1% risk cap and the 2.0
  net floor checked fresh); if either fails, the position is never
  created (fail closed) rather than left open unprotected - realistic
  nonzero fees and slippage do not make this policy reject every entry.
  If both the stop and take-profit are touched within one candle, STOP is
  assumed to occur first; a gap through the stop fills at the worse
  available price; a gap beyond the take-profit target is never credited
  as a favourable improvement; a position's own stop/target checks begin
  IMMEDIATELY, on the SAME candle its entry filled on - a pending signal
  decided from the previous candle's close fills at this candle's open,
  and this candle's own high/low reflect price action AFTER that open, so
  a stop or target genuinely can (and does) fire within it. This is not
  same-close execution: the entry decision still came from the prior
  completed candle, the fill still happens no earlier than this candle's
  open, and the strategy itself is never consulted about this candle's
  close before the entry - only the engine's own post-fill check reads
  this candle's OHLC, and only after the fill. Candidates' declared
  signal parameters are unchanged.

## Shadow mode (`shadow/`): forward-only observation of a frozen candidate

Shadow mode answers a different question from everything above: not "how
did E1 perform on already-observed history" (the research phase already
answered that) but "how is E1 performing on real market data it has never
seen, going forward, starting from a fixed date" - without ever risking
real or Testnet capital to find out.

**Design constraint, honored throughout:** nothing in `shadow/` modifies
E1 itself, its config, its risk policy, or any already-published research
result. `shadow/engine.py::run_shadow_cycle` reuses `backtest/engine.py::
run_segment` - the exact same simulation core `research/round3_report.py`
uses for E1 - completely unmodified, so shadow mode's entry/stop/target/
fee/slippage/sizing assumptions are byte-for-byte identical to the
already-evaluated candidate.

**"Recompute everything, every cycle" instead of incremental state.** A
naive incremental design (carry a rolling strategy/portfolio state forward
between cycles) would need to reimplement or duplicate the exact state
transitions `run_segment` already owns. Instead, every `shadow-run`
invocation re-runs `run_segment` from scratch over the ENTIRE accumulated
shadow candle history (from the fixed start boundary through the latest
completed candle). This is a pure, deterministic function of immutable
inputs, so it is naturally idempotent: the same inputs always produce the
same outputs. `shadow/store.py::ShadowStore` then diffs the freshly
computed trades/equity/journal entries against a persisted high-water mark
(`last_processed_close_time_ms`) and inserts only the rows strictly after
it, in one atomic transaction that also advances the mark - so a crash
between "candles fetched" and "this transaction commits" is always safe to
retry, and a completed candle can never be scored twice into the
persisted record. The cost of this design is that `run_segment`'s own
per-candle loop re-aggregates E1's weekly/4h view of history on every
call, so the recompute cost grows faster than linearly with the length of
the accumulated history - acceptable for an hourly cycle over the
multi-month/year horizon shadow mode is meant to run for, not a general
performance guarantee for an unboundedly long-lived deployment.

**The fixed shadow start boundary** (`shadow/boundary.py::
SHADOW_START_BOUNDARY_MS`, `2026-09-06T00:00:00Z`) is a plain module
constant, deliberately NOT a config field - a config field could be moved
earlier by a user, which would violate "must be fixed at or after". Every
candle shadow mode ever fetches, stores, or scores is filtered/asserted
against it (`assert_no_pre_boundary_candles`) before it can reach the
strategy. This is a completely different, independent boundary from
`research/cutoff.py::RESEARCH_CUTOFF_MS` (2025-05-16T00:00:00Z, the
pre-cutoff research/development boundary) - shadow mode never reads,
writes, or otherwise interacts with the research cutoff module.

**Bootstrap and the settling buffer (`shadow/bootstrap.py`) - why
`shadow-run` does not need to wait ~315 days.** E1's own
`min_required_candles` (~7,564 1h candles) is a warm-up requirement for
its INDICATORS (weekly EMA40+slope, 4h EMA200+slope+ATR+Donchian), not a
claim that trading can only start on day 315. `shadow-bootstrap` fetches
that much causal history (plus a small, fixed safety margin) STRICTLY
BEFORE the boundary, via the same paginated `data/historical_fetch.py::
fetch_historical_range` the `fetch-data --start --end` CLI command already
uses, and stores it as warm-up-only candles (`shadow/store.py`'s
`shadow_warmup_candles` provenance table) - candles E1's own aggregation
machinery reads for indicator context exactly like any other historical
candle, but which `shadow/engine.py` never lets `run_segment`'s loop treat
as a decision point (see below), so they can structurally never produce a
trade, move equity, or affect drawdown.

That alone is not quite sufficient: a 4h Donchian setup can arm on the
VERY LAST warm-up candle and still have up to `CONFIRMATION_WINDOW_1H_
CANDLES` (4, E1's own frozen constant) hours left to confirm - hours that
fall AFTER the boundary. Left unguarded, this would let a setup that
closed strictly BEFORE the boundary produce a trade strictly AFTER it,
violating "the first eligible 4h setup must close at or after the
boundary". The fix costs nothing and is exact, not probabilistic, because
`SHADOW_START_BOUNDARY_MS` (midnight UTC) always lands exactly on a
4h-candle grid boundary (every midnight does - 24h is an exact multiple of
4h): `shadow/bootstrap.py::compute_effective_min_required_candles` sets
the `min_required` value passed to the UNMODIFIED `run_segment` to
`warmup_candle_count + CONFIRMATION_WINDOW_1H_CANDLES + 1`, a
`CONFIRMATION_WINDOW_1H_CANDLES`-hour "settling buffer" past the boundary.
By the time `run_segment`'s loop reaches its first real, trade-affecting
iteration, ANY setup that closed before the boundary has had its entire
confirmation window fully elapse (it is provably `HOLD_SETUP_ALREADY_
CONSUMED`/expired by then), while the first genuinely POST-boundary setup
(whose own 4h bucket cannot close any earlier than
`boundary + CONFIRMATION_WINDOW_1H_CANDLES - 1`, again by grid alignment)
is caught at the very first hour it could possibly confirm - zero lost
eligibility, zero pre-boundary leakage. See `shadow/bootstrap.py`'s own
docstring for the full derivation and `tests/unit/test_shadow_engine.py`
for the proof in both directions.

`shadow-run` refuses to operate (`NOT_BOOTSTRAPPED`/`BOOTSTRAP_INVALID`,
zero network calls either way) until `shadow/bootstrap.py::
verify_bootstrap_complete` passes - a REAL re-derivation from the candles
actually present in `CandleStore` right now (exact count, exact range,
zero gaps), never a cached boolean. Bootstrap itself fails closed and
writes nothing to either database if the fetched warm-up range contains a
confirmed gap or turns out short/misaligned - a gap would mean E1's own
weekly/4h aggregation cannot honestly back an evaluation at the boundary.

**Structurally incapable of placing an order.** `shadow/` only ever
constructs `data/market_data_public.py::BinancePublicMarketDataClient`
against the read-only public market-data host - the same class every
other backtest/research code path already uses, with no signing logic and
no order-placement method of any kind. Nothing in `shadow/` imports
`execution/testnet_adapter.py` (the ONLY class in this codebase capable of
submitting a real order) - `tests/unit/test_shadow_engine.py` proves this
at the source level, not just by code review.

**Kill switch is a coarse, "pause everything" gate.** `shadow/engine.py`
checks a dedicated shadow kill switch (`risk/kill_switch.py::KillSwitch`,
reused unmodified, pointed at its own flag file) FIRST, before any fetch,
lock acquisition, or processing - if engaged, the entire cycle
short-circuits. This is coarser than `execution/live_runner.py`'s
per-order `RiskContext.kill_switch_engaged` gating (which still lets a
cycle run and only blocks the order itself); a finer-grained gate here
would require modifying `run_segment`, which is forbidden by this
package's own design constraint.

**Overlap lock.** `shadow/lock.py::ShadowLock` is a non-blocking
`fcntl.flock` advisory lock on a dedicated file - a second concurrent
`shadow-run` invocation fails immediately with `ShadowLockError` rather
than queuing or corrupting state; the OS releases the lock automatically
if the holding process crashes.

**Two SQLite connections, one file.** `data/storage.py::CandleStore`
(candle history) and `shadow/store.py::ShadowStore` (everything else -
trades, equity, journal entries, run state, the current open position)
both point at `data/shadow_agent.db`, but as two independent connections.
This is safe because, within one process, their writes are always
sequential and never interleaved: a cycle upserts fetched candles
(committing that write) strictly before it opens and commits
`ShadowStore`'s own transaction - and `shadow/lock.py` guarantees only one
process ever runs a cycle at a time.

### Optional Telegram notifications (`shadow/notifications/`)

An entirely optional, SHADOW-mode-only layer on top of everything above -
disabled by default, and shadow trading is completely unaffected by
Telegram's availability either way. **Design constraint, honored
throughout:** nothing here ever modifies E1's parameters, signals,
timeframe rules, risk policy, R/R, fees, slippage, sizing, or execution,
and nothing here ever adds Testnet or production order access.

**Durable outbox, delivery attempted only after commit.** A notification
event (`shadow/store.py::NotificationEvent`) is enqueued in the SAME
atomic transaction as the trading state it describes
(`ShadowStore.record_cycle_atomically`'s new `notification_events`
parameter) - so a hypothetical entry/exit is always durably persisted
BEFORE any attempt to actually reach Telegram, and a Telegram-side failure
of any kind can never roll back, delay, or corrupt the underlying trading
fact. `event_id` is deterministic and content-derived (`entry:<entry_time_
ms>`, `exit:<exit_time_ms>`, `daily_summary:<melbourne_date_iso>`) and
inserted with `ON CONFLICT(event_id) DO NOTHING`, so re-persisting the same
logical event across a retried/recomputed cycle is a harmless no-op rather
than a duplicate row - the same idempotency pattern this project already
uses for trades/equity/journal entries.

**Honest delivery-status tracking, not a false exactly-once claim.**
`shadow/notifications/telegram_client.py::TelegramClient.send_message`
classifies every outcome: a confirmed HTTP 200 with `{"ok": true}` is
`SENT`; any other definitive response (non-200, `{"ok": false}`, a
malformed body) is `FAILED` and never auto-retried (Telegram is known NOT
to have delivered it); a safe pre-transmission failure (DNS, connection
refused, connect timeout - `requests.exceptions.ConnectionError` and its
`ConnectTimeout` subclass) is retried with bounded linear backoff before
becoming `FAILED`; a read timeout AFTER the request was already
transmitted (`requests.exceptions.ReadTimeout`, deliberately NOT a
`ConnectionError` subclass) is `AMBIGUOUS` and NEVER auto-retried, since
Telegram's Bot API has no client-supplied idempotency key and a retry
could genuinely duplicate an already-delivered message. `notification-
retry --confirm` is the one deliberate, manual, warned code path that may
resend an `AMBIGUOUS` or `FAILED` event.

**Secret handling.** The bot token and chat ID are read from the
environment only (`config/loader.py::load_telegram_secrets`, opposite
fail-mode from the Testnet `load_secrets`: missing/blank just means no
notification is ever sent, never a fail-closed error) - never written to
or read from any YAML config, never persisted to the database, never
logged. Telegram's Bot API embeds the bot token in the request URL path
itself (no header-based alternative), so `telegram_client.py::_redact`
strips it out of every string this module could ever surface (a
`requests` exception, an HTTP response body) BEFORE that string is ever
returned, persisted as `shadow_notification_outbox.last_error`, or shown
by the CLI.

**Message content without touching forbidden files.** The entry
notification's planned stop-loss/take-profit prices are reproduced EXACTLY
by calling the same public, unmodified `backtest/risk_reward.py::
build_realized_plan` function `run_segment` already called internally,
fed the identical fill price, config-derived stop/fee/slippage rates,
exchange filters, and pre-entry equity - never approximated, never a
second implementation of that math (`shadow/notifications/builder.py::
compute_realized_plan_for_display`). Because `run_shadow_cycle` recomputes
the ENTIRE latest segment every cycle, an entry and its own exit can both
land in a single cycle (e.g. after a paused kill switch, or simply a
fast-moving take-profit) - `shadow/engine.py` builds an entry notification
for every genuinely new entry (`entry_time_ms` past the prior high-water
mark), whether that position is still open at the end of the cycle or has
already closed again within the same recompute, so neither event is ever
silently skipped or duplicated across cycles.

**Daily summary timing.** `shadow/engine.py::_maybe_send_daily_summary`
uses Python's stdlib `zoneinfo.ZoneInfo("Australia/Melbourne")` (DST-aware
automatically, no hand-rolled offset table) to send at most one
SHADOW-labelled summary per Melbourne calendar date, on the first
successful cycle at or after 08:00 local time - if the machine was
asleep/offline through 08:00, the first later successful cycle still sends
it, since there is no separate schedule to have "missed".

**Structurally incapable of order placement**, exactly like the rest of
`shadow/`: nothing under `shadow/notifications/` imports
`execution/testnet_adapter.py`, `execution/live_runner.py`,
`execution/binance_signing.py`, or any other order-placement-capable
module, and the only HTTP host it can ever construct a client for is
`https://api.telegram.org` - proven at the source level (AST-inspected,
not just grepped) by `tests/unit/
test_shadow_notifications_no_order_placement.py`.

## Crash recovery, the pending-order state machine, and the atomic transaction boundary

A process can die at any point: before sending an order, mid-HTTP-call, after
Binance has filled it but before the response is processed, or after the
response is processed but before the portfolio/risk state is persisted. The
system must recover correctly from a crash at ANY of these points, without
double-applying a fill or losing track of one.

**Round 2 correction (finding #1):** an earlier revision split this across
two independently-committed SQLite files - `portfolio_store.py` and
`pending_orders_store.py` - each written by its own separate `COMMIT`. A
crash between those two commits could apply a fill's cost to the portfolio
but never mark the order resolved (or the reverse), and there was no way to
detect or repair that split afterward. The authoritative transaction
boundary is now a single database, opened as a single SQLite connection,
holding both tables: `persistence/execution_store.py`'s `ExecutionStateStore`.
Its `apply_order_result_atomically()` method is the ONLY code path allowed
to change either table as a result of an order's execution, and it does so
inside one explicit transaction:

```
BEGIN IMMEDIATE
  re-read the pending_orders row (by client_order_id) and the
  portfolio_state row (by symbol) - both fresh, inside the transaction
  verify Binance's cumulative executed_qty/cumulative_quote_qty never
    decreased since what was already applied (else raise and roll back -
    see order_outcome.py's InconsistentExecutionReportError)
  compute the NEW delta only (cumulative fields minus what was already
    applied - never the full requested/remaining quantity)
  bucket commission by the asset it was actually charged in and apply the
    delta to portfolio_state (portfolio/state.py::apply_fill_delta)
  update pending_orders' applied_executed_qty / applied_cumulative_quote_qty /
    applied_commission_* fields, and mark it RESOLVED if the order's status
    is now terminal (FILLED / CANCELED / REJECTED / EXPIRED / the internal
    NEVER_SUBMITTED synthesized for a confirmed -2013 NOT_FOUND)
COMMIT
```

Any failure at any point after `BEGIN IMMEDIATE` - an injected fault, an
`InconsistentExecutionReportError`, a missing pending-order or portfolio
row, an unexpected exception - triggers an explicit `ROLLBACK`, leaving
BOTH tables exactly as they were before the call. That is what makes a
retry from the same durable state safe: the next cycle's reconciliation
re-reads the same still-`SUBMITTED` row and the same untouched portfolio,
and applying the same (or a further-progressed) order result is provably
either a no-op (if nothing new happened) or applies exactly the new delta
- never zero permanently, never twice. `tests/unit/test_execution_store.py`
proves this by injecting a fault at each of the three points a crash could
previously have landed between the old two commits (`before_write`,
`after_portfolio_write`, `before_commit`) and asserting the database is
byte-for-byte unchanged after rollback, then that a subsequent call from
that same state applies the fill exactly once.

The journal (`journal/journal.py`) is a separate, append-only audit log,
written only AFTER this transaction has already committed - it is never
treated as authoritative execution state and never participates in the
transaction; losing or corrupting it would not affect correctness, only
the audit trail.

The pending-order side of that same transaction is keyed by the
deterministic client order ID:

```
                     write BEFORE the exchange call
                                |
                                v
                         +--------------+
                         |  SUBMITTED   |  applied_executed_qty = 0
                         +--------------+
                                |
        (exchange call happens; process may crash here)
                                |
                                v
              next cycle's reconciliation queries
              get_order(client_order_id)
                                |
        +-----------+-----------+-----------+
        |           |                       |
   NOT_FOUND    still open              terminal
  (-2013: the   (NEW /                (FILLED / CANCELED /
   order never  PARTIALLY_FILLED)      REJECTED / EXPIRED)
   reached                                   |
   Binance)         |                        v
        |           v            apply_order_result_atomically()
        v    apply the NEW delta      applies the full
  mark RESOLVED,   only (never the    remaining delta,
  no portfolio     full requested     mark RESOLVED -
  change            qty), STAY        ALL in the SAME
                    SUBMITTED,        transaction as the
                    block ALL new     portfolio update
                    orders this
                    cycle
```

Why writing the `SUBMITTED` row *before* the exchange call is what makes
this correct: no matter when the crash happens, the next run finds a
durable record it can resolve by asking Binance directly - there is no
window where an order could have been sent but left no local trace.
Exactly-once application is enforced by the cumulative `applied_executed_qty`
/ `applied_cumulative_quote_qty` fields inside that one transaction:
`order_outcome.py` only ever applies the delta beyond what was already
applied, computed from Binance's own cumulative fields (never a
caller-recomputed running total, and never a proportional estimate across
fills at different prices - `tests/unit/test_execution_store.py` proves
this exact for two partial fills at different prices), so observing the
same order's status again - across cycles, after a crash, or both - never
double-counts a fill. A row only leaves the `SUBMITTED` state once its
outcome is terminal; a `NOT_FOUND` result is resolved immediately through
the same atomic path (a synthetic `NEVER_SUBMITTED` terminal result, since
there is nothing to apply but the row still must close out consistently),
and a still-open order stays `SUBMITTED` and blocks all new orders until a
later cycle resolves it.

### Remaining non-atomic operations

Two things are deliberately NOT inside `apply_order_result_atomically()`'s
transaction, and are not claimed to be atomic with it:

- **The exchange call itself.** Submitting the order to Binance
  (`adapter.place_market_order`) happens between writing the `SUBMITTED`
  row and calling `apply_order_result_atomically()` - it cannot be inside
  the same database transaction because it is a network call, not a local
  write. This is exactly why the pending-order row is written durably
  *before* that call: the crash window this creates is bridged by
  reconciliation (asking Binance directly what happened), not by trying to
  make an HTTP request part of a SQLite transaction.
- **The journal write.** `journal.record(...)` happens after
  `apply_order_result_atomically()` has already committed, in its own
  separate write to a separate database file. It is intentionally not
  folded into the same transaction (see "why SQLite" below) - the journal
  is an audit trail, not authoritative state, so this ordering means the
  journal can theoretically miss recording an event that already,
  correctly, changed portfolio state (if the process dies between the
  commit and the journal write), but can never record an event that
  didn't happen.

## Why SQLite, why no ORM

A small number of independent SQLite files - candles (plus their confirmed
historical gap manifest, `candle_gaps`, held in the SAME file specifically
so a download's candles and its gap record share one transaction - see
above), the unified execution store (portfolio state + pending orders,
held together specifically so they share one transaction - see above),
risk state, the journal - keep the system inspectable with nothing more
than the `sqlite3` CLI. There is no concurrent-writer scenario in V0.1 (one
CLI invocation runs at a time), so a lightweight, hand-written schema is
preferred over an ORM's abstraction for a project whose priority is
auditability over developer convenience at scale.

That single-invocation assumption is exactly what does NOT hold once
automatic scheduling exists (a cron job, a daemon) - see
[SCHEDULING_DESIGN.md](SCHEDULING_DESIGN.md) for the overlap-guard design
(a single-instance process lock, a database-backed cycle lease with
expiry, and candle-close-time uniqueness) required before that is safe to
build. Nothing in that design is implemented yet.

`ExecutionStateStore.open_read_only()` is a second, narrower exception to
"one CLI invocation at a time": it opens a SQLite connection with the
driver's own `mode=ro` URI flag specifically so that `testnet-health`
(read-only by design) can safely inspect execution state while, in
principle, another process holds the writable connection - a write
attempted through the read-only connection fails at the SQLite layer,
and it never creates the database file if one does not already exist.
