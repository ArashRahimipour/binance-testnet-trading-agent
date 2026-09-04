"""Tests for the strictly read-only `testnet-health` command
(`execution/testnet_health.py`), proving every guarantee in that module's
docstring and in SECURITY.md/RISK_POLICY.md.
"""

from __future__ import annotations

import inspect
import time
from decimal import Decimal

import responses
from click.testing import CliRunner

from tests.fixtures.exchange_info import make_exchange_info
from trading_agent.cli.main import cli
from trading_agent.config.models import AppConfig, Secrets
from trading_agent.execution import testnet_health, testnet_readonly
from trading_agent.execution.binance_signing import TESTNET_HOST
from trading_agent.execution.testnet_health import run_testnet_health_check
from trading_agent.persistence.execution_store import ExecutionStateStore
from trading_agent.portfolio.state import PortfolioState
from trading_agent.strategy.base import PositionSide

HOST = TESTNET_HOST
SECRET_KEY = "distinctive-fake-api-key-zzz"
SECRET_VALUE = "distinctive-fake-api-secret-yyy"


def _config(tmp_path) -> AppConfig:
    return AppConfig(
        mode="testnet",
        paths={"data_dir": str(tmp_path), "logs_dir": str(tmp_path), "db_path": str(tmp_path / "agent.db")},
    )


def _secrets() -> Secrets:
    return Secrets(testnet_api_key=SECRET_KEY, testnet_api_secret=SECRET_VALUE)


def _mock_server_time(offset_ms: int = 0):
    server_time = int(time.time() * 1000) + offset_ms
    responses.add(responses.GET, f"{HOST}/api/v3/time", json={"serverTime": server_time}, status=200)


def _mock_exchange_info(**kwargs):
    responses.add(responses.GET, f"{HOST}/api/v3/exchangeInfo", json=make_exchange_info(**kwargs), status=200)


def _mock_account(base_free="0", base_locked="0", quote_free="50", quote_locked="0"):
    responses.add(
        responses.GET,
        f"{HOST}/api/v3/account",
        json={"balances": [
            {"asset": "USDT", "free": quote_free, "locked": quote_locked},
            {"asset": "BTC", "free": base_free, "locked": base_locked},
        ]},
        status=200,
    )


def _mock_open_orders(orders=None):
    responses.add(responses.GET, f"{HOST}/api/v3/openOrders", json=orders or [], status=200)


def _mock_happy_path(**account_kwargs):
    _mock_server_time()
    _mock_exchange_info()
    _mock_account(**account_kwargs)
    _mock_open_orders()


# --- Source-level guarantees: no reference to order-placing capability. ---


def _assert_no_order_placement_reference(source: str) -> None:
    # Mentioning the name in a docstring to explain its ABSENCE is fine and
    # expected (see both modules' module docstrings) - what must never
    # appear is an actual reference to it: a definition, a call, an
    # attribute access, or an import.
    forbidden_patterns = (
        "def place_market_order",
        ".place_market_order(",
        "import place_market_order",
        "from trading_agent.execution.testnet_adapter import",
        "TestnetBrokerAdapter(",
    )
    for pattern in forbidden_patterns:
        assert pattern not in source, f"found forbidden pattern {pattern!r}"


def test_testnet_health_module_never_references_place_market_order():
    _assert_no_order_placement_reference(inspect.getsource(testnet_health))


def test_testnet_readonly_module_never_references_place_market_order():
    _assert_no_order_placement_reference(inspect.getsource(testnet_readonly))


def test_no_order_placement_method_reachable_from_the_client_used_here():
    from trading_agent.execution.testnet_readonly import ReadOnlyTestnetClient

    assert not hasattr(ReadOnlyTestnetClient, "place_market_order")


def test_production_endpoints_cannot_be_selected():
    from trading_agent.execution.testnet_readonly import ReadOnlyTestnetClient

    assert ReadOnlyTestnetClient.BASE_URL == TESTNET_HOST
    assert "api.binance.com" not in inspect.getsource(testnet_health)
    assert "api.binance.com" not in inspect.getsource(testnet_readonly)


# --- Happy path and GET-only proof. ---


@responses.activate
def test_happy_path_passes_and_only_get_requests_occur(tmp_path):
    _mock_happy_path()
    report = run_testnet_health_check(_config(tmp_path), _secrets())
    assert report.passed is True
    assert len(responses.calls) == 4  # time, exchangeInfo, account, openOrders
    assert all(call.request.method == "GET" for call in responses.calls)


@responses.activate
def test_no_database_or_local_state_file_created_when_none_exists(tmp_path):
    _mock_happy_path()
    config = _config(tmp_path)
    assert not config.paths.db_path.exists()
    report = run_testnet_health_check(config, _secrets())
    assert report.passed is True
    assert not config.paths.db_path.exists()
    local_state_step = next(s for s in report.steps if s.name == "local_state")
    assert local_state_step.ok is True
    assert "no local execution-state database" in local_state_step.detail


@responses.activate
def test_existing_local_state_is_not_modified(tmp_path):
    _mock_happy_path(base_free="0.001", quote_free="10")
    config = _config(tmp_path)
    with ExecutionStateStore(config.paths.db_path) as store:
        store.save_portfolio(
            "BTCUSDT",
            PortfolioState(
                quote_balance=Decimal(10), base_balance=Decimal("0.001"),
                position_side=PositionSide.LONG, avg_entry_price=Decimal(50000),
                realized_pnl_quote=Decimal(0),
            ),
            updated_at_ms=123,
        )
        store.create_pending("ta-unresolved", "BTCUSDT", "SELL", Decimal("0.001"), 1, 2)

    before = config.paths.db_path.read_bytes()
    report = run_testnet_health_check(config, _secrets())
    after = config.paths.db_path.read_bytes()

    assert report.passed is True
    assert before == after  # byte-for-byte unchanged
    with ExecutionStateStore(config.paths.db_path) as store:
        portfolio = store.load_portfolio("BTCUSDT")
        pending = store.get_pending("ta-unresolved")
    assert portfolio.base_balance == Decimal("0.001")
    assert pending.status == "SUBMITTED"  # never reconciled by the health check


@responses.activate
def test_pending_orders_reported_but_not_reconciled(tmp_path):
    _mock_happy_path()
    config = _config(tmp_path)
    with ExecutionStateStore(config.paths.db_path) as store:
        store.save_portfolio("BTCUSDT", PortfolioState.initial(Decimal(50)), updated_at_ms=0)
        store.create_pending("ta-unresolved", "BTCUSDT", "SELL", Decimal("0.001"), 1, 2)

    report = run_testnet_health_check(config, _secrets())
    pending_step = next(s for s in report.steps if s.name == "pending_orders")
    assert pending_step.ok is True
    assert "ta-unresolved" in pending_step.detail
    assert "NOT reconciled" in pending_step.detail
    with ExecutionStateStore(config.paths.db_path) as store:
        assert store.get_pending("ta-unresolved").status == "SUBMITTED"


# --- Secret handling. ---


@responses.activate
def test_secret_never_appears_in_report_on_happy_path(tmp_path):
    _mock_happy_path(base_free="0.001", base_locked="0.0002")
    report = run_testnet_health_check(_config(tmp_path), _secrets())
    all_text = "\n".join(f"{s.name} {s.detail}" for s in report.steps)
    assert SECRET_KEY not in all_text
    assert SECRET_VALUE not in all_text
    assert "signature=" not in all_text


@responses.activate
def test_secret_never_appears_on_invalid_credentials_failure(tmp_path):
    _mock_server_time()
    _mock_exchange_info()
    responses.add(
        responses.GET, f"{HOST}/api/v3/account",
        json={"code": -2015, "msg": "Invalid API-key, IP, or permissions for action."}, status=401,
    )
    report = run_testnet_health_check(_config(tmp_path), _secrets())
    all_text = "\n".join(f"{s.name} {s.detail}" for s in report.steps)
    assert SECRET_KEY not in all_text
    assert SECRET_VALUE not in all_text
    assert "signature=" not in all_text
    account_step = next(s for s in report.steps if s.name == "account_info")
    assert account_step.ok is False
    assert report.passed is False


@responses.activate
def test_secret_never_appears_in_cli_stdout_or_stderr(tmp_path, monkeypatch):
    _mock_server_time()
    _mock_exchange_info()
    responses.add(
        responses.GET, f"{HOST}/api/v3/account",
        json={"code": -2015, "msg": "Invalid API-key, IP, or permissions for action."}, status=401,
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"mode: testnet\npaths:\n  data_dir: {tmp_path}\n  logs_dir: {tmp_path}\n  db_path: {tmp_path / 'agent.db'}\n")
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", SECRET_KEY)
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", SECRET_VALUE)

    result = CliRunner().invoke(cli, ["--config", str(config_path), "testnet-health"])

    assert SECRET_KEY not in result.output
    assert SECRET_VALUE not in result.output
    assert "signature=" not in result.output
    assert result.exit_code != 0  # invalid credentials -> fails closed


# --- Balance / order reporting. ---


@responses.activate
def test_nonzero_btc_balance_is_reported_as_information_not_a_failure(tmp_path):
    _mock_happy_path(base_free="1.5", base_locked="0")
    report = run_testnet_health_check(_config(tmp_path), _secrets())
    assert report.passed is True
    balances_step = next(s for s in report.steps if s.name == "balances")
    assert balances_step.ok is True
    assert "1.5" in balances_step.detail


@responses.activate
def test_locked_balances_are_reported(tmp_path):
    _mock_happy_path(base_free="0.0005", base_locked="0.0005", quote_free="10", quote_locked="5")
    report = run_testnet_health_check(_config(tmp_path), _secrets())
    balances_step = next(s for s in report.steps if s.name == "balances")
    assert "locked=0.0005" in balances_step.detail
    assert "locked=5" in balances_step.detail


@responses.activate
def test_open_orders_are_displayed_without_modification(tmp_path):
    _mock_server_time()
    _mock_exchange_info()
    _mock_account()
    _mock_open_orders([
        {"symbol": "BTCUSDT", "orderId": 42, "clientOrderId": "ta-x", "price": "60000", "origQty": "0.001", "status": "NEW", "side": "SELL"},
    ])
    report = run_testnet_health_check(_config(tmp_path), _secrets())
    open_orders_step = next(s for s in report.steps if s.name == "open_orders")
    assert open_orders_step.ok is True
    assert "orderId=42" in open_orders_step.detail
    assert "side=SELL" in open_orders_step.detail
    assert all(call.request.method == "GET" for call in responses.calls)


@responses.activate
def test_balance_comparison_only_reported_when_local_state_exists(tmp_path):
    _mock_happy_path(base_free="0.001", quote_free="10")
    config = _config(tmp_path)
    report_without_local_state = run_testnet_health_check(config, _secrets())
    assert not any(s.name == "balance_comparison" for s in report_without_local_state.steps)

    responses.reset()
    _mock_happy_path(base_free="0.001", quote_free="10")
    with ExecutionStateStore(config.paths.db_path) as store:
        store.save_portfolio(
            "BTCUSDT",
            PortfolioState(
                quote_balance=Decimal(10), base_balance=Decimal("0.001"),
                position_side=PositionSide.LONG, avg_entry_price=Decimal(50000),
                realized_pnl_quote=Decimal(0),
            ),
            updated_at_ms=1,
        )
    report_with_local_state = run_testnet_health_check(config, _secrets())
    comparison_step = next(s for s in report_with_local_state.steps if s.name == "balance_comparison")
    assert comparison_step.ok is True
    assert "diff 0" in comparison_step.detail


# --- Fail-closed behaviors. ---


@responses.activate
def test_excessive_clock_drift_fails_closed_before_any_signed_request(tmp_path):
    _mock_server_time(offset_ms=10_000_000)  # ~2.7 hours off the real clock
    report = run_testnet_health_check(_config(tmp_path), _secrets())
    assert report.passed is False
    clock_step = next(s for s in report.steps if s.name == "clock_sync")
    assert clock_step.ok is False
    # No signed request (account/open orders) was ever attempted.
    assert not any("api/v3/account" in call.request.url for call in responses.calls)
    assert not any("api/v3/openOrders" in call.request.url for call in responses.calls)


@responses.activate
def test_malformed_exchange_information_fails_closed(tmp_path):
    _mock_server_time()
    _mock_exchange_info(tick_size="0", step_size="0", min_qty="0", min_notional="0")
    report = run_testnet_health_check(_config(tmp_path), _secrets())
    assert report.passed is False
    exchange_info_step = next(s for s in report.steps if s.name == "exchange_info")
    assert exchange_info_step.ok is False
    # No signed request was ever attempted after a filter validation failure.
    assert not any("api/v3/account" in call.request.url for call in responses.calls)


@responses.activate
def test_invalid_credentials_produce_a_sanitized_failure(tmp_path):
    _mock_server_time()
    _mock_exchange_info()
    responses.add(
        responses.GET, f"{HOST}/api/v3/account",
        json={"code": -2015, "msg": "Invalid API-key, IP, or permissions for action."}, status=401,
    )
    report = run_testnet_health_check(_config(tmp_path), _secrets())
    assert report.passed is False
    account_step = next(s for s in report.steps if s.name == "account_info")
    assert account_step.ok is False
    assert "-2015" in account_step.detail
    assert "Invalid API-key" in account_step.detail
    assert SECRET_VALUE not in account_step.detail
