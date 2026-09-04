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
backoff on rate limits, de-duplicates overlapping candles, and validates
the assembled series before storing it (see
`src/trading_agent/data/historical_fetch.py`).

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

## Running on the Spot Testnet

```bash
trading-agent --mode testnet run
```

This performs **one decision cycle**: reconcile any unresolved order from
a previous run and check that local and exchange balances still agree,
fetch the latest completed candles from the Testnet, generate a signal,
and - for an EXIT only - size it, risk-check it, validate it against live
exchange filters, and (if every check passes and the
[kill switch](RISK_POLICY.md#kill-switch) is not engaged) place one real
(but play-money) order on the Spot Testnet. It is meant to be invoked once
per completed 4h candle, e.g. by a cron job or scheduled task:

```cron
# Run 5 minutes after every 4h candle closes (server time is UTC)
5 0,4,8,12,16,20 * * * cd /path/to/repo && .venv/bin/trading-agent --mode testnet run >> logs/cron.log 2>&1
```

> **A BUY signal is currently never acted on automatically on Testnet.**
> Automatic entry is disabled pending a verified exchange-resident
> protective stop order - see [RISK_POLICY.md](RISK_POLICY.md#protective-exits-why-max_risk_per_trade_pct-now-means-what-it-says-and-why-testnet-entry-is-disabled)
> for why. The `run` command will log and report a suppressed BUY signal
> rather than silently ignoring it. EXIT (closing a position you opened
> manually via the Testnet UI) still works normally.

On its very first run, the agent reconciles its starting portfolio from
your actual Testnet account balance. If that account already holds a
nonzero base-asset balance, the agent refuses to start rather than guess a
cost basis - flatten the position manually via the Testnet UI first.

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
- [CHANGELOG.md](CHANGELOG.md) - version history

## Official Binance documentation consulted

- https://github.com/binance/binance-spot-api-docs/blob/master/testnet/general-info.md
- https://github.com/binance/binance-spot-api-docs/blob/master/testnet/rest-api.md
- https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md
- https://github.com/binance/binance-spot-api-docs/blob/master/filters.md
