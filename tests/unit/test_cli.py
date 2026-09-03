from click.testing import CliRunner

from trading_agent.cli.main import cli


def test_config_check_default_mode():
    result = CliRunner().invoke(cli, ["config-check"])
    assert result.exit_code == 0
    assert "mode: backtest" in result.output


def test_mode_override_testnet_accepted():
    result = CliRunner().invoke(cli, ["--mode", "testnet", "config-check"])
    assert result.exit_code == 0
    assert "mode: testnet" in result.output


def test_mode_live_is_rejected_at_cli_parsing():
    result = CliRunner().invoke(cli, ["--mode", "live", "config-check"])
    assert result.exit_code != 0
    assert "Invalid value" in result.output or "invalid choice" in result.output.lower()


def test_mode_production_is_rejected_at_cli_parsing():
    result = CliRunner().invoke(cli, ["--mode", "production", "config-check"])
    assert result.exit_code != 0
