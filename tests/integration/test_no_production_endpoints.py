"""Source-level proof that production Binance endpoints cannot be selected.

This scans the actual package source (not just behavior at one call site)
so that a future change re-introducing a configurable order-execution host
would fail this test even before anyone writes a test for the specific new
code path.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from trading_agent.execution.testnet_adapter import TESTNET_HOST, TestnetBrokerAdapter

SRC_ROOT = Path(inspect.getfile(TestnetBrokerAdapter)).resolve().parents[2]
PRODUCTION_HOST_MARKERS = ("api.binance.com", "api1.binance.com", "api-gcp.binance.com")

# The only file allowed to mention a production host: the read-only,
# order-incapable public market-data client (approved as the historical
# backtest data source - see market_data_public.py's module docstring).
ALLOWED_FILE = "market_data_public.py"


def _iter_source_files() -> list[Path]:
    return list((SRC_ROOT / "trading_agent").rglob("*.py"))


def test_production_host_string_appears_only_in_the_approved_readonly_client():
    offending: list[str] = []
    for path in _iter_source_files():
        if path.name == ALLOWED_FILE:
            continue
        text = path.read_text(encoding="utf-8")
        for marker in PRODUCTION_HOST_MARKERS:
            if marker in text:
                offending.append(f"{path}: contains {marker!r}")
    assert offending == [], "production host string found outside the approved file:\n" + "\n".join(offending)


def test_testnet_adapter_base_url_is_the_testnet_host():
    assert TestnetBrokerAdapter.BASE_URL == TESTNET_HOST
    assert "api.binance.com" not in TestnetBrokerAdapter.BASE_URL


def test_testnet_adapter_has_no_order_incapable_bypass():
    # The order-placing class must not expose any way to redirect requests.
    forbidden_attrs = {"base_url", "host", "set_base_url", "set_host"}
    instance_attrs = set(vars(TestnetBrokerAdapter(api_key="k", api_secret="s")).keys())
    assert not (forbidden_attrs & instance_attrs)


def test_market_data_client_allowlist_does_not_include_arbitrary_hosts():
    from trading_agent.data.market_data_public import ALLOWED_HOSTS

    assert ALLOWED_HOSTS == {"https://api.binance.com", "https://testnet.binance.vision"}
