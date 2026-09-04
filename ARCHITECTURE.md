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
| `execution/` | Order validator, idempotent client order IDs, the Testnet-only broker adapter, reconciliation, the backtest broker, and the single-cycle live runner |
| `persistence/` | SQLite stores for portfolio state and live risk-tracking state |
| `journal/` | Append-only audit trail of every decision |
| `metrics/` | Backtest performance report computation |
| `backtest/` | The walk-forward backtest engine tying the above together |
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

1. Fetch server time and the latest completed candles from the Testnet's
   public market-data endpoints (no API key needed for this).
2. Validate the candle series (fail closed on staleness/gaps/duplicates).
3. Load or reconcile portfolio state.
4. Generate a signal from the completed candles only.
5. If BUY/EXIT: size it, risk-check it, validate it, submit it via the
   Testnet-only adapter, reconcile on timeout, update persisted state.
6. Record every step in the journal.

The backtest engine (`backtest/engine.py`) walks the same pipeline
candle-by-candle over historical data, swapping only the broker.

## Why SQLite, why no ORM

Three small, independent SQLite files (or tables) - candles, portfolio
state, journal - keep the system inspectable with nothing more than the
`sqlite3` CLI. There is no concurrent-writer scenario in V0.1 (one CLI
invocation runs at a time), so a lightweight, hand-written schema is
preferred over an ORM's abstraction for a project whose priority is
auditability over developer convenience at scale.
