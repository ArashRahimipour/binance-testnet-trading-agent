"""Fault-injection tests proving `apply_order_result_atomically` is
genuinely atomic: a failure at any of the three former crash boundaries
(before any write, between the two writes, after both writes but before
commit) leaves the database completely unchanged, and a subsequent retry
from that same unchanged state applies the fill exactly once - never zero
times permanently, never twice.

This directly reproduces and then closes review round 2, finding #1: the
previous design (separate PortfolioStore + PendingOrdersStore, each
independently committed) had no way to prevent a crash between those two
commits from silently losing a fill. That failure mode is now
structurally impossible - both tables live behind one connection and one
transaction.
"""

import sqlite3
from decimal import Decimal

import pytest

from trading_agent.execution.order_outcome import InconsistentExecutionReportError
from trading_agent.execution.testnet_adapter import Fill, OrderResult
from trading_agent.persistence.execution_store import ExecutionStateStore
from trading_agent.portfolio.state import PortfolioState
from trading_agent.strategy.base import PositionSide

QUOTE_ASSET = "USDT"
BASE_ASSET = "BTC"


def _filled_order(client_order_id="ta-1", executed_qty="0.001", cumulative_quote="50.0", fills=None) -> OrderResult:
    return OrderResult(
        order_id=1,
        client_order_id=client_order_id,
        status="FILLED",
        executed_qty=Decimal(executed_qty),
        cumulative_quote_qty=Decimal(cumulative_quote),
        transact_time_ms=1,
        fills=fills or [Fill(Decimal(50000), Decimal(executed_qty), Decimal("0.05"), "USDT")],
        raw={},
    )


def _seed(store: ExecutionStateStore, symbol="BTCUSDT", quote="100") -> None:
    store.save_portfolio(symbol, PortfolioState.initial(Decimal(quote)), updated_at_ms=0)
    store.create_pending("ta-1", symbol, "BUY", Decimal("0.001"), signal_candle_close_time_ms=1, submitted_at_ms=2)


def test_normal_application_updates_both_tables_in_one_call(tmp_path):
    with ExecutionStateStore(tmp_path / "state.db") as store:
        _seed(store)
        store.apply_order_result_atomically("BTCUSDT", QUOTE_ASSET, BASE_ASSET, _filled_order(), Decimal("0.001"), now_ms=3)
        portfolio = store.load_portfolio("BTCUSDT")
        pending = store.get_pending("ta-1")
    assert portfolio.base_balance == Decimal("0.001")
    assert portfolio.position_side == PositionSide.LONG
    assert pending.status == "RESOLVED"
    assert pending.applied_executed_qty == Decimal("0.001")


@pytest.mark.parametrize("fault_point", ["before_write", "after_portfolio_write", "before_commit"])
def test_fault_at_every_former_crash_boundary_leaves_db_unchanged_then_retry_applies_once(tmp_path, fault_point):
    db_path = tmp_path / "state.db"
    with ExecutionStateStore(db_path) as store:
        _seed(store)
        store._raise_fault_at = fault_point  # test-only seam - see class docstring

        with pytest.raises(Exception):  # noqa: B017 - the injected fault itself, or a real one
            store.apply_order_result_atomically(
                "BTCUSDT", QUOTE_ASSET, BASE_ASSET, _filled_order(), Decimal("0.001"), now_ms=3
            )

        # --- Prove the rollback was total: re-read with a FRESH connection too. ---
        portfolio_after_fault = store.load_portfolio("BTCUSDT")
        pending_after_fault = store.get_pending("ta-1")
        assert portfolio_after_fault.base_balance == Decimal(0)  # never zero-then-corrupted, never partial
        assert portfolio_after_fault.position_side == PositionSide.FLAT
        assert pending_after_fault.status == "SUBMITTED"  # still pending, safely
        assert pending_after_fault.applied_executed_qty == Decimal(0)

    with ExecutionStateStore(db_path) as fresh_store:
        portfolio_fresh = fresh_store.load_portfolio("BTCUSDT")
        pending_fresh = fresh_store.get_pending("ta-1")
        assert portfolio_fresh.base_balance == Decimal(0)
        assert pending_fresh.applied_executed_qty == Decimal(0)

        # --- Retry (as a restarted process's reconciliation would) succeeds exactly once. ---
        fresh_store.apply_order_result_atomically(
            "BTCUSDT", QUOTE_ASSET, BASE_ASSET, _filled_order(), Decimal("0.001"), now_ms=4
        )
        portfolio_after_retry = fresh_store.load_portfolio("BTCUSDT")
        pending_after_retry = fresh_store.get_pending("ta-1")
    assert portfolio_after_retry.base_balance == Decimal("0.001")
    assert pending_after_retry.status == "RESOLVED"
    assert pending_after_retry.applied_executed_qty == Decimal("0.001")


def test_retry_after_successful_commit_never_double_applies(tmp_path):
    # Simulates re-running reconciliation against an order that was already
    # fully resolved - e.g. a duplicate startup reconciliation pass.
    with ExecutionStateStore(tmp_path / "state.db") as store:
        _seed(store)
        store.apply_order_result_atomically("BTCUSDT", QUOTE_ASSET, BASE_ASSET, _filled_order(), Decimal("0.001"), now_ms=3)
        # Same terminal order observed again (e.g. a second reconciliation pass
        # before the caller notices it's already RESOLVED and skips it).
        store.apply_order_result_atomically("BTCUSDT", QUOTE_ASSET, BASE_ASSET, _filled_order(), Decimal("0.001"), now_ms=5)
        portfolio = store.load_portfolio("BTCUSDT")
    assert portfolio.base_balance == Decimal("0.001")  # not 0.002 - the delta was zero the second time


def test_decreasing_executed_qty_is_rejected_and_rolled_back(tmp_path):
    with ExecutionStateStore(tmp_path / "state.db") as store:
        _seed(store)
        store.apply_order_result_atomically("BTCUSDT", QUOTE_ASSET, BASE_ASSET, _filled_order(), Decimal("0.001"), now_ms=3)

        bogus_order = _filled_order(executed_qty="0.0005", cumulative_quote="25.0")  # decreased!
        with pytest.raises(InconsistentExecutionReportError):
            store.apply_order_result_atomically("BTCUSDT", QUOTE_ASSET, BASE_ASSET, bogus_order, Decimal("0.001"), now_ms=6)

        portfolio = store.load_portfolio("BTCUSDT")
        pending = store.get_pending("ta-1")
    assert portfolio.base_balance == Decimal("0.001")  # unchanged by the rejected report
    assert pending.applied_executed_qty == Decimal("0.001")  # unchanged


def test_apply_without_pending_record_raises_and_writes_nothing(tmp_path):
    with ExecutionStateStore(tmp_path / "state.db") as store:
        store.save_portfolio("BTCUSDT", PortfolioState.initial(Decimal(100)), updated_at_ms=0)
        with pytest.raises(ValueError):
            store.apply_order_result_atomically(
                "BTCUSDT", QUOTE_ASSET, BASE_ASSET, _filled_order(), Decimal("0.001"), now_ms=3
            )
        assert store.load_portfolio("BTCUSDT").base_balance == Decimal(0)


def test_two_partial_fills_at_different_prices_produce_exact_cash_asset_and_avg_price(tmp_path):
    # Fill 1: 0.0004 BTC @ 50000 = 20.0 quote. Fill 2: 0.0006 BTC @ 60000 = 36.0 quote.
    # Total: 0.001 BTC for 56.0 quote -> exact avg price = 56.0/0.001 = 56000.
    # A proportional-estimate approach (splitting the total 56.0 by qty
    # fraction instead of using the exact per-observation cumulative delta)
    # would still get this right by coincidence for two fills seen in full,
    # but fails once fills are reconciled incrementally with only cumulative
    # totals available - which is exactly the scenario tested here: each
    # reconciliation pass sees only Binance's cumulative fields, no
    # per-fill breakdown, and the deltas must still be exact.
    with ExecutionStateStore(tmp_path / "state.db") as store:
        _seed(store, quote="100")
        first = OrderResult(
            1, "ta-1", "PARTIALLY_FILLED", Decimal("0.0004"), Decimal("20.0"), 1,
            fills=[Fill(Decimal(50000), Decimal("0.0004"), Decimal(0), "USDT")], raw={},
        )
        store.apply_order_result_atomically("BTCUSDT", QUOTE_ASSET, BASE_ASSET, first, Decimal(0), now_ms=3)

        # Second reconciliation pass: only cumulative fields available (no fills).
        second = OrderResult(1, "ta-1", "FILLED", Decimal("0.001"), Decimal("56.0"), 1, fills=[], raw={})
        store.apply_order_result_atomically("BTCUSDT", QUOTE_ASSET, BASE_ASSET, second, Decimal(0), now_ms=4)

        portfolio = store.load_portfolio("BTCUSDT")
    assert portfolio.base_balance == Decimal("0.001")
    assert portfolio.quote_balance == Decimal(100) - Decimal("56.0")
    assert portfolio.avg_entry_price == Decimal(56000)


def test_two_partial_fills_accumulate_via_applied_cumulative_fields(tmp_path):
    with ExecutionStateStore(tmp_path / "state.db") as store:
        _seed(store)
        first = OrderResult(
            1, "ta-1", "PARTIALLY_FILLED", Decimal("0.0005"), Decimal("25.0"), 1,
            fills=[Fill(Decimal(50000), Decimal("0.0005"), Decimal("0.025"), "USDT")], raw={},
        )
        store.apply_order_result_atomically("BTCUSDT", QUOTE_ASSET, BASE_ASSET, first, Decimal("0.001"), now_ms=3)

        second = OrderResult(1, "ta-1", "FILLED", Decimal("0.001"), Decimal("51.0"), 1, fills=[], raw={})
        store.apply_order_result_atomically("BTCUSDT", QUOTE_ASSET, BASE_ASSET, second, Decimal("0.001"), now_ms=4)

        portfolio = store.load_portfolio("BTCUSDT")
        pending = store.get_pending("ta-1")
    assert portfolio.base_balance == Decimal("0.001")
    assert pending.applied_cumulative_quote_qty == Decimal("51.0")
    assert pending.status == "RESOLVED"


# --- open_read_only: used by execution/testnet_health.py. ---


def test_open_read_only_returns_none_and_creates_nothing_when_file_absent(tmp_path):
    db_path = tmp_path / "does_not_exist.db"
    store = ExecutionStateStore.open_read_only(db_path)
    assert store is None
    assert not db_path.exists()


def test_open_read_only_reads_existing_data_without_creating_schema_side_effects(tmp_path):
    db_path = tmp_path / "state.db"
    with ExecutionStateStore(db_path) as store:
        _seed(store)
        store.apply_order_result_atomically("BTCUSDT", QUOTE_ASSET, BASE_ASSET, _filled_order(), Decimal("0.001"), now_ms=3)

    with ExecutionStateStore.open_read_only(db_path) as ro_store:
        portfolio = ro_store.load_portfolio("BTCUSDT")
        pending = ro_store.get_pending("ta-1")
    assert portfolio.base_balance == Decimal("0.001")
    assert pending.status == "RESOLVED"


def test_open_read_only_connection_rejects_writes(tmp_path):
    db_path = tmp_path / "state.db"
    with ExecutionStateStore(db_path) as store:
        _seed(store)

    with ExecutionStateStore.open_read_only(db_path) as ro_store, pytest.raises(sqlite3.OperationalError):
        ro_store.save_portfolio("BTCUSDT", PortfolioState.initial(Decimal(999)), updated_at_ms=99)

    # Unaffected by the rejected write attempt.
    with ExecutionStateStore(db_path) as store:
        assert store.load_portfolio("BTCUSDT").quote_balance == Decimal(100)


def test_open_read_only_missing_tables_raise_rather_than_silently_create(tmp_path):
    db_path = tmp_path / "empty.db"
    sqlite3.connect(str(db_path)).close()  # an existing file, but no schema at all
    with ExecutionStateStore.open_read_only(db_path) as ro_store, pytest.raises(sqlite3.OperationalError):
        ro_store.load_portfolio("BTCUSDT")
