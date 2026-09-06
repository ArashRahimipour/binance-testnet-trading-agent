"""Proofs for the Telegram config/secrets pieces added to `config/models.py`
and `config/loader.py` - mirrors `test_secrets.py`'s coverage of the
existing `Secrets`/`load_secrets` pair, but for the opposite fail-mode:
Telegram credentials are entirely OPTIONAL (missing just means shadow mode
never sends a notification), never fail-closed like Testnet credentials.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from trading_agent.config.loader import load_telegram_secrets
from trading_agent.config.models import AppConfig, TelegramConfig, TelegramSecrets


def test_telegram_secrets_repr_never_reveals_values():
    secrets = TelegramSecrets(bot_token="123456:super-secret-bot-token-value", chat_id="super-secret-chat-id")
    assert "super-secret" not in repr(secrets)
    assert "super-secret" not in str(secrets)
    assert "redacted" in repr(secrets)


def test_telegram_secrets_require_nonempty_values():
    with pytest.raises(ValidationError):
        TelegramSecrets(bot_token="", chat_id="")


def test_load_telegram_secrets_returns_none_when_missing(monkeypatch, tmp_path):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    empty_env = tmp_path / ".env.missing"
    empty_env.write_text("")
    assert load_telegram_secrets(env_file=empty_env) is None


def test_load_telegram_secrets_returns_none_when_only_one_is_set(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "some-token")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    empty_env = tmp_path / ".env.missing"
    empty_env.write_text("")
    assert load_telegram_secrets(env_file=empty_env) is None


def test_load_telegram_secrets_returns_none_when_blank(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "   ")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "   ")
    empty_env = tmp_path / ".env.missing"
    empty_env.write_text("")
    assert load_telegram_secrets(env_file=empty_env) is None


def test_load_telegram_secrets_reads_from_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-bot-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "fake-chat-id")
    empty_env = tmp_path / ".env.missing"
    empty_env.write_text("")
    secrets = load_telegram_secrets(env_file=empty_env)
    assert secrets is not None
    assert secrets.bot_token == "fake-bot-token"
    assert secrets.chat_id == "fake-chat-id"


def test_load_telegram_secrets_never_raises_unlike_load_secrets(monkeypatch, tmp_path):
    """The opposite fail-mode from `load_secrets` (Testnet credentials,
    which fail closed) - a missing/blank Telegram credential is entirely
    expected and must never raise, since shadow trading itself never
    depends on Telegram being configured."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    empty_env = tmp_path / ".env.missing"
    empty_env.write_text("")
    result = load_telegram_secrets(env_file=empty_env)  # must not raise
    assert result is None


# --- TelegramConfig: disabled by default, never carries secrets ------------


def test_telegram_config_defaults_to_disabled():
    assert TelegramConfig().enabled is False


def test_telegram_config_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        TelegramConfig(enabled=True, bot_token="should-never-be-a-yaml-field")  # type: ignore[call-arg]


def test_app_config_telegram_defaults_to_disabled():
    config = AppConfig(mode="backtest")
    assert config.telegram.enabled is False


def test_app_config_telegram_can_be_enabled():
    config = AppConfig(mode="shadow", telegram={"enabled": True})
    assert config.telegram.enabled is True
