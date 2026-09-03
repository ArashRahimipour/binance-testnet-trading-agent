"""Loads validated configuration from YAML and secrets from the environment.

Secrets are read only from environment variables (populated from `.env` via
python-dotenv in local development, or real environment variables in any
other deployment). They are never read from, or written to, the YAML config.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from trading_agent.config.models import AppConfig, Secrets

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "default.yaml"


def load_config(path: str | Path | None = None, overrides: dict[str, Any] | None = None) -> AppConfig:
    """Load and validate the application config.

    Args:
        path: Path to a YAML config file. Defaults to config/default.yaml.
        overrides: Optional dict of values to merge on top (e.g. CLI flags).
    """
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    with open(config_path, "r", encoding="utf-8") as fh:
        raw: dict[str, Any] = yaml.safe_load(fh) or {}

    if overrides:
        raw = _deep_merge(raw, overrides)

    return AppConfig.model_validate(raw)


def load_secrets(env_file: str | Path | None = None) -> Secrets:
    """Load Binance Spot Testnet secrets from the environment.

    Raises pydantic.ValidationError if the required variables are missing -
    this is intentional fail-closed behavior; the agent must not start
    trading without valid testnet credentials.
    """
    load_dotenv(dotenv_path=env_file, override=False)
    return Secrets(
        testnet_api_key=os.environ.get("BINANCE_TESTNET_API_KEY", ""),
        testnet_api_secret=os.environ.get("BINANCE_TESTNET_API_SECRET", ""),
    )


def _deep_merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result
