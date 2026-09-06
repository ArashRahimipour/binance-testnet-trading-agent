"""Command-line entry point.

The `--mode` option is a `click.Choice` built directly from the `Mode` enum
("backtest", "testnet", "shadow"). There is no "live" choice and no way to
pass one - click rejects any other value before any application code runs.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
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
from trading_agent.data.gap_recovery import (
    CHECKPOINT_FILENAME,
    DEFAULT_CONNECT_TIMEOUT_SECONDS,
    DEFAULT_MAX_SECONDS_PER_GAP,
    DEFAULT_READ_TIMEOUT_SECONDS,
    GapAuditCheckpoint,
    GapForensicReport,
    IncompleteAuditError,
    RecoveryOutcome,
    apply_gap_recovery,
    run_gap_forensics,
)
from trading_agent.data.gap_recovery import (
    DEFAULT_MAX_RETRIES as GAP_AUDIT_DEFAULT_MAX_RETRIES,
)
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
from trading_agent.research.blocked_chronological_evaluation import (
    BlockResult,
    CandidateBlockedChronologicalResult,
    run_candidate_blocked_chronological_evaluation,
)
from trading_agent.research.candidate_registry import CANDIDATE_REGISTRY, TOTAL_CANDIDATE_COUNT
from trading_agent.research.candidate_registry_round3 import (
    REQUIRED_MARKET_INTERVAL as ROUND3_MARKET_INTERVAL,
)
from trading_agent.research.cutoff import RESEARCH_CUTOFF_ISO, split_at_cutoff
from trading_agent.research.fixed_duration_evaluation import DEFAULT_BLOCK_DURATION_DAYS
from trading_agent.research.freeze import freeze_candidate, save_frozen_candidate
from trading_agent.research.frozen_baseline import reproduce_frozen_baseline_report
from trading_agent.research.post_mortem import CandidatePostMortem, build_post_mortem_report
from trading_agent.research.round2_report import Round2Comparison, build_round2_report
from trading_agent.research.round3_report import Round3CandidateReport, build_round3_report
from trading_agent.research.scorecard import ScorecardEntry, ScorecardStatus, build_scorecard
from trading_agent.research.sensitivity_comparison import (
    CandidateSensitivityComparison,
    build_candidate_sensitivity_comparison,
)
from trading_agent.risk.kill_switch import KillSwitch
from trading_agent.shadow.boundary import SHADOW_START_BOUNDARY_ISO
from trading_agent.shadow.engine import (
    ShadowConfigError,
    run_shadow_cycle,
    shadow_kill_switch_path,
)
from trading_agent.shadow.lock import ShadowLockError
from trading_agent.shadow.report import ShadowReport, build_shadow_report
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
@click.option("--end", "end_str", default=None, help="ISO8601 UTC end date, EXCLUSIVE (default: now) - candles opening at or after this date are never fetched or stored. Only used with --start.")
@click.option("--max-retries", default=DEFAULT_MAX_RETRIES, show_default=True, help="Max retries per page (and per gap-recovery attempt) before giving up.")
@click.pass_context
def fetch_data(ctx: click.Context, limit: int, start_str: str | None, end_str: str | None, max_retries: int) -> None:
    """Fetch historical candles from the public read-only market-data host and store them.

    Without --start, fetches the most recent `--limit` completed candles in
    a single request. With --start (optionally --end), pages through the
    full date range - suitable for downloading multiple years of history.

    --start/--end together define a genuine half-open, EXCLUSIVE-of-`--end`
    [--start, --end) range: a candle opening exactly AT --end is never
    fetched or stored, even though Binance's own API treats its `endTime`
    parameter as inclusive - this is enforced at both the HTTP request
    itself and by filtering every result again regardless (see
    `data/historical_fetch.py`'s module docstring for the incident this
    guards against: an earlier version of this command stored a BTCUSDT/1h
    candle exactly at the immutable 2025-05-16 research cutoff after a
    `--end 2025-05-16` run).

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


def _gap_audit_progress_printer() -> Callable[[str], None]:
    """Every progress line is echoed AND immediately flushed - required so
    the operator sees live progress even when stdout is redirected to a
    file/pipe (block-buffered by default in that case), which is exactly
    what made the original hang look like dead silence for 90 minutes."""

    def _on_progress(message: str) -> None:
        click.echo(message)
        sys.stdout.flush()

    return _on_progress


def _print_gap_forensic_report(report: GapForensicReport) -> None:
    if report.interrupted:
        click.echo(
            f"*** INTERRUPTED (Ctrl+C): only {report.gaps_processed}/{report.total_gaps} confirmed gap(s) "
            "were processed before this partial report was produced. Re-run to resume - already-checkpointed "
            "gaps are skipped, not re-downloaded. ***"
        )
    click.echo(
        f"symbol={report.symbol} interval={report.interval} generated_at_ms={report.generated_at_ms}\n"
        f"total_confirmed_gaps={report.total_gaps} gaps_processed={report.gaps_processed} "
        f"total_missing_hours={report.total_missing_hours}\n"
        f"  fully_recoverable_hours={report.fully_recoverable_hours}\n"
        f"  partially_recoverable_hours={report.partially_recoverable_hours}\n"
        f"  genuine_no_data_exchange_outage_hours={report.genuine_no_data_hours}\n"
        f"  unresolved_hours={report.unresolved_hours}"
    )
    for gap_result in report.gap_results:
        gap = gap_result.gap
        click.echo(
            f"\n--- confirmed gap: expected_open_time_ms={gap.expected_open_time_ms} "
            f"previous_open_time_ms={gap.previous_open_time_ms} next_open_time_ms={gap.next_open_time_ms} "
            f"missing_intervals={gap.missing_intervals} ---"
        )
        for hour in gap_result.missing_hours:
            click.echo(
                f"  hour open_time_ms={hour.open_time_ms}: {hour.outcome.value} "
                f"(found_1m_candles={hour.found_1m_candle_count}) - {hour.detail}"
            )
            if hour.provenance is not None:
                p = hour.provenance
                click.echo(
                    f"    provenance: source={p.source} retrieved_at_ms={p.retrieved_at_ms} "
                    f"component_count={p.component_count} "
                    f"first_component_open_time_ms={p.first_component_open_time_ms} "
                    f"last_component_open_time_ms={p.last_component_open_time_ms} "
                    f"validation_result={p.validation_result} content_hash={p.content_hash}"
                )

    click.echo("\n--- resulting gap-free segments if every FULLY_RECOVERABLE hour above were stored ---")
    for seg in report.resulting_segments_after_recovery:
        click.echo(
            f"  segment {seg.segment_index}: start_time_ms={seg.start_time_ms} end_time_ms={seg.end_time_ms} "
            f"candle_count={seg.candle_count}"
        )
    click.echo(
        f"\nRound-3 (multitimeframe_breakout_E1_round3) complete "
        f"{report.round3_block_duration_days}-day fixed-duration block count after recovery: "
        f"{report.round3_complete_blocks_after_recovery} "
        f"(anchor_warm_up_candles_required={report.round3_min_required_candles}; a pure candle-counting "
        "estimate only - no candidate evaluation was run to produce this number)"
    )


_GAP_AUDIT_MAX_RETRIES_HELP = "Max attempts per HTTP request before giving up on it (capped exponential backoff between attempts)."
_GAP_AUDIT_MAX_SECONDS_PER_GAP_HELP = "Wall-clock budget per confirmed gap - any hour not yet checked once this is exceeded is marked UNRESOLVED rather than waited on further."
_GAP_AUDIT_RESET_CHECKPOINT_HELP = "Discard the on-disk checkpoint first, so every confirmed gap is re-audited from scratch instead of resuming."


def _gap_audit_checkpoint(config: AppConfig) -> GapAuditCheckpoint:
    return GapAuditCheckpoint(config.paths.data_dir / CHECKPOINT_FILENAME)


@cli.command("research-gap-audit")
@click.option("--max-retries", default=GAP_AUDIT_DEFAULT_MAX_RETRIES, show_default=True, help=_GAP_AUDIT_MAX_RETRIES_HELP)
@click.option("--max-seconds-per-gap", default=DEFAULT_MAX_SECONDS_PER_GAP, show_default=True, help=_GAP_AUDIT_MAX_SECONDS_PER_GAP_HELP)
@click.option("--reset-checkpoint", is_flag=True, default=False, help=_GAP_AUDIT_RESET_CHECKPOINT_HELP)
@click.pass_context
def research_gap_audit_cmd(ctx: click.Context, max_retries: int, max_seconds_per_gap: float, reset_checkpoint: bool) -> None:
    """READ-ONLY forensic report over every confirmed BTCUSDT 1h gap
    already recorded in the local candle database (see `data/
    gap_recovery.py`).

    For every missing hour, queries Binance's official 1-minute historical
    klines (BATCHED per contiguous missing range - never one request per
    hour or per minute) and classifies it: FULLY_RECOVERABLE (all 60
    1-minute candles present, continuous, and validated - would be
    reconstructed by `research-gap-recover --confirm`), PARTIALLY_RECOVERABLE
    (some but not all 60 exist - never reconstructed), GENUINE_NO_DATA (the
    exchange has nothing at all for that hour - a real, permanent outage),
    or UNRESOLVED (a validation failure, a candle at or after the immutable
    research cutoff which is never even fetched, or a gap whose own
    `--max-seconds-per-gap` budget ran out before reaching that hour).

    Prints live progress as it runs ("gap X/28: ...", each fetch attempt
    with its outcome, every gap's completion) - every line is flushed
    immediately, so it stays visible even when output is redirected to a
    file. Progress is CHECKPOINTED to `<data_dir>/gap_audit_checkpoint.json`
    as each gap completes: interrupting with Ctrl+C prints a clear partial
    summary (never a raw traceback) and re-running later skips every
    already-completed gap rather than re-downloading it - pass
    `--reset-checkpoint` to force a full re-audit instead.

    This command NEVER writes to the candle database, NEVER runs a
    candidate evaluation of any kind, NEVER connects to Testnet, and NEVER
    places an order - it only ever makes read-only, boundedly-timed-out
    requests to the public, unauthenticated Binance market-data host.
    Reconstruction, when something is fully recoverable, always aggregates
    real 1-minute data exactly as Binance's own 1h candles are built
    (open=first open, high=max high, low=min low, close=last close,
    volume=sum volume) - never interpolated, never fabricated.
    """
    config: AppConfig = ctx.obj["config"]
    if config.mode != Mode.BACKTEST:
        click.echo("The research-gap-audit command requires --mode backtest.", err=True)
        sys.exit(1)
    if config.market.interval != "1h":
        click.echo(
            f"The research-gap-audit command requires market.interval='1h' (got {config.market.interval!r}) - "
            "gap forensics/recovery is only meaningful for hourly gaps.",
            err=True,
        )
        sys.exit(1)

    with CandleStore(config.paths.db_path) as store:
        candles = store.get_candles(config.market.symbol, "1h")
        gaps = store.get_gaps(config.market.symbol, "1h")

    if not gaps:
        click.echo(f"No confirmed 1h gaps recorded for {config.market.symbol} - nothing to audit.")
        return

    checkpoint = _gap_audit_checkpoint(config)
    if reset_checkpoint:
        checkpoint.clear()

    client = BinancePublicMarketDataClient(
        PRODUCTION_MARKET_DATA_HOST, timeout_seconds=(DEFAULT_CONNECT_TIMEOUT_SECONDS, DEFAULT_READ_TIMEOUT_SECONDS)
    )
    report = run_gap_forensics(
        client, config.market.symbol, "1h", candles, gaps,
        max_retries=max_retries, max_seconds_per_gap=max_seconds_per_gap,
        on_progress=_gap_audit_progress_printer(), checkpoint=checkpoint,
    )
    _print_gap_forensic_report(report)
    if report.interrupted:
        click.echo("\nInterrupted before completing - recovery is not possible from a partial audit. Re-run to resume.", err=True)
        sys.exit(130)
    click.echo(
        "\nThis was a READ-ONLY audit - nothing was stored. Run `research-gap-recover --confirm` to store "
        "any FULLY_RECOVERABLE candle reported above."
    )


@cli.command("research-gap-recover")
@click.option("--confirm", is_flag=True, default=False, help="Explicitly approve storing every FULLY_RECOVERABLE reconstructed candle. Without this flag, nothing is written.")
@click.option("--max-retries", default=GAP_AUDIT_DEFAULT_MAX_RETRIES, show_default=True, help=_GAP_AUDIT_MAX_RETRIES_HELP)
@click.option("--max-seconds-per-gap", default=DEFAULT_MAX_SECONDS_PER_GAP, show_default=True, help=_GAP_AUDIT_MAX_SECONDS_PER_GAP_HELP)
@click.option("--reset-checkpoint", is_flag=True, default=False, help=_GAP_AUDIT_RESET_CHECKPOINT_HELP)
@click.pass_context
def research_gap_recover_cmd(
    ctx: click.Context, confirm: bool, max_retries: int, max_seconds_per_gap: float, reset_checkpoint: bool
) -> None:
    """Runs the EXACT SAME read-only, bounded, checkpointed forensic
    analysis as `research-gap-audit`, then - ONLY if `--confirm` is passed
    AND that analysis completed without interruption - stores every
    FULLY_RECOVERABLE reconstructed candle (see `data/gap_recovery.py`)
    atomically alongside a freshly recomputed gap manifest (a gap that is
    now fully resolved is removed; a gap only partially resolved is
    replaced by a narrower one covering exactly what remains missing).

    Storage is atomic (one transaction - either every recovered candle and
    the updated gap manifest are committed together, or nothing is) and
    idempotent (re-running this command against an already-recovered
    database re-derives and re-asserts the identical result). Never
    fabricates, interpolates, changes any strategy/candidate/parameter/
    scorecard/risk/fee/slippage/sizing/execution logic, runs a candidate
    evaluation, or connects to Testnet.

    RECOVERY IS IMPOSSIBLE WITHOUT A COMPLETED AUDIT: if the analysis is
    interrupted (Ctrl+C) before every confirmed gap is processed, this
    command refuses to store anything at all, even with `--confirm` -
    re-run (already-checkpointed gaps are skipped) until it completes.
    Without `--confirm`, this command performs the exact same analysis and
    prints the exact same report as `research-gap-audit` but stores
    nothing at all - `--confirm` is the ONLY thing that makes this command
    write to the database.
    """
    config: AppConfig = ctx.obj["config"]
    if config.mode != Mode.BACKTEST:
        click.echo("The research-gap-recover command requires --mode backtest.", err=True)
        sys.exit(1)
    if config.market.interval != "1h":
        click.echo(
            f"The research-gap-recover command requires market.interval='1h' (got {config.market.interval!r}) - "
            "gap forensics/recovery is only meaningful for hourly gaps.",
            err=True,
        )
        sys.exit(1)

    with CandleStore(config.paths.db_path) as store:
        candles = store.get_candles(config.market.symbol, "1h")
        gaps = store.get_gaps(config.market.symbol, "1h")

        if not gaps:
            click.echo(f"No confirmed 1h gaps recorded for {config.market.symbol} - nothing to recover.")
            return

        checkpoint = _gap_audit_checkpoint(config)
        if reset_checkpoint:
            checkpoint.clear()

        client = BinancePublicMarketDataClient(
            PRODUCTION_MARKET_DATA_HOST, timeout_seconds=(DEFAULT_CONNECT_TIMEOUT_SECONDS, DEFAULT_READ_TIMEOUT_SECONDS)
        )
        report = run_gap_forensics(
            client, config.market.symbol, "1h", candles, gaps,
            max_retries=max_retries, max_seconds_per_gap=max_seconds_per_gap,
            on_progress=_gap_audit_progress_printer(), checkpoint=checkpoint,
        )
        _print_gap_forensic_report(report)

        if report.interrupted:
            click.echo(
                "\nInterrupted before completing - recovery is impossible without a completed audit, even "
                "with --confirm. Nothing was stored. Re-run to resume (already-checkpointed gaps are skipped).",
                err=True,
            )
            sys.exit(130)

        if not confirm:
            click.echo(
                "\nNo --confirm flag given - NOTHING was stored (identical read-only analysis to "
                "`research-gap-audit`). Re-run with --confirm to store the FULLY_RECOVERABLE candle(s) above."
            )
            return

        recoverable_count = sum(
            1
            for gap_result in report.gap_results
            for hour in gap_result.missing_hours
            if hour.outcome == RecoveryOutcome.FULLY_RECOVERABLE
        )
        if recoverable_count == 0:
            click.echo("\n--confirm given, but there is nothing FULLY_RECOVERABLE to store.")
            return

        try:
            result = apply_gap_recovery(store, config.market.symbol, "1h", report)
        except IncompleteAuditError as exc:
            click.echo(f"\n{exc}", err=True)
            sys.exit(1)

    click.echo(
        f"\nSTORED {result.stored_candle_count} recovered candle(s), atomically, alongside a freshly "
        f"recomputed gap manifest. {len(result.remaining_confirmed_gaps)} confirmed 1h gap(s) remain."
    )
    for gap in result.remaining_confirmed_gaps:
        click.echo(
            f"  remaining confirmed gap: expected_open_time_ms={gap.expected_open_time_ms} "
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


@cli.command("research-backtest")
@click.pass_context
def research_backtest_cmd(ctx: click.Context) -> None:
    """Leakage-resistant strategy-development research phase.

    Reproduces the frozen, REJECTED v0.1 EMA baseline's report (unchanged,
    for regression comparison only), then runs a BLOCKED CHRONOLOGICAL
    EVALUATION (see `research/blocked_chronological_evaluation.py` - this
    is NOT walk-forward optimization; every candidate's parameters are
    fixed before this command ever runs and nothing is fitted or selected
    per block) of the declared candidate registry (three families, a
    small fixed set of configurations) using ONLY data strictly before the
    immutable research cutoff (2025-05-16). The already-observed
    2025-05-16..2026-09-04 period is never used to develop, filter,
    threshold, rank, or select any candidate - see `research/cutoff.py`.
    Every block of every candidate is reported, never only the best. A
    RESEARCH_SURVIVOR is automatically frozen (see `research/freeze.py`)
    so it cannot later be "tested" again on data this run already saw.
    """
    config: AppConfig = ctx.obj["config"]
    if config.mode != Mode.BACKTEST:
        click.echo("The research-backtest command requires --mode backtest.", err=True)
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

    pre_cutoff, consumed = split_at_cutoff(candles)
    click.echo(
        f"research cutoff: {RESEARCH_CUTOFF_ISO}  pre_cutoff_candles={len(pre_cutoff)}  "
        f"consumed_candles={len(consumed)} (already observed - reproduces the frozen baseline ONLY)"
    )

    journal_path = config.paths.data_dir / "journal.db"
    with Journal(journal_path) as journal:
        click.echo("\n=== FROZEN BASELINE (ema_crossover_v0_1_rejected) - regression reproduction only ===")
        try:
            frozen = reproduce_frozen_baseline_report(candles, config, filters, journal)
        except ValueError as exc:
            click.echo(f"Frozen baseline reproduction failed: {exc}", err=True)
            sys.exit(1)
        click.echo(f"verdict: {frozen.verdict}")

        click.echo(
            f"\n=== CANDIDATE DEVELOPMENT: {TOTAL_CANDIDATE_COUNT} declared configurations, "
            "pre-cutoff data only ==="
        )
        if not pre_cutoff:
            click.echo("No pre-cutoff candles available - cannot develop any candidate.", err=True)
            sys.exit(1)

        candidate_by_id = {spec.candidate_id: spec for spec in CANDIDATE_REGISTRY}
        results: list[CandidateBlockedChronologicalResult] = []
        for spec in CANDIDATE_REGISTRY:
            result = run_candidate_blocked_chronological_evaluation(spec, pre_cutoff, config, filters, journal)
            results.append(result)
            click.echo(f"\n--- {spec.candidate_id} ({spec.family}) params={spec.params} ---")
            for block in result.blocks:
                _print_block_result(block)
            for warning in result.warnings:
                click.echo(f"  WARNING: {warning}", err=True)

    scorecard = build_scorecard(results)
    click.echo(f"\n=== SCORECARD ===\n{scorecard.multiple_testing_warning}\n")
    frozen_dir = config.paths.data_dir / "research" / "frozen_candidates"
    now_ms = int(time.time() * 1000)
    freeze_boundary_ms = max([now_ms] + [c.close_time_ms for c in candles]) if candles else now_ms

    for entry in scorecard.entries:
        click.echo(f"{entry.candidate_id} ({entry.family}): status={entry.status.value}")
        click.echo(
            f"  total_trades={entry.total_trade_count} blocks_with_a_trade={entry.blocks_with_a_trade}/"
            f"{entry.block_count} median_block_realized_return_pct={entry.median_block_realized_return_pct} "
            f"aggregate_realized_pnl_quote={entry.aggregate_realized_pnl_quote} "
            f"worst_block_realized_return_pct={entry.worst_block_realized_return_pct} "
            f"worst_block_max_drawdown_pct={entry.worst_block_max_drawdown_pct} "
            f"positive_realized_pnl_block_fraction={entry.positive_realized_pnl_block_fraction} "
            f"max_best_trade_contribution_pct={entry.max_best_trade_contribution_pct}"
        )
        click.echo(
            f"  (marked-to-market, reported only) median_block_marked_to_market_return_pct="
            f"{entry.median_block_marked_to_market_return_pct} median_excess_return_vs_buy_and_hold_pct="
            f"{entry.median_excess_return_vs_buy_and_hold_pct}"
        )
        click.echo(f"  {entry.benchmark_note}")
        for criterion in entry.criteria:
            click.echo(f"    [{'PASS' if criterion.passed else 'FAIL'}] {criterion.name}: {criterion.detail}")
        click.echo(f"  {entry.caveat}")

        if entry.status == ScorecardStatus.RESEARCH_SURVIVOR:
            spec = candidate_by_id[entry.candidate_id]
            record = freeze_candidate(
                entry, frozen_at_ms=now_ms, freeze_boundary_ms=freeze_boundary_ms,
                candidate=spec, config=config, filters=filters,
            )
            path = save_frozen_candidate(record, frozen_dir)
            click.echo(
                f"  FROZEN at {path} (candidate_version={record.candidate_version}, "
                f"source_commit_hash={record.source_commit_hash}) - next valid test requires candles "
                f"with open_time_ms >= {freeze_boundary_ms} (i.e. genuinely new data, never data this "
                "run already saw) AND must pass "
                "freeze.assert_frozen_candidate_matches_current_implementation (fails closed on any "
                "strategy/config drift)."
            )

    click.echo(
        "\nNote: this is exploratory research on simulated fills over historical data, not a "
        "claim of profitability and not approval for live or Testnet trading. REJECTED, "
        "RESEARCH_SURVIVOR, and INSUFFICIENT_EVIDENCE are the only statuses this command ever "
        "reports."
    )


def _print_block_result(block_result: BlockResult) -> None:
    block = block_result.block
    if block.skipped_reason is not None:
        click.echo(f"  segment {block.segment_index} block {block.block_index}: SKIPPED - {block.skipped_reason}")
        return
    report = block_result.performance
    assert report is not None
    start_date = datetime.fromtimestamp(block.window_start_time_ms / 1000, tz=UTC).date() if block.window_start_time_ms else None
    end_date = datetime.fromtimestamp(block.window_end_time_ms / 1000, tz=UTC).date() if block.window_end_time_ms else None
    excess_return_pct = (
        report.total_return_pct - report.buy_and_hold_return_pct if report.buy_and_hold_return_pct is not None else None
    )
    click.echo(
        f"  segment {block.segment_index} block {block.block_index}: {start_date} to {end_date} "
        f"({block.candle_count} candles) trades={report.trade_count} "
        f"return_pct={report.total_return_pct:.2f} max_drawdown_pct={report.max_drawdown_pct:.2f} "
        f"buy_and_hold_pct={report.buy_and_hold_return_pct} excess_return_pct={excess_return_pct} "
        f"(marked-to-market - see SCORECARD's benchmark note; never used for survivor status) "
        f"ends_open_position={block_result.ends_with_open_position}"
    )
    if block_result.extended is not None:
        realized = block_result.extended.pnl_breakdown.realized_closed_trade_pnl_quote
        click.echo(f"    realized_closed_trade_pnl_quote={realized} (this - not return_pct above - drives scoring)")
    if block_result.diagnostics is not None and block_result.diagnostics.rejected_entries_by_reason:
        reasons = ", ".join(f"{code}={count}" for code, count in sorted(block_result.diagnostics.rejected_entries_by_reason.items()))
        click.echo(f"    rejected_entries_by_reason (includes exchange-filter/min-notional rejections): {reasons}")
    if block_result.risk_reward is not None:
        rr = block_result.risk_reward
        click.echo(
            f"    fixed_1_to_2_risk_reward_policy: entries_approved={rr.entries_approved} "
            f"rejected_net_rr_below_2={rr.entries_rejected_net_rr_below_minimum} "
            f"rejected_exchange_filter_within_risk_budget={rr.entries_rejected_exchange_filter_within_risk_budget} "
            f"rejected_post_fill_revalidation={rr.entries_rejected_post_fill_revalidation} "
            f"stop_loss_exits={rr.stop_loss_exits} take_profit_exits={rr.take_profit_exits} "
            f"gap_losses_exceeding_planned_risk={rr.gap_losses_exceeding_planned_risk}"
        )
        if rr.entries_approved > 0:
            click.echo(
                f"      planned_risk_quote_total={rr.planned_risk_quote_total} "
                f"planned_reward_quote_total={rr.planned_reward_quote_total} "
                f"planned_risk_pct_per_entry={list(rr.planned_risk_pct_values)} "
                f"planned_reward_pct_per_entry={list(rr.planned_reward_pct_values)} "
                f"gross_reward_to_risk_per_entry={list(rr.gross_reward_to_risk_values)} "
                f"net_reward_to_risk_per_entry={list(rr.net_reward_to_risk_values)}"
            )


@cli.command("research-postmortem")
@click.pass_context
def research_postmortem_cmd(ctx: click.Context) -> None:
    """Read-only candidate POST-MORTEM report over an already-completed
    pre-cutoff blocked chronological evaluation (see `research/post_mortem.py`).

    Re-runs `run_candidate_blocked_chronological_evaluation` for every
    declared candidate (the SAME deterministic computation `research-backtest`
    already performs, on the SAME pre-cutoff-only data - no candle at or
    after the immutable research cutoff is ever used, see
    `research/cutoff.py`) and reports detailed per-trade statistics: win
    rate, PnL distribution, exit-reason breakdown, realized R-multiples,
    planned-vs-realized R/R, fee/slippage totals, exclusion-of-best-trades
    analysis, PnL concentration, chronological stability, and the fixed
    risk/reward policy's own rejection/compliance diagnostics.

    This command changes NOTHING about any strategy, parameter, threshold,
    risk/reward rule, fee, slippage, sizing, or execution behavior - it is
    pure, deterministic, read-only aggregation over results that already
    exist. It NEVER ranks candidates and NEVER selects one - see
    `research/post_mortem.py::MULTIPLE_TESTING_NOTE`. Every candidate ends
    with exactly one evidence-only diagnosis label (never "profitable",
    "approved", or "rejected" - those remain `research-backtest`'s own,
    separate scorecard vocabulary).
    """
    config: AppConfig = ctx.obj["config"]
    if config.mode != Mode.BACKTEST:
        click.echo("The research-postmortem command requires --mode backtest.", err=True)
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

    pre_cutoff, consumed = split_at_cutoff(candles)
    click.echo(
        f"research cutoff: {RESEARCH_CUTOFF_ISO}  pre_cutoff_candles={len(pre_cutoff)}  "
        f"consumed_candles={len(consumed)} (NEVER used below - post-mortem is pre-cutoff development "
        "data only)"
    )
    if not pre_cutoff:
        click.echo("No pre-cutoff candles available - cannot build a post-mortem for any candidate.", err=True)
        sys.exit(1)

    results = [
        run_candidate_blocked_chronological_evaluation(spec, pre_cutoff, config, filters)
        for spec in CANDIDATE_REGISTRY
    ]
    report = build_post_mortem_report(results)

    click.echo(f"\n{report.multiple_testing_note}\n")
    click.echo(f"{report.equity_accounting_note}\n")
    for candidate in report.candidates:
        _print_candidate_post_mortem(candidate)

    click.echo(
        "\nNote: this is a read-only diagnostic report on already-computed, already-authorized "
        "pre-cutoff research results - not a claim of profitability and not approval for live or "
        "Testnet trading. It selects nothing; the accompanying `research-backtest` scorecard remains "
        "the only pass/fail assessment this project makes."
    )


def _print_candidate_post_mortem(c: CandidatePostMortem) -> None:
    click.echo(f"\n--- {c.candidate_id} ({c.family}) params={c.params} ---")
    tc = c.trade_counts
    click.echo(
        f"  entries_approved={tc.total_entries_approved} closed_trades={tc.closed_trades} "
        f"wins={tc.wins} losses={tc.losses} breakeven={tc.breakeven} win_rate_pct={tc.win_rate_pct}"
    )
    p = c.pnl_stats
    click.echo(
        f"  avg_net_pnl_quote={p.average_net_pnl_quote} median_net_pnl_quote={p.median_net_pnl_quote} "
        f"avg_winner_quote={p.average_winner_quote} avg_loser_quote={p.average_loser_quote} "
        f"realized_payoff_ratio={p.realized_payoff_ratio} profit_factor={p.profit_factor}"
    )
    click.echo(
        f"  expected_value: quote={p.expected_value_quote} "
        f"pct_of_starting_equity={p.expected_value_pct_of_starting_equity} "
        f"r_multiple={p.expected_value_r_multiple}"
    )
    for e in c.exit_reason_breakdown:
        click.echo(
            f"  exit_reason={e.exit_reason}: trade_count={e.trade_count} win_rate_pct={e.win_rate_pct} "
            f"expectancy_quote={e.expectancy_quote}"
        )
    r = c.realized_r_distribution
    click.echo(
        f"  realized_R: n={r.trades_with_known_r} excluded_unknown={r.trades_excluded_unknown_r} "
        f"min={r.min_r} median={r.median_r} mean={r.mean_r} max={r.max_r} "
        f"pct_>=+2R={r.pct_at_least_plus_2r} pct_<-1R={r.pct_losing_more_than_minus_1r}"
    )
    pr = c.planned_vs_realized
    click.echo(
        f"  planned_vs_realized: avg_planned_net_rr={pr.average_planned_net_reward_to_risk} "
        f"avg_realized_r_on_winners={pr.average_realized_r_multiple_on_winners} "
        f"total_planned_risk_quote={pr.total_planned_risk_quote} "
        f"total_planned_reward_quote={pr.total_planned_reward_quote} "
        f"total_realized_pnl_quote={pr.total_realized_pnl_quote}"
    )
    click.echo(f"  costs: total_fees_quote={c.costs.total_fees_quote} total_slippage_quote={c.costs.total_slippage_quote}")
    for ex in c.exclusions:
        click.echo(
            f"  {ex.label}: trades_excluded={ex.trades_excluded} net_pnl_quote={ex.net_pnl_quote} "
            f"remains_positive={ex.remains_positive}"
        )
    conc = c.concentration
    click.echo(
        f"  concentration: top1_pct={conc.top1_pct_of_positive_pnl} top3_pct={conc.top3_pct_of_positive_pnl} "
        f"top5_pct={conc.top5_pct_of_positive_pnl} "
        f"trades_to_50pct_net_profit={conc.trades_to_reach_50_pct_of_net_profit} "
        f"trades_to_100pct_net_profit={conc.trades_to_reach_100_pct_of_net_profit}"
    )
    cs = c.chronological_stability
    click.echo(
        f"  chronological_stability: longest_losing_streak_trades={cs.longest_losing_streak_trades} "
        f"longest_underwater_period_days={cs.longest_underwater_period_days}"
    )
    click.echo(
        f"    first_half: trades={cs.first_half.trade_count} net_pnl_quote={cs.first_half.net_pnl_quote} "
        f"win_rate_pct={cs.first_half.win_rate_pct}"
    )
    click.echo(
        f"    second_half: trades={cs.second_half.trade_count} net_pnl_quote={cs.second_half.net_pnl_quote} "
        f"win_rate_pct={cs.second_half.win_rate_pct}"
    )
    for y in cs.per_calendar_year:
        click.echo(f"    year={y.year}: trades={y.trade_count} net_pnl_quote={y.net_pnl_quote} win_rate_pct={y.win_rate_pct}")
    for b in cs.per_block:
        click.echo(
            f"    segment={b.segment_index} block={b.block_index}: trades={b.trade_count} "
            f"net_pnl_quote={b.net_pnl_quote} positive={b.positive}"
        )
    rr = c.risk_reward_diagnostics
    click.echo(
        f"  risk_reward_policy: rejected_net_rr_below_2={rr.entries_rejected_net_rr_below_minimum} "
        f"rejected_exchange_filter={rr.entries_rejected_exchange_filter} "
        f"rejected_post_fill_revalidation={rr.entries_rejected_post_fill_revalidation} "
        f"stop_loss_trades_total={rr.stop_loss_trades_total} "
        f"within_planned_risk={rr.stop_loss_trades_within_planned_risk} "
        f"gap_exceeding_planned_risk={rr.stop_loss_trades_gap_exceeding_planned_risk} "
        f"planned_1pct_risk_compliance_pct={rr.planned_1pct_risk_compliance_pct}"
    )
    if c.breakout_exclusion_check.applicable:
        bc = c.breakout_exclusion_check
        click.echo(
            f"  breakout_exclusion_check: remains_positive_excl_best_trade={bc.remains_positive_excluding_best_trade} "
            f"remains_positive_excl_best_3={bc.remains_positive_excluding_best_3_trades} "
            f"remains_positive_excl_best_5pct={bc.remains_positive_excluding_best_5_pct}"
        )
    for warning in c.warnings:
        click.echo(f"  WARNING: {warning}", err=True)
    click.echo(f"  DIAGNOSIS: {c.diagnosis.value} - {c.diagnosis_reason}")


def _print_scorecard_entry_summary(entry: ScorecardEntry, indent: str = "") -> None:
    click.echo(f"{indent}status={entry.status.value}")
    click.echo(
        f"{indent}total_trades={entry.total_trade_count} blocks_with_a_trade={entry.blocks_with_a_trade}/"
        f"{entry.block_count} median_block_realized_return_pct={entry.median_block_realized_return_pct} "
        f"aggregate_realized_pnl_quote={entry.aggregate_realized_pnl_quote} "
        f"worst_block_realized_return_pct={entry.worst_block_realized_return_pct} "
        f"worst_block_max_drawdown_pct={entry.worst_block_max_drawdown_pct} "
        f"positive_realized_pnl_block_fraction={entry.positive_realized_pnl_block_fraction} "
        f"max_best_trade_contribution_pct={entry.max_best_trade_contribution_pct}"
    )
    for criterion in entry.criteria:
        click.echo(f"{indent}  [{'PASS' if criterion.passed else 'FAIL'}] {criterion.name}: {criterion.detail}")
    click.echo(f"{indent}{entry.caveat}")


@cli.command("research-sensitivity")
@click.pass_context
def research_sensitivity_cmd(ctx: click.Context) -> None:
    """DURATION-NORMALIZED sensitivity report over the same pre-cutoff data
    (see `research/fixed_duration_evaluation.py` and `research/
    sensitivity_comparison.py`).

    The original `research-backtest` method splits every gap-free segment
    into a FIXED NUMBER of blocks by candle count - a tiny fragment segment
    therefore gets the same voting weight as a multi-year dominant segment.
    This command re-scores all nine ORIGINAL, UNMODIFIED round-1 candidates
    using fixed 365-day-duration blocks instead, and prints the original
    and duration-normalized scorecards SIDE BY SIDE for comparison. It
    NEVER changes any original result, scorecard, diagnosis, or frozen
    artifact (`research/blocked_chronological_evaluation.py` and
    `research/scorecard.py` are called completely unmodified), and it
    NEVER retroactively creates a RESEARCH_SURVIVOR from the sensitivity
    side of the comparison.
    """
    config: AppConfig = ctx.obj["config"]
    if config.mode != Mode.BACKTEST:
        click.echo("The research-sensitivity command requires --mode backtest.", err=True)
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

    pre_cutoff, consumed = split_at_cutoff(candles)
    click.echo(
        f"research cutoff: {RESEARCH_CUTOFF_ISO}  pre_cutoff_candles={len(pre_cutoff)}  "
        f"consumed_candles={len(consumed)} (NEVER used below)"
    )
    if not pre_cutoff:
        click.echo("No pre-cutoff candles available.", err=True)
        sys.exit(1)

    for spec in CANDIDATE_REGISTRY:
        comparison: CandidateSensitivityComparison = build_candidate_sensitivity_comparison(
            spec, pre_cutoff, config, filters
        )
        click.echo(f"\n--- {comparison.candidate_id} ({comparison.family}) params={comparison.params} ---")
        click.echo(f"  {comparison.methodology_note}")
        click.echo(f"  [{comparison.round_1_label}] (UNCHANGED, reproduced exactly - never altered)")
        _print_scorecard_entry_summary(comparison.round_1_original_evaluation, indent="    ")
        click.echo(
            f"  [duration_normalized_sensitivity] (fixed {DEFAULT_BLOCK_DURATION_DAYS}-day blocks - "
            "NON-BINDING, see note below)"
        )
        _print_scorecard_entry_summary(comparison.duration_normalized_sensitivity, indent="    ")
        click.echo(f"  {comparison.non_binding_note}")
        click.echo(f"  {comparison.cross_candidate_date_divergence_note}")
        for fragment in comparison.fragments:
            click.echo(
                f"  INSUFFICIENT-DURATION FRAGMENT: segment={fragment.segment_index} "
                f"candles={fragment.candle_count} available_tradable_duration_days="
                f"{fragment.available_tradable_duration_days:.1f} - {fragment.reason}"
            )
        for leftover in comparison.leftovers:
            click.echo(
                f"  LEFTOVER PARTIAL WINDOW: segment={leftover.segment_index} candles={leftover.candle_count} "
                f"duration_days={leftover.duration_days:.1f} (excluded from all pass/fail calculations)"
            )

    click.echo(
        "\nNote: round_1_original_evaluation above is never changed by this command - it remains the "
        "sole official status for every round-1 candidate. duration_normalized_sensitivity is a "
        "diagnostic lens only."
    )


@cli.command("research-round2")
@click.pass_context
def research_round2_cmd(ctx: click.Context) -> None:
    """ROUND-2 report: `breakout_regime_D1_round2`'s complete pre-cutoff
    result (see `research/round2_report.py`, `research/candidate_registry_round2.py`,
    and `research/candidates/breakout_regime_gate.py`).

    D1 is an explicitly RESULT-INFORMED round-2 hypothesis (round 1 showed
    breakout_B1 had broad trade-level profitability but sustained losses
    in 2021-2022) - NEVER presented as an untouched, pre-registered test.
    Evaluated ONLY on pre-cutoff data using the duration-normalized
    sensitivity blocks; the consumed post-cutoff period is never touched.
    Scored against the SAME conservative, pre-declared scorecard
    thresholds as round 1 - nothing loosened or tightened. Reports every
    full-duration block (including any that failed), never only the best.
    Even a RESEARCH_SURVIVOR verdict here is NOT a claim of profitability
    and NOT approval for live or Testnet trading.
    """
    config: AppConfig = ctx.obj["config"]
    if config.mode != Mode.BACKTEST:
        click.echo("The research-round2 command requires --mode backtest.", err=True)
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

    pre_cutoff, consumed = split_at_cutoff(candles)
    click.echo(
        f"research cutoff: {RESEARCH_CUTOFF_ISO}  pre_cutoff_candles={len(pre_cutoff)}  "
        f"consumed_candles={len(consumed)} (NEVER used below - D1 is pre-cutoff development data only)"
    )
    if not pre_cutoff:
        click.echo("No pre-cutoff candles available - cannot evaluate D1.", err=True)
        sys.exit(1)

    comparison: Round2Comparison = build_round2_report(pre_cutoff, config, filters)
    d1 = comparison.d1
    click.echo(f"\n=== ROUND {d1.round_number} HYPOTHESIS: {d1.candidate_id} ({d1.family}) params={d1.params} ===")
    click.echo(f"cumulative_candidate_configurations_examined={d1.cumulative_candidate_configurations_examined}")
    click.echo(d1.multiple_testing_warning)

    click.echo("\n--- D1 scorecard (same conservative thresholds as round 1) ---")
    _print_scorecard_entry_summary(d1.scorecard)

    click.echo(
        f"\n--- D1 EMA200 regime gate ---\nbreakout_signals_evaluated={d1.regime_gate_signals_evaluated} "
        f"blocked_by_regime_gate={d1.regime_gate_signals_blocked} "
        f"blocked_pct={d1.regime_gate_blocked_pct}"
    )
    click.echo(f"D1 max_drawdown_pct (worst full-duration block)={d1.max_drawdown_pct}")

    for fragment in d1.fragments:
        click.echo(
            f"INSUFFICIENT-DURATION FRAGMENT: segment={fragment.segment_index} candles={fragment.candle_count} "
            f"available_tradable_duration_days={fragment.available_tradable_duration_days:.1f} - {fragment.reason}"
        )
    for leftover in d1.leftovers:
        click.echo(
            f"LEFTOVER PARTIAL WINDOW: segment={leftover.segment_index} candles={leftover.candle_count} "
            f"duration_days={leftover.duration_days:.1f} (excluded from all pass/fail calculations)"
        )

    click.echo("\n--- D1 detailed post-mortem (every full-duration block, no hidden failures) ---")
    _print_candidate_post_mortem(d1.post_mortem)

    click.echo(f"\n{d1.not_approved_note}")

    click.echo(f"\n{comparison.comparison_note}")
    click.echo("\n--- breakout_B1 on IDENTICAL dates (same duration-normalized blocks) ---")
    _print_scorecard_entry_summary(comparison.b1_scorecard_on_identical_dates)
    _print_candidate_post_mortem(comparison.b1_post_mortem_on_identical_dates)

    click.echo(
        "\nNote: this is exploratory research on simulated fills over historical data, not a claim of "
        "profitability and not approval for live or Testnet trading."
    )


@cli.command("research-round3")
@click.pass_context
def research_round3_cmd(ctx: click.Context) -> None:
    """ROUND-3 report: `multitimeframe_breakout_E1_round3`'s complete
    pre-cutoff result (see `research/round3_report.py`, `research/
    candidate_registry_round3.py`, and `research/candidates/
    multitimeframe_breakout.py`).

    E1 is a round-3 hypothesis examined AFTER round 1's nine candidates
    and round 2's D1 (OFFICIAL REJECTED verdict, preserved unchanged)
    were already observed - NEVER presented as an untouched, pre-
    registered test. It requires 1h candles specifically (its own weekly
    and 4h context is derived by aggregating that 1h stream - see that
    module's docstring) - this command overrides the configured market
    interval to "1h" regardless of the loaded config file's own default,
    and reads ONLY interval="1h" rows from the SAME candle database
    (candles for multiple intervals of the same symbol coexist there,
    keyed by (symbol, interval, open_time_ms) - see `data/storage.py`).
    Evaluated ONLY on pre-cutoff data using the duration-normalized
    fixed-duration blocks; the consumed post-cutoff period is never
    touched. Scored against the SAME conservative, pre-declared scorecard
    thresholds as rounds 1 and 2 - nothing loosened or tightened. Reports
    every full-duration block (including any that failed), never only
    the best. Even a RESEARCH_SURVIVOR verdict here is NOT a claim of
    profitability and NOT approval for live or Testnet trading.

    A full multi-year 1h evaluation can take a considerable amount of
    time (this candidate's weekly-EMA-40 warm-up alone requires roughly
    45 weeks of 1h candles, and its weekly/4h context is re-derived from
    the 1h stream on every single decision) - this is an accepted,
    disclosed cost of the required multi-timeframe design, not a defect.
    """
    config: AppConfig = ctx.obj["config"]
    if config.mode != Mode.BACKTEST:
        click.echo("The research-round3 command requires --mode backtest.", err=True)
        sys.exit(1)
    config = config.model_copy(update={"market": config.market.model_copy(update={"interval": ROUND3_MARKET_INTERVAL})})

    with CandleStore(config.paths.db_path) as store:
        candles = store.get_candles(config.market.symbol, ROUND3_MARKET_INTERVAL)
    if not candles:
        click.echo(
            f"No interval={ROUND3_MARKET_INTERVAL!r} candles found. Run `fetch-data --config <config-with-"
            f"market.interval={ROUND3_MARKET_INTERVAL}>` first (see this command's own docstring).",
            err=True,
        )
        sys.exit(1)

    client = BinancePublicMarketDataClient(PRODUCTION_MARKET_DATA_HOST)
    try:
        filters = SymbolFilters.from_exchange_info(client.get_exchange_info(config.market.symbol))
    except Exception as exc:  # noqa: BLE001 - surfaced to the user, then exit
        click.echo(f"Failed to fetch exchange filters: {exc}", err=True)
        sys.exit(1)

    pre_cutoff, consumed = split_at_cutoff(candles)
    click.echo(
        f"research cutoff: {RESEARCH_CUTOFF_ISO}  pre_cutoff_candles={len(pre_cutoff)}  "
        f"consumed_candles={len(consumed)} (NEVER used below - E1 is pre-cutoff development data only)"
    )
    if not pre_cutoff:
        click.echo("No pre-cutoff candles available - cannot evaluate E1.", err=True)
        sys.exit(1)

    report: Round3CandidateReport = build_round3_report(pre_cutoff, config, filters)
    click.echo(f"\n=== ROUND {report.round_number} HYPOTHESIS: {report.candidate_id} ({report.family}) params={report.params} ===")
    click.echo(f"cumulative_candidate_configurations_examined={report.cumulative_candidate_configurations_examined}")
    click.echo(report.multiple_testing_warning)

    click.echo("\n--- E1 scorecard (same conservative thresholds as rounds 1 and 2) ---")
    _print_scorecard_entry_summary(report.scorecard)

    click.echo(
        "\n--- E1 multi-timeframe funnel diagnostics ---\n"
        f"weekly_filter_rejections={report.weekly_filter_rejections} "
        f"four_h_setups_detected={report.four_h_setups_detected} setups_armed={report.setups_armed} "
        f"setups_expired={report.setups_expired} one_h_confirmations={report.one_h_confirmations} "
        f"strategy_entries={report.strategy_entries}"
    )

    for fragment in report.fragments:
        click.echo(
            f"INSUFFICIENT-DURATION FRAGMENT: segment={fragment.segment_index} candles={fragment.candle_count} "
            f"available_tradable_duration_days={fragment.available_tradable_duration_days:.1f} - {fragment.reason}"
        )
    for leftover in report.leftovers:
        click.echo(
            f"LEFTOVER PARTIAL WINDOW: segment={leftover.segment_index} candles={leftover.candle_count} "
            f"duration_days={leftover.duration_days:.1f} (excluded from all pass/fail calculations)"
        )

    click.echo("\n--- E1 detailed post-mortem (every full-duration block, no hidden failures) ---")
    _print_candidate_post_mortem(report.post_mortem)

    click.echo(f"\n{report.not_approved_note}")
    click.echo(
        "\nNote: this is exploratory research on simulated fills over historical data, not a claim of "
        "profitability and not approval for live or Testnet trading."
    )


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


@cli.command("shadow-run")
@click.pass_context
def shadow_run_cmd(ctx: click.Context) -> None:
    """Run one forward-only shadow cycle for the frozen
    multitimeframe_breakout_E1_round3 candidate (see shadow/engine.py).

    Fetches only completed Binance BTCUSDT candles (read-only, no API key),
    never before SHADOW_START_BOUNDARY_ISO, simulates entries/stops/targets
    against E1's own already-evaluated rules, and persists everything to
    data/shadow_agent.db. NEVER submits a Testnet or live order of any kind.
    Intended to be invoked once per completed 1h candle (e.g. by an
    external scheduler/cron) - see README.md for exact setup.
    """
    config: AppConfig = ctx.obj["config"]
    if config.mode != Mode.SHADOW:
        click.echo("The shadow-run command requires --mode shadow.", err=True)
        sys.exit(1)
    try:
        result = run_shadow_cycle(config)
    except ShadowLockError as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)
    except ShadowConfigError as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)
    click.echo(
        f"status={result.status} new_candles_fetched={result.new_candles_fetched} "
        f"segment_length={result.segment_length} min_required_candles={result.min_required_candles} "
        f"new_trades={result.new_trades_persisted} new_equity_points={result.new_equity_points_persisted} "
        f"new_journal_entries={result.new_journal_entries_persisted}"
    )
    click.echo(result.detail)
    click.echo(
        "This is a forward-only shadow simulation - no order was placed, and no profitability is claimed."
    )


@cli.command("shadow-status")
@click.pass_context
def shadow_status_cmd(ctx: click.Context) -> None:
    """Show shadow mode's current local state (no Binance call is made)."""
    config: AppConfig = ctx.obj["config"]
    if config.mode != Mode.SHADOW:
        click.echo("The shadow-status command requires --mode shadow.", err=True)
        sys.exit(1)
    switch = KillSwitch(shadow_kill_switch_path(config))
    click.echo(f"shadow_start_boundary: {SHADOW_START_BOUNDARY_ISO}")
    click.echo(f"shadow kill_switch: {'ENGAGED (' + (switch.reason() or '') + ')' if switch.is_engaged() else 'disengaged'}")
    report = build_shadow_report(config)
    state = report.run_state
    click.echo(f"total_cycles: {state.total_cycles}")
    click.echo(f"last_run_at_ms: {state.last_run_at_ms}")
    click.echo(f"last_processed_close_time_ms: {state.last_processed_close_time_ms}")
    click.echo(f"last_cycle_status: {state.last_cycle_status}  detail: {state.last_cycle_detail}")
    click.echo(
        f"data: {report.data_gaps.stored_candle_count} stored candle(s), "
        f"{report.data_gaps.latest_segment_length}/{report.data_gaps.min_required_candles} in current segment, "
        f"{report.data_gaps.gap_count} confirmed gap(s)"
    )
    click.echo(f"closed_trades: {report.performance.trade_count}")
    if report.open_position is not None:
        click.echo(
            f"open_position: entry_time_ms={report.open_position.entry_time_ms} "
            f"entry_price={report.open_position.entry_price} quantity={report.open_position.quantity} "
            f"unrealized_pnl_quote={report.open_position.unrealized_pnl_quote}"
        )
    else:
        click.echo("open_position: none")
    click.echo(report.promotion_review_note)


@cli.command("shadow-report")
@click.pass_context
def shadow_report_cmd(ctx: click.Context) -> None:
    """Print the full shadow-mode report: closed trades, win rate,
    expectancy (in R and in quote currency), drawdown, costs, longest
    losing streak, open position, and data gaps. Strictly local/read-only -
    no Binance call is ever made. See shadow/report.py.
    """
    config: AppConfig = ctx.obj["config"]
    if config.mode != Mode.SHADOW:
        click.echo("The shadow-report command requires --mode shadow.", err=True)
        sys.exit(1)
    report: ShadowReport = build_shadow_report(config)
    perf = report.performance

    click.echo(f"=== Shadow report: multitimeframe_breakout_E1_round3 (since {SHADOW_START_BOUNDARY_ISO}) ===")
    click.echo(f"closed_trades: {perf.trade_count}")
    click.echo(f"win_rate_pct: {perf.win_rate}")
    click.echo(f"expectancy_r: {report.expectancy_r}")
    click.echo(f"expectancy_quote: {report.expectancy_quote}")
    click.echo(f"profit_factor: {perf.profit_factor}")
    click.echo(f"max_drawdown_pct: {perf.max_drawdown_pct}")
    click.echo(f"starting_equity: {perf.starting_equity}  ending_equity: {perf.ending_equity}")
    click.echo(
        f"exit_reasons: strategy={perf.strategy_exit_count} stop_loss={perf.stop_loss_exit_count} "
        f"take_profit={perf.take_profit_exit_count}"
    )
    click.echo(f"longest_losing_streak: {report.longest_losing_streak}")
    click.echo(f"total_fees_paid_quote: {report.total_fees_paid_quote}")
    click.echo(f"total_slippage_cost_quote: {report.total_slippage_cost_quote}")
    click.echo(
        f"data_gaps: {report.data_gaps.gap_count} confirmed gap(s), "
        f"{report.data_gaps.total_missing_intervals} missing interval(s) total, "
        f"{report.data_gaps.stored_candle_count} stored candle(s), "
        f"current segment {report.data_gaps.latest_segment_length}/{report.data_gaps.min_required_candles}"
    )
    if report.open_position is not None:
        pos = report.open_position
        click.echo(
            f"open_position: entry_time_ms={pos.entry_time_ms} entry_price={pos.entry_price} "
            f"quantity={pos.quantity} latest_close_price={pos.latest_close_price} "
            f"unrealized_pnl_quote={pos.unrealized_pnl_quote}"
        )
    else:
        click.echo("open_position: none")
    click.echo(f"promotion_review: {report.promotion_review_note}")
    click.echo(report.not_profitable_note)


@cli.group("shadow-kill-switch")
def shadow_kill_switch_group() -> None:
    """Manually controlled shadow-mode kill switch - halts shadow-run
    entirely when engaged. Completely separate from the testnet
    `kill-switch` group and flag file."""


@shadow_kill_switch_group.command("engage")
@click.option("--reason", default="", help="Why the shadow kill switch is being engaged.")
@click.pass_context
def shadow_kill_switch_engage(ctx: click.Context, reason: str) -> None:
    config: AppConfig = ctx.obj["config"]
    switch = KillSwitch(shadow_kill_switch_path(config))
    switch.engage(reason)
    click.echo(f"Shadow kill switch ENGAGED: {switch.reason()}")


@shadow_kill_switch_group.command("disengage")
@click.pass_context
def shadow_kill_switch_disengage(ctx: click.Context) -> None:
    config: AppConfig = ctx.obj["config"]
    KillSwitch(shadow_kill_switch_path(config)).disengage()
    click.echo("Shadow kill switch DISENGAGED.")


@shadow_kill_switch_group.command("status")
@click.pass_context
def shadow_kill_switch_status(ctx: click.Context) -> None:
    config: AppConfig = ctx.obj["config"]
    switch = KillSwitch(shadow_kill_switch_path(config))
    click.echo(f"ENGAGED: {switch.reason()}" if switch.is_engaged() else "disengaged")


if __name__ == "__main__":
    cli()
