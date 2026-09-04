# Scheduling and Overlap-Guard Design (future work, not yet implemented)

This document specifies what is required before this project may run its
`testnet run` cycle automatically and unattended (a cron job, a systemd
timer, a daemon loop, or any other scheduler). **Nothing in this document
is implemented yet.** There is no daemon, no process lock, no lease table,
and no scheduler integration in the current codebase - `trading-agent
--mode testnet run` remains a single, manually (or externally,
one-shot-per-invocation) triggered decision cycle that starts, does at
most one thing, and exits. This document exists so that when automatic
scheduling is built, it is built to a specification the project has
already thought through - not improvised under the pressure of "we should
probably just be able to run this on a timer now."

## Why this needs a design pass before any code

`run_testnet_cycle` (`execution/live_runner.py`) assumes it is the only
process touching `ExecutionStateStore`'s SQLite database, the risk-state
store, the journal, and the kill-switch flag file at any given moment.
That assumption holds today because a human runs the command by hand, or
a single cron line runs it once per candle. It stops holding the moment
any of the following becomes possible:

- A cron job's previous invocation is still running (a slow network call,
  a stuck retry loop) when the next scheduled invocation fires.
- Two different scheduling mechanisms are configured against the same
  config/database by mistake (e.g. both a systemd timer and a leftover
  cron line).
- A process is killed (OOM, host reboot, `kill -9`) mid-cycle, after it
  has taken some durable action but before it releases whatever "I'm
  running" signal it set.

`ExecutionStateStore`'s atomic transaction (ARCHITECTURE.md's "Crash
recovery" section) already protects the CONTENTS of a single order's
application from being corrupted by a crash. It does NOT protect against
two overlapping PROCESSES independently reading the same "no pending
order, signal is X" state and both deciding to act on it, or against one
process's stale in-memory decision being applied after a second process
has already moved state forward. That is a different failure class -
concurrent access, not partial application - and needs a different
mechanism: an explicit overlap guard.

## Required components

### 1. A single-instance process lock

Before doing anything else - before even reading config or touching any
database - the process must attempt to acquire an exclusive, host-local
lock (an `flock()`'d PID file is the standard, simplest mechanism; a
`fcntl.flock` on a file under `config.paths.data_dir`, e.g.
`data_dir/testnet.lock`, held for the process's entire lifetime and
released automatically on exit or crash by the OS - never a lock whose
release depends on the process's own cleanup code running).

- If the lock cannot be acquired, the process must exit immediately,
  non-zero, having done nothing else - no config validation side effects,
  no network calls, no database opens. This is the first line of defense
  against overlapping cron invocations on the SAME host, and it is cheap
  and immediate: it does not depend on any database round-trip.
- This lock is host-local by construction (`flock` does not span
  machines). It does not protect against two different hosts pointed at
  the same database - the lease mechanism below does.

### 2. A database-backed cycle lease with expiry

A new table (living in the same `ExecutionStateStore` database, so it
shares that database's existing backup/inspection story - not a new
separate file) records who is currently "running a cycle" and until when
that claim is valid:

```sql
CREATE TABLE cycle_lease (
    symbol TEXT PRIMARY KEY,
    holder_id TEXT NOT NULL,        -- e.g. f"{hostname}:{pid}:{start_time_ms}"
    acquired_at_ms INTEGER NOT NULL,
    expires_at_ms INTEGER NOT NULL, -- acquired_at_ms + a short, config-driven TTL
    candle_close_time_ms INTEGER NOT NULL  -- see uniqueness section below
);
```

Acquiring the lease is a single conditional write, executed inside its
own short transaction, that succeeds only if no unexpired row exists for
this symbol:

```sql
DELETE FROM cycle_lease WHERE symbol = ? AND expires_at_ms < ?;  -- reap any expired lease first
INSERT INTO cycle_lease (symbol, holder_id, acquired_at_ms, expires_at_ms, candle_close_time_ms)
VALUES (?, ?, ?, ?, ?);  -- fails on the PRIMARY KEY if a live lease still exists
```

- The TTL must be generous relative to how long a cycle can legitimately
  take (network timeouts, retries) but short relative to the candle
  interval - e.g. a few minutes for 4h candles - so a genuinely dead
  holder's lease expires and is reclaimable well before the next
  scheduled invocation, without requiring the new process to guess
  whether the old one is really gone.
- The lease is released explicitly (a `DELETE` matching `holder_id`) when
  the cycle completes normally, and is otherwise left to expire on its
  own - never "cleaned up" by a signal handler or `atexit` hook, since
  those do not run on `SIGKILL` or a host power loss. Relying on expiry,
  not on cleanup code executing, is what makes this crash-safe.
- Because this is a single row-level `INSERT` guarded by a `PRIMARY KEY`
  constraint (not a read-then-write race), two processes racing to
  acquire the same symbol's lease at the same instant are decided by
  SQLite's own transaction serialization - never both succeeding.

### 3. Uniqueness by symbol and candle close time

The lease alone stops two processes from running *concurrently*, but not
a scheduler that fires the SAME candle's cycle twice in a row (e.g. a
misconfigured cron entry, a manual re-run, a scheduler retry after a
false-positive failure detection). `run_testnet_cycle` must therefore be
idempotent with respect to `(symbol, candle_close_time_ms)`, not just
protected against overlap:

- Before generating a new signal, check whether a cycle has already been
  durably recorded as run for this exact `(symbol, candle_close_time_ms)`
  pair (a small `completed_cycles` table, or equivalently a query against
  the journal for a `CYCLE_COMPLETE` event carrying that close time). If
  so, exit reporting `ALREADY_PROCESSED_THIS_CANDLE` rather than
  re-evaluating the strategy and potentially generating a second,
  redundant order attempt.
- This is a SEPARATE guarantee from the deterministic client-order-ID
  scheme already in place (`execution/client_order_id.py`), which stops a
  *retried order submission* from becoming a duplicate order at the
  exchange. The candle-uniqueness check stops the *cycle itself* from
  being re-run at the application level; the client-order-ID scheme is
  the fallback structural guarantee if it somehow were, since a
  re-attempt would submit the exact same deterministic ID and be rejected
  or matched as a duplicate.

### 4. Protection against overlapping cron invocations

Combining the two mechanisms above gives the actual required guarantee:

1. Cron fires. The process attempts the local `flock` (component 1). If
   another instance is still holding it, exit immediately - this is the
   common case for "previous invocation still running" on the same host.
2. If the lock is acquired, the process attempts the database lease
   (component 2) for this symbol. If a live lease already exists (e.g. a
   different host, or a lock file that was somehow bypassed), exit
   without proceeding.
3. Only once both are held does the process check candle uniqueness
   (component 3) and, if this candle has not already been processed,
   proceed with `run_testnet_cycle` as today.
4. On completion (success OR a handled failure), release the lease
   explicitly. On an unhandled crash, the lease is left to expire
   naturally.

No cron configuration change (e.g. `flock` wrapping the cron line itself)
should be relied upon as the ONLY guard - it is a reasonable belt-and-
suspenders addition, but the guarantee must live in the application,
since the scheduler invoking it (cron today, something else tomorrow) is
explicitly out of this project's control and easy to misconfigure.

### 5. Recovery from a process dying while holding the lease

This is the scenario the whole design centers on, so it is worth stating
explicitly rather than leaving it implied by "the lease expires":

- A process crashes (killed, OOM, host reboot) after acquiring the lease
  but before releasing it, at ANY point in its cycle - including after it
  has taken a durable action (e.g. after `execution_store.create_pending`
  but before the order response is applied).
- The lease row remains in the database with its original `expires_at_ms`
  until either (a) that time passes, or (b) a later process observes it
  and reaps it (the `DELETE ... WHERE expires_at_ms < ?` in the
  acquisition query above) - never by a human or another process manually
  clearing it before expiry, which would defeat the point of an expiry
  in the first place.
- Once the lease expires and a new process acquires it, RECOVERY of
  whatever the dead process was doing is entirely the existing crash-
  recovery mechanism's job (ARCHITECTURE.md's "Crash recovery" section):
  `reconcile_pending_orders` runs, as it already does today, before any
  new signal is generated. The lease mechanism's only responsibility is
  making sure that reconciliation runs from a SINGLE new process, not
  from two processes racing each other to reconcile the same crashed
  order at the same time - which is exactly the concurrent-access failure
  class this document opened with, and which `ExecutionStateStore`'s
  transaction alone does not prevent (two processes could each read the
  same `SUBMITTED` row, then both attempt
  `apply_order_result_atomically` - individually safe and idempotent
  each, but redundant network calls and log noise the lease avoids
  entirely by ensuring only one process is ever in that code path for a
  given symbol at a time).
- The TTL choice directly trades off "how fast can we recover from a
  dead holder" against "how much margin does a slow-but-alive holder
  have before being wrongly declared dead and having a second process
  start reconciling underneath it." This tradeoff must be revisited
  against real observed cycle durations before this is implemented - not
  guessed at generically here.

## What this document does NOT specify (deliberately)

- No daemon/scheduler-loop implementation - the mechanism above is
  designed to wrap the EXISTING one-shot `run_testnet_cycle` invocation
  model, not to replace it with a persistent process. Whether automatic
  scheduling eventually means "cron calls the CLI" (using the guards
  above) or "a long-running daemon sleeps between candles" is an open
  question this document does not answer, since the guard design is the
  same either way and is the actual prerequisite.
- No multi-symbol concurrency model - the lease table above is keyed by
  `symbol` specifically so that a future multi-market version could run
  independent symbols concurrently without contention, but V0.1 is
  single-symbol (BTC/USDT) and this is not exercised or tested here.
- No implementation, no tests, no schema migration. This is a design
  document only - see the top of this file.
