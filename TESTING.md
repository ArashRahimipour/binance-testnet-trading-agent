# Testing

282 tests, all offline: nothing in the suite makes a real network call or
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
| Crash recovery / exactly-once fill application, atomic across BOTH tables | `tests/unit/test_execution_store.py` (fault injection at all three former crash boundaries, retry-after-commit never double-applies, decreasing-value rejection), `tests/unit/test_startup_reconciliation.py`, `tests/unit/test_live_runner.py::test_pending_order_from_previous_crashed_run_is_resolved_before_new_signal`, `::test_still_open_pending_order_blocks_new_signal_this_cycle` |
| Reconciliation mismatch blocks ALL orders, not just BUY | `tests/unit/test_risk_engine.py::test_reconciliation_blocked_blocks_both_buy_and_exit`, `test_live_runner.py::test_balance_discrepancy_blocks_sell_too`, `::test_local_base_balance_exceeding_exchange_blocks_sell`, `::test_exchange_base_balance_exceeding_local_blocks_sell` |
| Exact partial-fill delta accounting across cumulative fields | `tests/unit/test_execution_store.py::test_two_partial_fills_at_different_prices_produce_exact_cash_asset_and_avg_price`, `test_order_outcome.py::test_decreasing_executed_qty_is_rejected_not_silently_applied` |
| Continuous balance reconciliation (free+locked) | `tests/unit/test_startup_reconciliation.py::test_reconcile_balances_*`, `test_live_runner.py::test_balance_discrepancy_blocks_sell_too` |
| Fee accounting (per-fill commission, entry+exit netting, commission-asset-aware) | `tests/unit/test_fees.py`, `tests/unit/test_portfolio_state.py::test_realized_pnl_nets_out_entry_fee_on_full_close`, `::test_realized_pnl_allocates_entry_fee_proportionally_on_partial_close`, `test_order_outcome.py::test_base_asset_commission_on_buy_reduces_received_quantity`, `::test_third_asset_commission_is_recorded_but_never_touches_quote_or_base` |
| No same-close backtest execution | `tests/unit/test_backtest_engine.py::test_changing_next_candle_open_changes_the_fill_price`, `::test_no_trade_fills_at_the_signal_candles_own_close`, `::test_signal_on_final_candle_is_reported_as_unexecuted_not_filled` |
| Backtest UTC-day boundary ordering | `tests/unit/test_backtest_engine.py::test_exit_resolved_on_first_candle_of_new_utc_day_is_attributed_to_new_day`, `::test_losing_stop_at_first_candle_of_new_utc_day_is_attributed_to_new_day` |
| Cost-aware, risk-budget stop-loss position sizing | `tests/unit/test_risk_based_position_sizer.py`, `test_backtest_engine.py::test_ordinary_stop_hit_stays_within_risk_budget`, `::test_gap_through_stop_can_exceed_risk_budget`, `::test_risk_budget_sizing_produces_smaller_position_for_smaller_risk_pct` |
| Server-time sync / clock drift | `tests/unit/test_testnet_adapter.py::test_sync_time_*`, `::test_timestamp_outside_recv_window_error_propagates`, `test_live_runner.py::test_excessive_clock_drift_fails_closed` |
| Historical-data pagination, retries, rate limits, dedup | `tests/unit/test_historical_fetch.py` |
| `testnet-health`: GET-only, no order-placement capability reachable | `tests/unit/test_testnet_health.py::test_happy_path_passes_and_only_get_requests_occur`, `::test_testnet_health_module_never_references_place_market_order`, `::test_testnet_readonly_module_never_references_place_market_order`, `test_testnet_readonly.py::test_has_no_order_placing_method_at_all`, `::test_no_request_is_ever_a_post_put_patch_or_delete` |
| `testnet-health`: no local state created or modified | `tests/unit/test_testnet_health.py::test_no_database_or_local_state_file_created_when_none_exists`, `::test_existing_local_state_is_not_modified`, `::test_pending_orders_reported_but_not_reconciled`, `test_execution_store.py::test_open_read_only_returns_none_and_creates_nothing_when_file_absent`, `::test_open_read_only_connection_rejects_writes` |
| `testnet-health`: secrets/signatures never leak | `tests/unit/test_testnet_health.py::test_secret_never_appears_in_report_on_happy_path`, `::test_secret_never_appears_on_invalid_credentials_failure`, `::test_secret_never_appears_in_cli_stdout_or_stderr` |
| `testnet-health`: balance/open-order reporting, fail-closed behavior | `tests/unit/test_testnet_health.py::test_nonzero_btc_balance_is_reported_as_information_not_a_failure`, `::test_locked_balances_are_reported`, `::test_open_orders_are_displayed_without_modification`, `::test_excessive_clock_drift_fails_closed_before_any_signed_request`, `::test_malformed_exchange_information_fails_closed`, `::test_invalid_credentials_produce_a_sanitized_failure` |

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
  one CLI invocation at a time (see ARCHITECTURE.md). Automatic scheduling
  is not implemented at all yet - see
  [SCHEDULING_DESIGN.md](SCHEDULING_DESIGN.md) for the overlap-guard
  design required before it can be, which therefore also has no tests.
- A Testnet-native protective order (OCO / `STOP_LOSS_LIMIT`) - not
  implemented at all in this revision, so there is nothing to test; see
  RISK_POLICY.md's "Protective exits" section for why, and what would need
  to be verified against a live Testnet before it could be enabled.
