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
  baseline). Every planned BUY is sized to a fixed 1% of current equity
  maximum planned loss with an explicit stop AND take-profit plan (target
  at exactly 2x the stop distance) computed from the REAL simulated fill
  price and persisted with the position; an entry is rejected outright if
  the NET (cost-adjusted) reward/risk ratio falls below 2.0, or if a
  risk-safe size cannot satisfy the exchange's minimum notional/lot-size.
  If both the stop and take-profit are touched within one candle, STOP is
  assumed to occur first; a gap through the stop fills at the worse
  available price; a gap beyond the take-profit target is never credited
  as a favourable improvement; a position's own stop/target checks begin
  only on the candle after its entry filled (no same-candle entry/exit
  lookahead). Candidates' declared signal parameters are unchanged.

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
