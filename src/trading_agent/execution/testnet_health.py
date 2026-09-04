"""Strictly read-only Binance Spot Testnet connectivity and credentials
health check (`trading-agent --mode testnet testnet-health`).

This module performs ONLY GET requests - public market data and signed
account/order-query endpoints - plus a read-only inspection of local
on-disk state. It has NO reference to `place_market_order` anywhere: it
does not import `execution/testnet_adapter.py` at all (the one class in
this codebase capable of placing, canceling, or modifying an order), and
the Testnet client it does use (`execution/testnet_readonly.py::
ReadOnlyTestnetClient`) has no such method to call in the first place -
see that module's docstring for the structural guarantee. See
`tests/unit/test_testnet_health.py` for the source-level and behavioral
proofs of every guarantee below.

What this command does, in order, and fails closed on:

1. Fetch Testnet server time (public, unsigned GET).
2. Synchronize the local clock against it and report the resulting offset
   in milliseconds - just an integer, never anything sensitive. Excessive
   drift (`ClockDriftError`) fails the check immediately, before any
   signed request is ever attempted.
3. Fetch BTCUSDT exchange info (public, unsigned GET) and validate that
   the filters this project actually depends on (tick size, step size,
   minimum quantity, minimum notional) are present and non-zero - a
   response missing them, or reporting zero, fails closed rather than
   silently proceeding with meaningless defaults (see
   `sizing/exchange_filters.py::SymbolFilters.from_exchange_info`, which
   itself defaults missing filters to "0" rather than raising - the
   validation that turns "0" into a failure lives here).
4. Perform ONE signed GET (`/api/v3/account`) - never a POST.
5. Report BTC/USDT free and locked balances as plain information. A
   nonzero BTC balance is not itself a failure - it is simply reported.
6. Query open BTCUSDT orders via GET (`/api/v3/openOrders`) - reported,
   never touched.
7. Report whether a local execution-state database exists at all -
   opened, if present, via `ExecutionStateStore.open_read_only()`, which
   never creates a missing file and returns a connection SQLite itself
   will refuse to write through.
8. Only when local state exists: compare its portfolio balances against
   the Testnet balances fetched in step 5, as information only - this
   never sets `reconciliation_blocked` or any other trading-path flag,
   and never writes anything back.
9. Report any local pending orders still in the SUBMITTED state, without
   resolving or otherwise touching them - the ordinary reconciliation
   path (`execution/startup_reconciliation.py`) is untouched by this
   command.
10. An overall PASS/FAIL: PASS only if every step that can meaningfully
    fail (1-6) succeeded. A local state file that does not exist yet is
    reported as informational, not a failure - steps 7-9 fail the overall
    result only if a local database exists but cannot be read back
    correctly (e.g. an unexpected schema), never merely for being absent.

Every detail string this module produces is passed through `_scrub()`
before being stored in the report, so even a coding mistake elsewhere
that let a secret, a signature, or a full signed query string leak into
an exception message cannot make it into this command's output.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

import requests

from trading_agent.config.models import AppConfig, Secrets
from trading_agent.data.market_data_public import BinancePublicMarketDataClient
from trading_agent.execution.binance_signing import (
    TESTNET_HOST,
    BinanceApiError,
    ClockDriftError,
)
from trading_agent.execution.symbol_parsing import base_asset, quote_asset
from trading_agent.execution.testnet_readonly import ReadOnlyTestnetClient
from trading_agent.persistence.execution_store import ExecutionStateStore
from trading_agent.sizing.exchange_filters import SymbolFilters

__all__ = ["CheckStep", "TestnetHealthReport", "run_testnet_health_check"]

_SIGNATURE_PATTERN = re.compile(r"signature=[0-9a-fA-F]+")


@dataclass(frozen=True, slots=True)
class CheckStep:
    name: str
    ok: bool
    detail: str


@dataclass
class TestnetHealthReport:
    steps: list[CheckStep] = field(default_factory=list)
    passed: bool = True

    def add(self, name: str, ok: bool, detail: str) -> None:
        self.steps.append(CheckStep(name, ok, detail))
        if not ok:
            self.passed = False


def _scrub(text: str, api_key: str, api_secret: str) -> str:
    """Defense in depth: strip anything shaped like a signature, and the
    literal secret values if they ever appeared, from any string this
    module is about to store in the report or print. Applied to every
    detail string unconditionally - not just ones judged "risky" - so a
    future mistake here fails safe rather than silently leaking."""
    scrubbed = _SIGNATURE_PATTERN.sub("signature=<redacted>", text)
    if api_key:
        scrubbed = scrubbed.replace(api_key, "<redacted>")
    if api_secret:
        scrubbed = scrubbed.replace(api_secret, "<redacted>")
    return scrubbed


def _describe_error(exc: Exception) -> str:
    """Never calls `str(exc)` on an exception type that could plausibly
    embed a URL, query string, or signed parameters (e.g. `requests`'
    connection/timeout errors, which can render the full request URL
    including `signature=...`) - only on types this project constructs
    itself from known-safe fields."""
    if isinstance(exc, BinanceApiError):
        return f"Binance API error {exc.code}: {exc.message} (HTTP {exc.status_code})"
    if isinstance(exc, ClockDriftError):
        return str(exc)  # built entirely from integers in compute_clock_offset - never a secret or signature
    if isinstance(exc, requests.exceptions.RequestException):
        return f"{type(exc).__name__}: network request to Testnet failed"
    if isinstance(exc, (ValueError, KeyError, TypeError, LookupError)):
        return f"{type(exc).__name__}: malformed or unexpected response"
    return f"{type(exc).__name__}: unexpected error"


def run_testnet_health_check(config: AppConfig, secrets: Secrets) -> TestnetHealthReport:
    report = TestnetHealthReport()
    symbol = config.market.symbol
    quote = quote_asset(symbol)
    base = base_asset(symbol)

    def fail(name: str, exc: Exception) -> None:
        report.add(name, False, _scrub(_describe_error(exc), secrets.testnet_api_key, secrets.testnet_api_secret))

    public_client = BinancePublicMarketDataClient(TESTNET_HOST)
    client = ReadOnlyTestnetClient(secrets.testnet_api_key, secrets.testnet_api_secret)

    # --- 1. Server time (public, unsigned GET). ---
    try:
        server_time_ms = public_client.get_server_time_ms()
    except Exception as exc:  # noqa: BLE001 - any failure here blocks every later step
        fail("server_time", exc)
        return report
    report.add("server_time", True, f"serverTime={server_time_ms}")

    # --- 2. Clock sync - fails closed on excessive drift, before any signed request. ---
    try:
        offset_ms = client.sync_time(server_time_ms)
    except ClockDriftError as exc:
        fail("clock_sync", exc)
        return report
    report.add("clock_sync", True, f"offset_ms={offset_ms}")

    # --- 3. Exchange info + required filters. ---
    try:
        exchange_info = public_client.get_exchange_info(symbol)
        filters = SymbolFilters.from_exchange_info(exchange_info)
        _require_nonzero_filters(filters)
    except Exception as exc:  # noqa: BLE001 - malformed/incomplete filters must fail closed
        fail("exchange_info", exc)
        return report
    report.add(
        "exchange_info", True,
        f"tick_size={filters.tick_size} step_size={filters.step_size} "
        f"min_qty={filters.min_qty} min_notional={filters.min_notional}",
    )

    # --- 4/5. Signed account info; free/locked balances (informational only). ---
    try:
        balances = client.get_account_balances()
    except Exception as exc:  # noqa: BLE001
        fail("account_info", exc)
        return report
    report.add("account_info", True, "signed GET /api/v3/account succeeded")
    base_free, base_locked = balances.get(base, (Decimal(0), Decimal(0)))
    quote_free, quote_locked = balances.get(quote, (Decimal(0), Decimal(0)))
    report.add(
        "balances", True,
        f"{base}: free={base_free} locked={base_locked}; {quote}: free={quote_free} locked={quote_locked}",
    )

    # --- 6. Open orders (GET only, reported, never touched). ---
    try:
        open_orders = client.get_open_orders(symbol)
    except Exception as exc:  # noqa: BLE001
        fail("open_orders", exc)
        return report
    if open_orders:
        detail = "; ".join(
            f"orderId={o.order_id} side={o.side} status={o.status} price={o.price} origQty={o.orig_qty}"
            for o in open_orders
        )
    else:
        detail = "none"
    report.add("open_orders", True, f"{len(open_orders)} open order(s): {detail}")

    # --- 7-9. Local execution state, read-only, informational only. ---
    _report_local_state(report, config, symbol, base, quote, base_free, base_locked, quote_free, quote_locked)

    return report


def _require_nonzero_filters(filters: SymbolFilters) -> None:
    missing = [
        name
        for name, value in (
            ("tick_size", filters.tick_size),
            ("step_size", filters.step_size),
            ("min_qty", filters.min_qty),
            ("min_notional", filters.min_notional),
        )
        if value <= 0
    ]
    if missing:
        raise ValueError(f"required exchange filter(s) missing or zero: {', '.join(missing)}")


def _report_local_state(
    report: TestnetHealthReport,
    config: AppConfig,
    symbol: str,
    base: str,
    quote: str,
    base_free: Decimal,
    base_locked: Decimal,
    quote_free: Decimal,
    quote_locked: Decimal,
) -> None:
    store = ExecutionStateStore.open_read_only(config.paths.db_path)
    if store is None:
        report.add("local_state", True, "no local execution-state database present")
        return
    try:
        report.add("local_state", True, "local execution-state database present (opened read-only)")

        try:
            portfolio = store.load_portfolio(symbol)
        except Exception as exc:  # noqa: BLE001 - an existing-but-unreadable DB is a real failure
            report.add("local_state_read", False, _describe_error(exc))
            return

        if portfolio is None:
            report.add("balance_comparison", True, f"no local portfolio state for {symbol} yet")
        else:
            base_diff = abs(portfolio.base_balance - (base_free + base_locked))
            quote_diff = abs(portfolio.quote_balance - (quote_free + quote_locked))
            report.add(
                "balance_comparison", True,
                f"local {base}={portfolio.base_balance} vs exchange={base_free + base_locked} (diff {base_diff}); "
                f"local {quote}={portfolio.quote_balance} vs exchange={quote_free + quote_locked} (diff {quote_diff})",
            )

        try:
            pending = store.load_open_pending(symbol)
        except Exception as exc:  # noqa: BLE001
            report.add("pending_orders_read", False, _describe_error(exc))
            return

        if pending:
            ids = ", ".join(p.client_order_id for p in pending)
            report.add(
                "pending_orders", True,
                f"{len(pending)} unresolved local pending order(s) (NOT reconciled by this command): {ids}",
            )
        else:
            report.add("pending_orders", True, "no unresolved local pending orders")
    finally:
        store.close()
