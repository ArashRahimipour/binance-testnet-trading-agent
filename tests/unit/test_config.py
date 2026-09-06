import pytest
from pydantic import ValidationError

from trading_agent.config import AppConfig, Mode, load_config
from trading_agent.config.loader import DEFAULT_CONFIG_PATH


def test_default_config_loads_and_validates():
    config = load_config()
    assert config.mode == Mode.BACKTEST
    assert config.market.symbol == "BTCUSDT"
    assert config.market.interval == "4h"


def test_default_config_path_exists():
    assert DEFAULT_CONFIG_PATH.exists()


def test_mode_rejects_live():
    with pytest.raises(ValidationError):
        AppConfig(mode="live")


def test_mode_rejects_arbitrary_string():
    with pytest.raises(ValidationError):
        AppConfig(mode="production")


def test_mode_has_exactly_backtest_testnet_and_shadow_never_live():
    # A deliberately closed enum: BACKTEST, TESTNET, and (forward-only,
    # never-order-placing) SHADOW - and, crucially, still no "live" mode.
    assert {m.value for m in Mode} == {"backtest", "testnet", "shadow"}
    assert "live" not in {m.value for m in Mode}


def test_strategy_requires_fast_below_slow():
    with pytest.raises(ValidationError):
        AppConfig(mode="backtest", strategy={"ema_fast": 50, "ema_slow": 20})


def test_unknown_top_level_field_rejected():
    with pytest.raises(ValidationError):
        AppConfig(mode="backtest", unknown_field=123)


def test_backtest_fractions_must_sum_to_one():
    with pytest.raises(ValidationError):
        AppConfig(mode="backtest", backtest={"train_fraction": 0.5, "validation_fraction": 0.3, "test_fraction": 0.3})


def test_invalid_interval_rejected():
    with pytest.raises(ValidationError):
        AppConfig(mode="backtest", market={"interval": "7h"})
