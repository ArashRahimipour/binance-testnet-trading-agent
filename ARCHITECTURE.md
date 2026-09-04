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
   signal. An order still open after this blocks new entries this cycle.
5. **Reconcile local balances against the exchange's free+locked
   balances**, every cycle, not just at cold start. A mismatch blocks new
   entries (never exits, never overwrites local state with a guess).
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

## Crash recovery and the pending-order state machine

A process can die at any point: before sending an order, mid-HTTP-call, after
Binance has filled it but before the response is processed, or after the
response is processed but before the portfolio/risk state is persisted. The
system must recover correctly from a crash at ANY of these points, without
double-applying a fill or losing track of one.

The mechanism is `persistence/pending_orders_store.py`, a durable table
keyed by the deterministic client order ID:

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
        |           v                 apply_order_result()
        v    apply the NEW delta      applies the full
  mark RESOLVED,   only (never the    remaining delta,
  no portfolio     full requested     mark RESOLVED
  change            qty), STAY
                    SUBMITTED,
                    block new
                    entries this
                    cycle
```

Why writing the `SUBMITTED` row *before* the exchange call is what makes
this correct: no matter when the crash happens, the next run finds a
durable record it can resolve by asking Binance directly - there is no
window where an order could have been sent but left no local trace. Exactly-
once application is enforced by `applied_executed_qty`: `order_outcome.py`
only ever applies `executed_qty - applied_executed_qty` (a Fill's own price
and quantity is real information from Binance, not a guess), so observing
the same order's status again - across cycles, after a crash, or both -
never double-counts a fill. A row only leaves the `SUBMITTED` state once its
outcome is terminal; a `NOT_FOUND` result is resolved immediately (nothing
to apply), and a still-open order stays `SUBMITTED` and blocks new entries
until a later cycle resolves it - the same order_outcome dispatch used
right after normal submission is reused here, so a status means the same
thing whether it is observed today or after a crash and restart.

## Why SQLite, why no ORM

Three small, independent SQLite files (or tables) - candles, portfolio
state, journal - keep the system inspectable with nothing more than the
`sqlite3` CLI. There is no concurrent-writer scenario in V0.1 (one CLI
invocation runs at a time), so a lightweight, hand-written schema is
preferred over an ORM's abstraction for a project whose priority is
auditability over developer convenience at scale.
