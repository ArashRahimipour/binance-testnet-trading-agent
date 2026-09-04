# Security

## Reporting

This is a personal/experimental project, not a maintained public service.
If you find a security issue, open an issue in this repository describing
it; do not include real credentials or account details in the report.

## No production trading endpoints, structurally

- `TestnetBrokerAdapter` (`src/trading_agent/execution/testnet_adapter.py`)
  is the **only** class in this codebase capable of placing, querying, or
  canceling an order. Its `BASE_URL` is a hard-coded class constant equal
  to `https://testnet.binance.vision` - not a constructor parameter, not
  read from config, not overridable by an environment variable.
- The read-only market-data client
  (`src/trading_agent/data/market_data_public.py`) has an explicit host
  allowlist (`{api.binance.com, testnet.binance.vision}`) and has **no
  order-placing methods at all** - it cannot submit an order even if
  misused, because the capability does not exist on the class.
- `--mode` is a `click.Choice` restricted to the two `Mode` enum values
  (`backtest`, `testnet`); there is no `live` choice and no code path that
  produces one.
- `tests/integration/test_no_production_endpoints.py` scans the actual
  package source for production Binance hostnames and asserts they appear
  only inside the one approved read-only file - a source-level proof that
  survives future edits, not just a behavioral test of today's code paths.

## Secrets

- Binance Spot Testnet API key/secret are read **only** from environment
  variables (`BINANCE_TESTNET_API_KEY`, `BINANCE_TESTNET_API_SECRET`),
  populated locally via `.env` (see `.env.example`) and loaded with
  `python-dotenv`.
- `.env`, `*.db`/`*.sqlite*`, and `logs/` are excluded via `.gitignore`.
  Never commit a real `.env`.
- `Secrets.__repr__`/`__str__` (`config/models.py`) are overridden to
  never reveal the actual key/secret values, even if a `Secrets` instance
  is accidentally printed or logged.
- All structured logs pass through `SecretRedactionFilter`
  (`logging_setup.py`), which redacts both any known secret value and any
  substring shaped like `api_key=...` / `secret=...` / `signature=...`,
  as defense in depth against a future logging bug.
- Testnet API keys must never be granted withdrawal permission (testnet
  funds cannot be withdrawn or transferred at all - see Binance's own
  Testnet documentation - but this is stated here as a standing rule for
  any future adapter with real capital).
- No test in the suite requires real credentials; all use fixed dummy
  strings and mocked HTTP.

## Order safety

- Client order IDs are deterministic
  (`execution/client_order_id.py`) - a hash of symbol, side, and the
  signal candle's close time - so a retry after a submission failure
  reuses the same ID rather than risking a duplicate order.
- Before ever retrying, the agent queries Binance for the order's actual
  status (`execution/reconciliation.py`) and retries only on a positive
  "order does not exist" confirmation. This applies to **any** ambiguous
  network failure - timeout, connection reset, DNS failure - not just a
  timeout specifically (`requests.exceptions.RequestException` is caught
  as a whole in `execution/live_runner.py`).
- The intent to submit an order is durably recorded (`persistence/
  execution_store.py`'s `ExecutionStateStore`) *before* the exchange call
  is made, so a process crash at any point - before, during, or after that
  call - is recoverable: the next run resolves it by asking Binance
  directly rather than guessing. Applying that resolution - updating
  portfolio state and marking the order resolved - happens in ONE atomic
  SQLite transaction, not two independently-committed writes, so a crash
  mid-application can never apply a fill's cost without recording it (or
  the reverse); see ARCHITECTURE.md's "Crash recovery" section.
- An order's actual reported status (`NEW`, `PARTIALLY_FILLED`, `FILLED`,
  `CANCELED`, `REJECTED`, `EXPIRED`) decides what happens to local state
  (`execution/order_outcome.py`) - the requested quantity is never
  substituted for a missing or zero `executed_qty`, deltas are computed
  from Binance's own cumulative fields (never a proportional estimate, and
  rejected outright if they ever decrease), and re-observing the same
  order (e.g. during crash recovery) never double-applies a fill.
- Commission is bucketed by the asset it was actually charged in (quote,
  base, or a third asset like BNB) rather than ever silently converted
  into an estimated fee while still crediting the full traded quantity;
  a base-asset commission reported on a SELL - which Binance's fee model
  does not support - is rejected rather than guessed at
  (`execution/fees.py`, `portfolio/state.py::apply_fill_delta`).
- Local balances are reconciled against the exchange's actual free+locked
  balances every cycle, not just at startup; an unexplained mismatch
  blocks ALL new order submission - BUY and EXIT/SELL alike, since this
  project sizes a SELL from the local balance before ever asking Binance,
  so a stale local figure is exactly as unsafe there as for a BUY.
- Signed requests use a bounded offset from the exchange's own server
  time, not the raw local clock (`TestnetBrokerAdapter.sync_time()`);
  excessive drift raises `ClockDriftError` and blocks the cycle rather
  than risking a rejected or misinterpreted signed request.
- Every proposed order passes through an independent risk engine
  (`risk/engine.py`) and order validator (`execution/order_validator.py`)
  before it can reach the broker adapter - the strategy has no path to
  the broker that skips these.
- Position sizing and order validation only ever round quantity/price
  **down** to exchange filter boundaries and **reject** (never enlarge) a
  trade that falls below an exchange minimum after rounding.
- Testnet operation is OBSERVATIONAL, not a general trading path: automatic
  entry (BUY) is disabled entirely, pending a verified exchange-resident
  protective stop order, and SELL exists only to close or help recover a
  position that already exists and has been fully reconciled - never to
  open or add to one. See RISK_POLICY.md's "Protective exits" section.

## Data integrity

Missing, stale, duplicated, or out-of-order candle data blocks trading
(`data/validation.py`) rather than being estimated or patched over -
see RISK_POLICY.md.

## Dependencies

Runtime dependencies are limited to `requests`, `pandas`, `numpy`,
`pydantic`, `PyYAML`, `python-dotenv`, and `click` - no futures/margin
SDKs, no generic auto-retry library that could bypass the
reconciliation-before-retry rule above.
