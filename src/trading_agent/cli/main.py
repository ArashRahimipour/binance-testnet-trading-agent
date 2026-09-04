"""Command-line entry point.

The `--mode` option is a `click.Choice` restricted to the two Mode enum
values ("backtest", "testnet"). There is no "live" choice and no way to pass
one - click rejects any other value before any application code runs.
"""

from __future__ import annotations

import sys
import time
from datetime import UTC, datetime

import click

from trading_agent.backtest.engine import (
    RunDiagnostics,
    ShutdownActivation,
    run_backtest,
    run_independent_holdout_evaluation,
)
from trading_agent.config import AppConfig, Mode, load_config
from trading_agent.config.loader import load_secrets
from trading_agent.data.historical_fetch import (
    DEFAULT_MAX_RETRIES,
    confirm_gaps,
    fetch_historical_range,
)
from trading_agent.data.ingestion import fetch_completed_candles, require_non_empty
from trading_agent.data.market_data_public import (
    PRODUCTION_MARKET_DATA_HOST,
    BinancePublicMarketDataClient,
)
from trading_agent.data.storage import CandleStore
from trading_agent.execution.live_runner import ColdStartReconciliationError, run_testnet_cycle
from trading_agent.execution.testnet_health import run_testnet_health_check
from trading_agent.journal.journal import Journal
from trading_agent.metrics.extended_report import ExtendedDiagnosticsReport
from trading_agent.persistence.execution_store import ExecutionStateStore
from trading_agent.persistence.risk_state_store import RiskStateStore
from trading_agent.risk.kill_switch import KillSwitch
from trading_agent.sizing.exchange_filters import SymbolFilters


@click.group()
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="Path to a YAML config file (defaults to config/default.yaml).",
)
@click.option(
    "--mode",
    "mode",
    type=click.Choice([m.value for m in Mode]),
    default=None,
    help="Override the execution mode from the config file.",
)
@click.pass_context
def cli(ctx: click.Context, config_path: str | None, mode: str | None) -> None:
    """Binance Spot Testnet trading agent (V0.1) - backtest and testnet only."""
    overrides = {"mode": mode} if mode else None
    try:
        config: AppConfig = load_config(config_path, overrides=overrides)
    except Exception as exc:  # noqa: BLE001 - surfaced to the user, then exit
        click.echo(f"Failed to load config: {exc}", err=True)
        sys.exit(1)
    ctx.ensure_object(dict)
    ctx.obj["config"] = config


@cli.command("config-check")
@click.pass_context
def config_check(ctx: click.Context) -> None:
    """Validate the configuration and print a summary (no secrets)."""
    config: AppConfig = ctx.obj["config"]
    click.echo(f"mode: {config.mode.value}")
    click.echo(f"symbol: {config.market.symbol}  interval: {config.market.interval}")
    click.echo(
        f"strategy: {config.strategy.name} "
        f"(ema_fast={config.strategy.ema_fast}, ema_slow={config.strategy.ema_slow})"
    )
    click.echo(f"risk.max_drawdown_pct: {config.risk.max_drawdown_pct}")
    click.echo("Configuration is valid.")


@cli.command("fetch-data")
@click.option("--limit", default=1000, show_default=True, help="Number of most recent completed candles to fetch (ignored if --start is given).")
@click.option("--start", "start_str", default=None, help="ISO8601 UTC start date, e.g. 2020-01-01 - triggers a paginated multi-year-capable download.")
@click.option("--end", "end_str", default=None, help="ISO8601 UTC end date (default: now). Only used with --start.")
@click.option("--max-retries", default=DEFAULT_MAX_RETRIES, show_default=True, help="Max retries per page (and per gap-recovery attempt) before giving up.")
@click.pass_context
def fetch_data(ctx: click.Context, limit: int, start_str: str | None, end_str: str | None, max_retries: int) -> None:
    """Fetch historical candles from the public read-only market-data host and store them.

    Without --start, fetches the most recent `--limit` completed candles in
    a single request. With --start (optionally --end), pages through the
    full date range - suitable for downloading multiple years of history.

    A CONFIRMED historical gap - a real, permanent hole in the exchange's
    own record, never fabricated or interpolated over - does not abort the
    download. Every gap is detected, given one focused narrow-range retry
    to rule out a pagination artifact or a transient API response, and (if
    still missing) recorded in a durable gap manifest alongside every
    valid candle around it. See ARCHITECTURE.md and
    `config.backtest.gap_policy` for how the backtest engine uses this.
    """
    config: AppConfig = ctx.obj["config"]
    client = BinancePublicMarketDataClient(PRODUCTION_MARKET_DATA_HOST)
    try:
        if start_str:
            start_ms = int(datetime.strptime(start_str, "%Y-%m-%d").replace(tzinfo=UTC).timestamp() * 1000)
            end_ms = (
                int(datetime.strptime(end_str, "%Y-%m-%d").replace(tzinfo=UTC).timestamp() * 1000)
                if end_str
                else int(time.time() * 1000)
            )
            fetch_result = fetch_historical_range(
                client, config.market.symbol, config.market.interval, start_ms, end_ms, max_retries=max_retries
            )
        else:
            candles = fetch_completed_candles(client, config.market.symbol, config.market.interval, limit=limit)
            require_non_empty(candles)
            fetch_result = confirm_gaps(
                client, config.market.symbol, config.market.interval, candles, max_retries=max_retries
            )
        require_non_empty(fetch_result.candles)
    except Exception as exc:  # noqa: BLE001 - surfaced to the user, then exit
        click.echo(f"Failed to fetch valid candle data: {exc}", err=True)
        sys.exit(1)

    with CandleStore(config.paths.db_path) as store:
        store.store_candles_and_gaps(
            fetch_result.candles, fetch_result.confirmed_gaps,
            config.market.symbol, config.market.interval, detected_at_ms=int(time.time() * 1000),
        )

    gap_count = len(fetch_result.confirmed_gaps)
    gap_word = "gap" if gap_count == 1 else "gaps"
    click.echo(
        f"Stored {len(fetch_result.candles)} completed candles with {gap_count} confirmed historical "
        f"{gap_word}. No candles were fabricated."
    )
    for gap in fetch_result.confirmed_gaps:
        click.echo(
            f"  confirmed gap: expected_open_time_ms={gap.expected_open_time_ms} "
            f"previous_open_time_ms={gap.previous_open_time_ms} next_open_time_ms={gap.next_open_time_ms} "
            f"missing_intervals={gap.missing_intervals}"
        )


@cli.command("backtest")
@click.pass_context
def backtest_cmd(ctx: click.Context) -> None:
    """Run the backtest engine over previously fetched candles."""
    config: AppConfig = ctx.obj["config"]
    if config.mode != Mode.BACKTEST:
        click.echo("The backtest command requires --mode backtest.", err=True)
        sys.exit(1)

    with CandleStore(config.paths.db_path) as store:
        candles = store.get_candles(config.market.symbol, config.market.interval)
    if not candles:
        click.echo("No candles found. Run `fetch-data` first.", err=True)
        sys.exit(1)

    client = BinancePublicMarketDataClient(PRODUCTION_MARKET_DATA_HOST)
    try:
        filters = SymbolFilters.from_exchange_info(client.get_exchange_info(config.market.symbol))
    except Exception as exc:  # noqa: BLE001 - surfaced to the user, then exit
        click.echo(f"Failed to fetch exchange filters: {exc}", err=True)
        sys.exit(1)

    journal_path = config.paths.data_dir / "journal.db"
    with Journal(journal_path) as journal:
        try:
            result = run_backtest(candles, config, filters, journal)
            holdout = run_independent_holdout_evaluation(candles, config, filters, journal)
        except ValueError as exc:
            click.echo(f"Backtest failed: {exc}", err=True)
            sys.exit(1)

    click.echo("=== CONTINUOUS OPERATIONAL SIMULATION ===")
    click.echo(
        f"gap_policy={config.backtest.gap_policy}  "
        f"segments={len(result.segments)}  confirmed_gaps={len(result.gaps)}  "
        f"starting_equity={config.backtest.starting_equity}"
    )
    for seg in result.segments:
        status_bits = []
        if seg.skipped_insufficient_candles:
            status_bits.append("skipped - too few candles for indicator warm-up")
        if seg.ends_with_open_position:
            status_bits.append("ends with an open position (unresolved research condition)")
        if seg.excluded_from_overall:
            status_bits.append("EXCLUDED from aggregate stats")
        status = f"  [{'; '.join(status_bits)}]" if status_bits else ""
        start_date = datetime.fromtimestamp(seg.start_time_ms / 1000, tz=UTC).date()
        end_date = datetime.fromtimestamp(seg.end_time_ms / 1000, tz=UTC).date()
        click.echo(
            f"  segment {seg.index}: {start_date} to {end_date} "
            f"({seg.candle_count} candles, {seg.trade_count} trade(s)){status}"
        )
        if seg.performance is not None:
            report = seg.performance
            click.echo(
                f"    starting_equity={report.starting_equity} ending_equity={report.ending_equity} "
                f"total_return_pct={report.total_return_pct:.2f} max_drawdown_pct={report.max_drawdown_pct:.2f} "
                f"strategy_exits={report.strategy_exit_count} stop_loss_exits={report.stop_loss_exit_count}"
            )
            if report.buy_and_hold is not None:
                bh = report.buy_and_hold
                click.echo(
                    f"    buy_and_hold: return_pct={bh.return_pct} max_drawdown_pct={bh.max_drawdown_pct} "
                    f"fee_pct_applied={bh.buy_fee_pct_applied}"
                )
        if seg.diagnostics is not None:
            _print_diagnostics(seg.diagnostics, indent="    ")
        if seg.extended is not None:
            _print_extended_diagnostics(seg.extended, indent="    ")
    if result.gaps:
        click.echo(
            "WARNING: results across gaps are NOT one continuous tradable equity history - "
            "each segment above starts fresh from the same baseline starting_equity; read each "
            "segment's own report above independently."
        )

    if result.reports:
        # Exactly one segment ran (the no-confirmed-gap case): the familiar
        # chronological train/validation/test/overall labels on that ONE
        # continuous run - see backtest/engine.py's module docstring for
        # why these are informational timeline slices, not independent
        # evaluations (use the holdout evaluation below for that).
        for split in ("train", "validation", "test", "overall"):
            report = result.reports[split]
            click.echo(f"--- {split} (continuous run) ---")
            click.echo(
                f"trades={report.trade_count} starting_equity={report.starting_equity} "
                f"ending_equity={report.ending_equity} total_return_pct={report.total_return_pct:.2f} "
                f"buy_and_hold_pct={report.buy_and_hold_return_pct} max_drawdown_pct={report.max_drawdown_pct:.2f}"
            )
            if report.buy_and_hold is not None:
                click.echo(
                    f"buy_and_hold_max_drawdown_pct={report.buy_and_hold.max_drawdown_pct} "
                    f"buy_and_hold_fee_pct_applied={report.buy_and_hold.buy_fee_pct_applied}"
                )
            click.echo(
                f"sharpe={report.sharpe_ratio} sortino={report.sortino_ratio} "
                f"win_rate={report.win_rate} profit_factor={report.profit_factor} "
                f"exposure_pct={report.exposure_pct:.1f} turnover={report.turnover:.2f} "
                f"strategy_exits={report.strategy_exit_count} stop_loss_exits={report.stop_loss_exit_count}"
            )
            if split in result.extended_reports:
                _print_extended_diagnostics(result.extended_reports[split], indent="")
        if result.diagnostics is not None:
            click.echo("--- diagnostics (continuous run, whole segment) ---")
            _print_diagnostics(result.diagnostics, indent="")
    elif result.aggregate_trade_stats is not None:
        agg = result.aggregate_trade_stats
        click.echo("--- aggregate_trade_stats (trade-level ONLY, see note) ---")
        click.echo(
            f"segments_included={agg.segments_included} total_trades={agg.total_trades} "
            f"total_realized_pnl_quote={agg.total_realized_pnl_quote} win_rate={agg.win_rate} "
            f"total_strategy_exits={agg.total_strategy_exits} total_stop_loss_exits={agg.total_stop_loss_exits}"
        )
        click.echo(f"NOTE: {agg.note}")

    for warning in result.warnings:
        click.echo(f"WARNING: {warning}", err=True)

    click.echo("")
    click.echo(f"=== {holdout.label} ===")
    for window in holdout.windows:
        start_date = datetime.fromtimestamp(window.window_start_time_ms / 1000, tz=UTC).date()
        end_date = datetime.fromtimestamp(window.window_end_time_ms / 1000, tz=UTC).date()
        warmup_date = datetime.fromtimestamp(window.warm_up_start_time_ms / 1000, tz=UTC).date()
        report = window.performance
        click.echo(
            f"  segment {window.segment_index} {window.label}: {start_date} to {end_date} "
            f"({window.candle_count} candles; warm-up from {warmup_date}, "
            f"{window.warm_up_candle_count} candle(s), never traded)"
        )
        click.echo(
            f"    trades={report.trade_count} starting_equity={report.starting_equity} "
            f"ending_equity={report.ending_equity} total_return_pct={report.total_return_pct:.2f} "
            f"max_drawdown_pct={report.max_drawdown_pct:.2f} "
            f"buy_and_hold_pct={report.buy_and_hold_return_pct}"
        )
        if window.ends_with_open_position or window.unresolved_pending_signal:
            click.echo(
                "    NOTE: window ends with an open position or unresolved pending signal - "
                "never carried into the next window, no exit price invented."
            )
        _print_diagnostics(window.diagnostics, indent="    ")
        _print_extended_diagnostics(window.extended, indent="    ")
    for warning in holdout.warnings:
        click.echo(f"WARNING (holdout evaluation): {warning}", err=True)

    click.echo(
        "\nNote: this is a research backtest on simulated fills, not a claim of live profitability. "
        "Do not select a strategy based solely on the best historical return."
    )


def _print_diagnostics(diagnostics: RunDiagnostics, indent: str) -> None:
    d = diagnostics
    click.echo(
        f"{indent}signals: buy={d.buy_signals_generated} exit={d.exit_signals_generated} "
        f"unexecuted={d.unexecuted_signals}"
    )
    click.echo(
        f"{indent}executed: entries={d.executed_entries} strategy_exits={d.executed_strategy_exits} "
        f"stop_loss_exits={d.executed_stop_loss_exits}"
    )
    if d.rejected_entries_by_reason:
        reasons = ", ".join(f"{code}={count}" for code, count in sorted(d.rejected_entries_by_reason.items()))
        click.echo(f"{indent}rejected_entries_by_reason: {reasons}")
    click.echo(
        f"{indent}first_executed_trade_time_ms={d.first_executed_trade_time_ms} "
        f"last_executed_trade_time_ms={d.last_executed_trade_time_ms}"
    )
    click.echo(f"{indent}max_drawdown_pct={d.max_drawdown_pct:.2f} at time_ms={d.max_drawdown_time_ms}")
    for activation in d.shutdown_activations.values():
        _print_shutdown_activation(activation, indent)
    click.echo(
        f"{indent}ending: cash_quote={d.ending_cash_quote} base_quantity={d.ending_base_quantity} "
        f"equity={d.ending_equity} open_position={d.ends_with_open_position}"
    )


def _print_shutdown_activation(activation: ShutdownActivation, indent: str) -> None:
    click.echo(
        f"{indent}SHUTDOWN {activation.reason_code}: first_activated_time_ms="
        f"{activation.first_activated_time_ms} equity_at_activation={activation.equity_at_activation} "
        f"drawdown_pct_at_activation={activation.drawdown_pct_at_activation * 100:.2f} "
        f"blocked_buy_count={activation.blocked_buy_count} duration_ms={activation.duration_ms} "
        f"remained_latched_to_end={activation.remained_latched_to_end}"
    )


def _print_extended_diagnostics(extended: ExtendedDiagnosticsReport, indent: str) -> None:
    acc = extended.accounting
    click.echo(
        f"{indent}accounting_identity: ending_cash={acc.ending_cash_quote} + "
        f"ending_base_quantity={acc.ending_base_quantity} * final_mark_price={acc.final_mark_price} "
        f"= computed_ending_equity={acc.computed_ending_equity} "
        f"(reported_ending_equity={acc.reported_ending_equity}, holds={acc.identity_holds})"
    )

    pnl = extended.pnl_breakdown
    click.echo(
        f"{indent}pnl: realized_closed_trade={pnl.realized_closed_trade_pnl_quote} "
        f"unrealized_open_position={pnl.unrealized_open_position_pnl_quote} "
        f"total_marked_to_market={pnl.total_marked_to_market_pnl_quote}"
    )
    click.echo(
        f"{indent}fees: entry_total={pnl.entry_fees_total_quote} exit_total={pnl.exit_fees_total_quote} "
        f"estimated={pnl.fees_are_estimated}  slippage_cost_total={pnl.slippage_cost_total_quote}"
    )
    if pnl.open_position_note:
        click.echo(f"{indent}NOTE (open position): {pnl.open_position_note}")

    expl = extended.explanation
    if expl.open_position_reason:
        click.echo(f"{indent}WHY open position: {expl.open_position_reason}")
    click.echo(f"{indent}{expl.entries_vs_closed_trades_note}")
    if expl.trading_stopped_reason:
        click.echo(f"{indent}WHY trading stopped: {expl.trading_stopped_reason}")
    if extended.scope_note:
        click.echo(f"{indent}SCOPE NOTE: {extended.scope_note}")

    tb = extended.time_based
    click.echo(
        f"{indent}time_based: cagr_pct={tb.cagr_pct} positive_months_pct={tb.positive_months_pct} "
        f"longest_underwater_days={tb.longest_underwater_days} "
        f"exposure_adjusted_return_pct={tb.exposure_adjusted_return_pct} calmar_ratio={tb.calmar_ratio}"
    )
    if tb.monthly_returns:
        months = ", ".join(f"{m.year}-{m.month:02d}={m.return_pct:.2f}%" for m in tb.monthly_returns)
        click.echo(f"{indent}monthly_returns: {months}")

    td = extended.trade_distribution
    if td is not None:
        click.echo(
            f"{indent}trade_distribution: median_return_pct={td.median_trade_return_pct} "
            f"avg_winner={td.average_winner_quote} avg_loser={td.average_loser_quote} "
            f"largest_winner={td.largest_winner_quote} largest_loser={td.largest_loser_quote}"
        )
        click.echo(
            f"{indent}best_trade: pnl={td.best_trade_pnl_quote} "
            f"contribution_pct={td.best_trade_contribution_pct} "
            f"result_excluding_best_trade_quote={td.total_pnl_excluding_best_trade_quote} "
            f"return_pct_excluding_best_trade={td.return_pct_excluding_best_trade}"
        )
        click.echo(
            f"{indent}streaks: max_consecutive_wins={td.max_consecutive_wins} "
            f"max_consecutive_losses={td.max_consecutive_losses}  "
            f"holding_period_hours: min={td.holding_period.min_hours} "
            f"median={td.holding_period.median_hours} mean={td.holding_period.mean_hours} "
            f"max={td.holding_period.max_hours}"
        )

    bs = extended.bootstrap
    click.echo(
        f"{indent}bootstrap_ci (n_trades={bs.n_trades}, n_resamples={bs.n_resamples}, seed={bs.seed}, "
        f"confidence={bs.confidence_level}): mean_return_pct={bs.mean_total_return_pct} "
        f"ci=[{bs.ci_low_pct}, {bs.ci_high_pct}]"
    )
    click.echo(f"{indent}CAVEAT: {bs.caveat}")

    rw = extended.rolling_windows
    if rw.windows:
        windows_str = ", ".join(
            f"#{w.window_index}(n={w.trade_count}, return_pct={w.return_pct:.2f}, "
            f"max_dd_pct={w.max_drawdown_pct:.2f})"
            for w in rw.windows
        )
        click.echo(f"{indent}rolling_windows (trades_per_window={rw.trades_per_window}): {windows_str}")
        click.echo(f"{indent}{rw.caveat}")

    if extended.already_consumed_warning:
        click.echo(f"{indent}WARNING: {extended.already_consumed_warning}")


@cli.command("run")
@click.pass_context
def run_cmd(ctx: click.Context) -> None:
    """Run a single testnet decision cycle (intended to be invoked once per completed candle).

    Testnet operation is OBSERVATIONAL: every cycle evaluates HOLD normally,
    but a BUY signal is always suppressed - this agent cannot initiate a
    position on Testnet. SELL exists only to close (or help recover) a
    position that already exists and has been fully reconciled against the
    exchange; it is not a general trading path. See RISK_POLICY.md.
    """
    config: AppConfig = ctx.obj["config"]
    if config.mode != Mode.TESTNET:
        click.echo("The run command requires --mode testnet.", err=True)
        sys.exit(1)
    try:
        secrets = load_secrets()
    except Exception as exc:  # noqa: BLE001 - surfaced to the user, then exit
        click.echo(f"Failed to load testnet credentials: {exc}", err=True)
        sys.exit(1)

    journal_path = config.paths.data_dir / "journal.db"
    risk_state_path = config.paths.data_dir / "risk_state.db"
    with (
        Journal(journal_path) as journal,
        ExecutionStateStore(config.paths.db_path) as execution_store,
        RiskStateStore(risk_state_path) as risk_state_store,
    ):
        try:
            result = run_testnet_cycle(config, secrets, journal, execution_store, risk_state_store)
        except ColdStartReconciliationError as exc:
            click.echo(f"Cannot start: {exc}", err=True)
            sys.exit(1)
    click.echo(f"action={result.action} reason={result.reason_code} detail={result.detail}")


@cli.command("testnet-health")
@click.pass_context
def testnet_health_cmd(ctx: click.Context) -> None:
    """Strictly read-only Binance Spot Testnet connectivity check.

    Performs ONLY GET requests: server time, clock sync, BTCUSDT exchange
    info, a signed account-info call, and an open-orders query. Reports
    local execution-state presence and any unresolved pending orders
    without modifying them. Never places, cancels, or modifies an order,
    never writes to local state, and never prints secrets, signatures, or
    signed query strings. See RISK_POLICY.md and SECURITY.md.
    """
    config: AppConfig = ctx.obj["config"]
    if config.mode != Mode.TESTNET:
        click.echo("The testnet-health command requires --mode testnet.", err=True)
        sys.exit(1)
    try:
        secrets = load_secrets()
    except Exception as exc:  # noqa: BLE001 - surfaced to the user, then exit
        click.echo(f"Failed to load testnet credentials: {exc}", err=True)
        sys.exit(1)

    report = run_testnet_health_check(config, secrets)
    for step in report.steps:
        click.echo(f"[{'PASS' if step.ok else 'FAIL'}] {step.name}: {step.detail}")
    click.echo(f"overall: {'PASS' if report.passed else 'FAIL'}")
    if not report.passed:
        sys.exit(1)


@cli.command("status")
@click.pass_context
def status_cmd(ctx: click.Context) -> None:
    """Show current mode, kill switch state, and portfolio state (no secrets)."""
    config: AppConfig = ctx.obj["config"]
    click.echo(f"mode: {config.mode.value}  symbol: {config.market.symbol}")
    if config.mode == Mode.TESTNET:
        click.echo(
            "testnet capability: OBSERVATIONAL - HOLD is evaluated normally, BUY is always "
            "suppressed (this agent cannot initiate a position on Testnet). SELL only closes "
            "or helps recover an already-established, fully-reconciled position; it is not a "
            "general trading path. See RISK_POLICY.md."
        )
    switch = KillSwitch(config.paths.data_dir / "KILL_SWITCH")
    click.echo(f"kill_switch: {'ENGAGED (' + (switch.reason() or '') + ')' if switch.is_engaged() else 'disengaged'}")
    with ExecutionStateStore(config.paths.db_path) as store:
        portfolio = store.load_portfolio(config.market.symbol)
    if portfolio is None:
        click.echo("portfolio: not initialized")
    else:
        click.echo(
            f"portfolio: quote_balance={portfolio.quote_balance} base_balance={portfolio.base_balance} "
            f"position_side={portfolio.position_side.value} realized_pnl_quote={portfolio.realized_pnl_quote}"
        )


@cli.group("kill-switch")
def kill_switch_group() -> None:
    """Manually controlled kill switch - halts ALL order submission when engaged."""


@kill_switch_group.command("engage")
@click.option("--reason", default="", help="Why the kill switch is being engaged.")
@click.pass_context
def kill_switch_engage(ctx: click.Context, reason: str) -> None:
    config: AppConfig = ctx.obj["config"]
    switch = KillSwitch(config.paths.data_dir / "KILL_SWITCH")
    switch.engage(reason)
    click.echo(f"Kill switch ENGAGED: {switch.reason()}")


@kill_switch_group.command("disengage")
@click.pass_context
def kill_switch_disengage(ctx: click.Context) -> None:
    config: AppConfig = ctx.obj["config"]
    KillSwitch(config.paths.data_dir / "KILL_SWITCH").disengage()
    click.echo("Kill switch DISENGAGED.")


@kill_switch_group.command("status")
@click.pass_context
def kill_switch_status(ctx: click.Context) -> None:
    config: AppConfig = ctx.obj["config"]
    switch = KillSwitch(config.paths.data_dir / "KILL_SWITCH")
    click.echo(f"ENGAGED: {switch.reason()}" if switch.is_engaged() else "disengaged")


if __name__ == "__main__":
    cli()
