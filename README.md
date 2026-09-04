# Binance Spot Testnet Trading Agent (V0.1)

> **Live trading is not available in this version.** V0.1 supports exactly
> two execution modes: `backtest` (simulated fills on historical data) and
> `testnet` (real orders against the Binance **Spot Testnet** only, using
> play money). There is no `live` mode, no production Binance endpoint
> anywhere in the order-submission code path, and no way to configure one -
> see [SECURITY.md](SECURITY.md) for how that is enforced and tested.

A modular, safety-first prototype for a multi-market trading agent. This
version trades BTC/USDT spot only, on 4-hour candles, long-or-cash only -
no margin, no leverage, no shorting, no futures, no withdrawals.

## Project status

This is a research prototype (V0.1). It has not been tested with a
meaningful amount of live capital because it cannot trade with real money
at all in this version. Backtest results in this repository are not a
claim of profitability - see [STRATEGY.md](STRATEGY.md).

## Architecture at a glance

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full picture. In short: the
strategy never talks to a broker. Every proposed trade flows
`strategy -> position sizer -> risk engine -> order validator -> broker
adapter`, and the broker adapter for real orders is hard-coded to the
Binance Spot Testnet host.

## Installation

Requires Python 3.11+.

```bash
git clone <this-repo-url>
cd binance-testnet-trading-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Configuration

Two separate things are configured differently, on purpose:

- **Non-secret settings** (mode, symbol, interval, strategy parameters,
  risk limits, fees/slippage assumptions, file paths) live in
  `config/default.yaml`. Copy it and edit a copy if you want different
  values; pass it with `--config path/to/your.yaml`.
- **Secrets** (Testnet API key/secret) live only in environment variables,
  loaded from a local `.env` file that is never committed. Copy
  `.env.example` to `.env` and fill in your **Spot Testnet** credentials
  (get them by logging in with GitHub at
  https://testnet.binance.vision/ - do **not** use production API keys,
  and never grant withdrawal permission on any key used here).

```bash
cp .env.example .env
# edit .env with your testnet key/secret
```

Validate your configuration at any time:

```bash
trading-agent config-check
trading-agent --mode testnet config-check   # override the mode for one command
```

## Running the tests

```bash
pytest
ruff check src tests
```

No test requires real credentials or network access - everything talks to
mocked HTTP responses. See [TESTING.md](TESTING.md) for what is covered.

## Historical data acquisition

Historical candles for backtesting come from Binance's public,
**unauthenticated, read-only** market-data endpoint
(`api.binance.com/api/v3/klines`) - not the Testnet, whose own history is
unreliable because it resets periodically and is a separate matching
engine from real markets. No API key is used or needed for this, and the
client used for it has no order-placing capability at all (see
`src/trading_agent/data/market_data_public.py`).

```bash
trading-agent fetch-data --limit 1000
```

This fetches the most recent completed candles for the configured symbol
and interval and stores them in the local SQLite database
(`config.paths.db_path`).

For a specific date range - including multiple years of history, which
needs more than one 1000-candle request - use `--start`/`--end`:

```bash
trading-agent fetch-data --start 2020-01-01 --end 2024-01-01
```

This pages through the full range automatically, with bounded retries and
backoff on rate limits, and de-duplicates overlapping candles. Real
multi-year history occasionally has a genuine gap - a candle the exchange
itself never recorded. This is never fabricated or interpolated over:
every gap is detected, given one focused narrow-range retry to rule out a
pagination artifact or a transient API response, and - if still missing -
CONFIRMED and recorded in a durable gap manifest alongside every valid
candle around it (see `src/trading_agent/data/historical_fetch.py` and
`data/gap_detection.py`). The command reports what it found:

```
Stored 43811 completed candles with 1 confirmed historical gap. No candles were fabricated.
  confirmed gap: expected_open_time_ms=1582113600000 previous_open_time_ms=1582099200000 next_open_time_ms=1582128000000 missing_intervals=1
```

Re-running the same download is idempotent - candles and gap records are
both keyed for upsert, so nothing is duplicated. This gap tolerance is
**historical-research-only**: live/Testnet signal generation
(`execution/live_runner.py`) always rejects any gap outright via a
completely separate, unmodified validation path - see ARCHITECTURE.md.

## Backtesting

```bash
trading-agent backtest
```

Runs the baseline strategy candle-by-candle over the stored history with no
look-ahead (see [STRATEGY.md](STRATEGY.md)), routes every proposed trade
through the same risk engine and order validator a live run would use,
and prints a performance report using **chronological holdout reporting**:
fixed train/validation/test date windows, evaluated with the *same*
strategy parameters throughout (see config/default.yaml) rather than
refitting per window. This is deliberately not model selection - it is
not genuine rolling walk-forward re-optimization, and no parameter is ever
chosen because it performed best on a split. The report covers total and
annualized return, max drawdown, volatility, Sharpe/Sortino (assumptions
documented), win rate, profit factor, exposure, turnover, trade count, and
a buy-and-hold comparison. A warning is printed whenever a split has too few trades to
be statistically meaningful. **This is a research report, not investment
advice, and past simulated performance does not indicate future results.**

If the stored history contains a confirmed gap, `config.backtest.gap_policy`
(default `"segment"`) splits the backtest into independent contiguous
segments at each gap rather than discarding the whole series - each
segment gets its own fresh indicator warm-up, portfolio, and day/cooldown
state, so nothing carries across the gap. A signal still queued at a
segment's end is cancelled, never carried into the next segment; a
position still open at a segment boundary caused by a gap is marked an
unresolved research condition (no exit price is ever invented for it) and
is, by default, excluded from the aggregate trade statistics
(`exclude_open_position_segments`). The command reports the breakdown:

```
gap_policy=segment  segments=2  confirmed_gaps=1
  segment 0: 2020-01-01 to 2020-02-19 (312 candles, 4 trade(s))
  segment 1: 2020-02-19 to 2024-01-01 (43495 candles, 61 trade(s))
WARNING: results across gaps are NOT one continuous tradable equity history - ...
```

Set `gap_policy: reject` to restore the original strict behavior (any gap
raises and aborts the backtest) if you would rather investigate a gap
manually before trusting a segmented result.

## Running on the Spot Testnet

```bash
trading-agent --mode testnet run
```

**Testnet operation is OBSERVATIONAL, not a general trading path.** Every
cycle: reconciles any unresolved order from a previous run and checks that
local and exchange balances still agree (either check failing blocks ALL
new order submission this cycle - BUY and EXIT alike, since an untrusted
local balance is exactly as unsafe for sizing a SELL as a BUY), fetches the
latest completed candles from the Testnet, and generates a signal. A BUY
signal is always logged and reported, never acted on - **this agent cannot
initiate a position on Testnet at all.** Only for an EXIT does it size,
risk-check, validate against live exchange filters, and (if every check
passes and the [kill switch](RISK_POLICY.md#kill-switch) is not engaged)
place one real (but play-money) order - and only to close, or help
recover, a position that already exists and has been fully reconciled
against the exchange. It is meant to be invoked once per completed 4h
candle, e.g. by a cron job or scheduled task:

```cron
# Run 5 minutes after every 4h candle closes (server time is UTC)
5 0,4,8,12,16,20 * * * cd /path/to/repo && .venv/bin/trading-agent --mode testnet run >> logs/cron.log 2>&1
```

> **A BUY signal is currently never acted on automatically on Testnet.**
> Automatic entry is disabled pending a verified exchange-resident
> protective stop order - see [RISK_POLICY.md](RISK_POLICY.md#protective-exits-why-max_risk_per_trade_pct-now-means-what-it-says-and-why-testnet-entry-is-disabled)
> for why. The `run` command will log and report a suppressed BUY signal
> rather than silently ignoring it. EXIT (closing a position you opened
> manually via the Testnet UI, or that a previous cycle opened) still
> works normally, provided local and exchange balances agree.

On its very first run, the agent reconciles its starting portfolio from
your actual Testnet account balance. If that account already holds a
nonzero base-asset balance, the agent refuses to start rather than guess a
cost basis - flatten the position manually via the Testnet UI first.

### Read-only Testnet connectivity check

```bash
trading-agent --mode testnet testnet-health
```

Before running `run` for the first time (or after rotating credentials, or
just to check things are working), this command verifies connectivity and
credentials **without ever placing, canceling, or modifying anything**:
server time, clock sync, BTCUSDT exchange filters, a signed account-info
call, and an open-orders query - all GET requests, nothing else. It also
reports (never modifies) local execution-state presence, a balance
comparison against the exchange when local state exists, and any
unresolved local pending orders. Exits non-zero on any failure - invalid
credentials, excessive clock drift, or a malformed/incomplete exchange
response all fail closed rather than reporting a false pass. Never prints
API keys, secrets, signatures, or signed query strings. See
[SECURITY.md](SECURITY.md) and [RISK_POLICY.md](RISK_POLICY.md) for the
full list of structural guarantees, and `tests/unit/test_testnet_health.py`
for their proofs.

```
[PASS] server_time: serverTime=1700000000000
[PASS] clock_sync: offset_ms=12
[PASS] exchange_info: tick_size=0.01 step_size=0.00001 min_qty=0.00001 min_notional=5
[PASS] account_info: signed GET /api/v3/account succeeded
[PASS] balances: BTC: free=0 locked=0; USDT: free=50 locked=0
[PASS] open_orders: 0 open order(s): none
[PASS] local_state: no local execution-state database present
overall: PASS
```

### Kill switch

```bash
trading-agent kill-switch engage --reason "reviewing strategy behavior"
trading-agent kill-switch status
trading-agent kill-switch disengage
```

Engaging the kill switch halts **all** order submission - both new
entries and exits - until manually disengaged. See
[RISK_POLICY.md](RISK_POLICY.md) for why exits are also blocked.

### Status

```bash
trading-agent status
```

Shows the current mode, kill switch state, and portfolio state (never
secrets).

## Documentation index

- [ARCHITECTURE.md](ARCHITECTURE.md) - module boundaries and data flow
- [RISK_POLICY.md](RISK_POLICY.md) - every configurable risk control and why it exists
- [STRATEGY.md](STRATEGY.md) - the baseline strategy's exact rules and limitations
- [TESTING.md](TESTING.md) - what is tested and how
- [SECURITY.md](SECURITY.md) - secret handling, endpoint restrictions, reporting
- [SCHEDULING_DESIGN.md](SCHEDULING_DESIGN.md) - the overlap-guard design required before any automatic scheduling is built (not yet implemented)
- [CHANGELOG.md](CHANGELOG.md) - version history

## Official Binance documentation consulted

- https://github.com/binance/binance-spot-api-docs/blob/master/testnet/general-info.md
- https://github.com/binance/binance-spot-api-docs/blob/master/testnet/rest-api.md
- https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md
- https://github.com/binance/binance-spot-api-docs/blob/master/filters.md
