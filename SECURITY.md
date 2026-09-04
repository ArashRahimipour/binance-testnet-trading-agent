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
  signal candle's close time - so a retry after a timeout reuses the same
  ID rather than risking a duplicate order.
- Before ever retrying a timed-out order, the agent queries Binance for
  its actual status (`execution/reconciliation.py`) and retries only on a
  positive "order does not exist" confirmation.
- Every proposed order passes through an independent risk engine
  (`risk/engine.py`) and order validator (`execution/order_validator.py`)
  before it can reach the broker adapter - the strategy has no path to
  the broker that skips these.
- Position sizing and order validation only ever round quantity/price
  **down** to exchange filter boundaries and **reject** (never enlarge) a
  trade that falls below an exchange minimum after rounding.

## Data integrity

Missing, stale, duplicated, or out-of-order candle data blocks trading
(`data/validation.py`) rather than being estimated or patched over -
see RISK_POLICY.md.

## Dependencies

Runtime dependencies are limited to `requests`, `pandas`, `numpy`,
`pydantic`, `PyYAML`, `python-dotenv`, and `click` - no futures/margin
SDKs, no generic auto-retry library that could bypass the
reconciliation-before-retry rule above.
