"""Command-line entry point.

The `--mode` option is a `click.Choice` restricted to the two Mode enum
values ("backtest", "testnet"). There is no "live" choice and no way to pass
one - click rejects any other value before any application code runs.
"""

from __future__ import annotations

import sys

import click

from trading_agent.config import AppConfig, Mode, load_config


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


if __name__ == "__main__":
    cli()
