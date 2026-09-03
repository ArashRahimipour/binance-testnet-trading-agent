import pytest
from pydantic import ValidationError

from trading_agent.config import Secrets
from trading_agent.config.loader import load_secrets


def test_secrets_repr_never_reveals_values():
    secrets = Secrets(testnet_api_key="super-secret-key-value", testnet_api_secret="super-secret-secret-value")
    assert "super-secret" not in repr(secrets)
    assert "super-secret" not in str(secrets)


def test_secrets_require_nonempty_values():
    with pytest.raises(ValidationError):
        Secrets(testnet_api_key="", testnet_api_secret="")


def test_load_secrets_fails_closed_when_missing(monkeypatch, tmp_path):
    monkeypatch.delenv("BINANCE_TESTNET_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_TESTNET_API_SECRET", raising=False)
    empty_env = tmp_path / ".env.missing"
    empty_env.write_text("")
    with pytest.raises(ValidationError):
        load_secrets(env_file=empty_env)


def test_load_secrets_reads_from_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "fake-key")
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "fake-secret")
    empty_env = tmp_path / ".env.missing"
    empty_env.write_text("")
    secrets = load_secrets(env_file=empty_env)
    assert secrets.testnet_api_key == "fake-key"
    assert secrets.testnet_api_secret == "fake-secret"
