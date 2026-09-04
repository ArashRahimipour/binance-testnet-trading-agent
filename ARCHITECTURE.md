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
| `data/` | Candle model, read-only market-data client, completed-candle filtering, fail-closed validation, SQLite candle storage |
| `indicators/` | Causal EMA/SMA (no look-ahead) |
| `strategy/` | `Signal` contract and the baseline EMA-crossover strategy - pure functions of (candles, position) |
| `sizing/` | Exchange filter parsing/rounding and fixed-fractional position sizing |
| `portfolio/` | Pure-function portfolio state machine (buy/sell transitions) and its SQLite persistence |
| `risk/` | The independent `RiskEngine`, its data contracts, and the kill switch |
| `execution/` | Order validator, idempotent client order IDs, the Testnet-only broker adapter, order-outcome dispatch, timeout/uncertain-order and startup reconciliation, fee computation, the backtest broker, and the single-cycle live runner |
| `persistence/` | SQLite stores for portfolio state, live risk-tracking state, and pending (in-flight) orders |
| `journal/` | Append-only audit trail of every decision |
| `metrics/` | Backtest performance report computation |
| `backtest/` | The chronological-holdout backtest engine tying the above together |
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
stop-loss mechanics described in its module docstring.

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

A small number of independent SQLite files - candles, the unified
execution store (portfolio state + pending orders, held together
specifically so they share one transaction - see above), risk state, the
journal - keep the system inspectable with nothing more than the `sqlite3`
CLI. There is no concurrent-writer scenario in V0.1 (one CLI invocation
runs at a time), so a lightweight, hand-written schema is preferred over an
ORM's abstraction for a project whose priority is auditability over
developer convenience at scale.
