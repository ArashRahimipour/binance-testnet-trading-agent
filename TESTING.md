# Testing

233 tests, all offline: nothing in the suite makes a real network call or
requires real credentials. HTTP is mocked with the `responses` library at
the point where `requests` would otherwise reach the network; time-
sensitive behavior is tested by passing explicit reference timestamps
rather than depending on the real clock.

```bash
pytest              # run everything
pytest tests/unit    # unit tests only
pytest tests/integration   # source-level / cross-module proofs
ruff check src tests       # lint
```

## Coverage by required category

| Requirement | Where it's tested |
|---|---|
| Incomplete-candle exclusion | `tests/unit/test_data_ingestion.py::test_incomplete_last_candle_is_excluded` |
| Stale data | `tests/unit/test_data_validation.py::test_stale_data_rejected`, `tests/unit/test_live_runner.py::test_stale_data_blocks_trade_without_clock_drift` |
| Missing data | `tests/unit/test_data_ingestion.py::test_require_non_empty_raises_on_empty_list`, `test_data_validation.py::test_empty_series_rejected` |
| Duplicate candles | `tests/unit/test_data_validation.py::test_duplicate_open_time_rejected`, `test_data_storage.py::test_upsert_is_idempotent_on_conflict` |
| Signal determinism | `tests/unit/test_strategy_trend_baseline.py::test_determinism_same_inputs_same_output` |
| No look-ahead | `tests/unit/test_indicators.py::test_ema_is_causal`, `test_strategy_trend_baseline.py::test_no_lookahead_earlier_signal_unaffected_by_future_candles` |
| Position sizing | `tests/unit/test_position_sizer.py` (all cases) |
| Fee calculations | `tests/unit/test_backtest_broker.py::test_fee_is_applied_to_notional_at_fill_price` |
| Minimum-notional rejection | `tests/unit/test_position_sizer.py::test_buy_sizing_rejects_when_below_min_notional_never_bumps_up`, `test_order_validator.py::test_rejects_below_min_notional_independently_of_sizer` |
| Lot-size / tick-size rounding | `tests/unit/test_exchange_filters.py` (rounding always down, never up) |
| Daily-loss shutdown | `tests/unit/test_risk_engine.py::test_max_daily_loss_shutdown_blocks_buy_but_not_exit` |
| Drawdown shutdown | `tests/unit/test_risk_engine.py::test_max_drawdown_shutdown_blocks_buy_but_not_exit` |
| Kill switch | `tests/unit/test_kill_switch.py`, `test_risk_engine.py::test_kill_switch_blocks_buy_and_exit`, `test_live_runner.py::test_kill_switch_blocks_exit` |
| Duplicate-order blocking | `tests/unit/test_risk_engine.py::test_duplicate_order_blocked`, `test_live_runner.py::test_duplicate_order_already_journaled_blocks_resubmission` |
| Timeout / uncertain-order reconciliation | `tests/unit/test_reconciliation.py`, `test_live_runner.py::test_timeout_then_not_found_retries_and_succeeds` |
| Ambiguous non-timeout network failures (connection reset, etc.) | `tests/unit/test_live_runner.py::test_connection_error_is_treated_as_ambiguous_and_reconciled_before_retry` |
| Insufficient balance | `tests/unit/test_portfolio_state.py::test_apply_buy_rejects_cost_exceeding_balance`, `test_apply_sell_rejects_quantity_above_held_balance` |
| Unsupported execution mode rejected | `tests/unit/test_cli.py::test_mode_live_is_rejected_at_cli_parsing`, `test_mode_production_is_rejected_at_cli_parsing`; `tests/unit/test_config.py::test_mode_rejects_live` |
| Proof production endpoints cannot be selected | `tests/integration/test_no_production_endpoints.py` - scans the actual package source for production hostnames outside the one approved read-only client |
| Order status handling (every Binance status) | `tests/unit/test_order_outcome.py` - one test per NEW/PARTIALLY_FILLED/FILLED/CANCELED/REJECTED/EXPIRED, including zero- and partial-execution cases, and that the requested quantity is never substituted |
| Crash recovery / exactly-once fill application | `tests/unit/test_startup_reconciliation.py`, `tests/unit/test_pending_orders_store.py`, `tests/unit/test_live_runner.py::test_pending_order_from_previous_crashed_run_is_resolved_before_new_signal`, `::test_still_open_pending_order_blocks_new_signal_this_cycle` |
| Continuous balance reconciliation (free+locked) | `tests/unit/test_startup_reconciliation.py::test_reconcile_balances_*`, `test_live_runner.py::test_balance_discrepancy_blocks_new_entries_but_exit_still_allowed` |
| Fee accounting (per-fill commission, entry+exit netting) | `tests/unit/test_fees.py`, `tests/unit/test_portfolio_state.py::test_realized_pnl_nets_out_entry_fee_on_full_close`, `::test_realized_pnl_allocates_entry_fee_proportionally_on_partial_close` |
| No same-close backtest execution | `tests/unit/test_backtest_engine.py::test_changing_next_candle_open_changes_the_fill_price`, `::test_no_trade_fills_at_the_signal_candles_own_close`, `::test_signal_on_final_candle_is_reported_as_unexecuted_not_filled` |
| Risk-budget stop-loss position sizing | `tests/unit/test_risk_based_position_sizer.py`, `test_backtest_engine.py::test_stop_loss_closes_position_when_low_breaches_stop`, `::test_risk_budget_sizing_produces_smaller_position_for_smaller_risk_pct` |
| Server-time sync / clock drift | `tests/unit/test_testnet_adapter.py::test_sync_time_*`, `::test_timestamp_outside_recv_window_error_propagates`, `test_live_runner.py::test_excessive_clock_drift_fails_closed` |
| Historical-data pagination, retries, rate limits, dedup | `tests/unit/test_historical_fetch.py` |

## Fixtures

- `tests/fixtures/klines.py` - synthetic Binance kline rows
- `tests/fixtures/exchange_info.py` - a representative `exchangeInfo` response, parameterizable per test

## What is intentionally not covered

- Real Testnet network behavior (rate limiting quirks, exact matching
  engine behavior) - by definition untestable without live network access,
  and out of scope for a suite that must run offline with no credentials.
  This is a real gap: the mocked adapter tests prove the code *would*
  behave correctly given a certain HTTP response, not that the Testnet
  actually returns that response in every case.
- Concurrency / multi-process access to the SQLite files - V0.1 assumes
  one CLI invocation at a time (see ARCHITECTURE.md).
- A Testnet-native protective order (OCO / `STOP_LOSS_LIMIT`) - not
  implemented at all in this revision, so there is nothing to test; see
  RISK_POLICY.md's "Protective exits" section for why, and what would need
  to be verified against a live Testnet before it could be enabled.
