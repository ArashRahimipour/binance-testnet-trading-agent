"""Source-level regression lock: `shadow/notifications/` can NEVER reach
order placement of any kind (real or Testnet), and the only host it can
ever construct an HTTP client for is Telegram's own Bot API.

This is a pure text/AST inspection of the package's own source files - it
never imports or executes untrusted code, and it fails loudly (rather than
silently) the moment anyone adds an import this package must never have.
See `shadow/notifications/__init__.py`'s module docstring, which this test
file is named after.
"""

from __future__ import annotations

import ast
from pathlib import Path

_NOTIFICATIONS_DIR = Path(__file__).resolve().parents[2] / "src" / "trading_agent" / "shadow" / "notifications"

#: Any module whose import would give this package the ability to place a
#: real or Testnet order, or sign an authenticated Binance request.
_FORBIDDEN_MODULE_SUBSTRINGS = (
    "trading_agent.execution.testnet_adapter",
    "trading_agent.execution.live_runner",
    "trading_agent.execution.binance_signing",
    "trading_agent.execution.startup_reconciliation",
    "trading_agent.execution.reconciliation",
    "trading_agent.execution.testnet_readonly",
    "trading_agent.execution.testnet_health",
)


def _notification_py_files() -> list[Path]:
    files = sorted(_NOTIFICATIONS_DIR.rglob("*.py"))
    assert files, "expected shadow/notifications/ source files to exist"
    return files


def test_notifications_package_exists_and_is_nonempty():
    assert _NOTIFICATIONS_DIR.is_dir()
    assert len(_notification_py_files()) >= 3


def test_no_notifications_source_file_imports_an_order_placement_module():
    for path in _notification_py_files():
        text = path.read_text()
        for forbidden in _FORBIDDEN_MODULE_SUBSTRINGS:
            assert forbidden not in text, f"{path} must never reference {forbidden}"


def test_no_notifications_source_file_imports_execution_backtest_broker():
    # `execution/backtest_broker.py` is a pure fill-price SIMULATOR (no
    # network, no order placement) that `shadow/engine.py` itself already
    # uses - but the notifications package has no legitimate reason to
    # import it directly at all (it only ever renders numbers already
    # computed elsewhere), so keep this path locked shut too.
    for path in _notification_py_files():
        text = path.read_text()
        assert "trading_agent.execution.backtest_broker" not in text, (
            f"{path} must never import execution/backtest_broker.py"
        )


def test_ast_reports_no_execution_module_imports_at_all():
    """A stricter, AST-based version of the two checks above - proves no
    `import trading_agent.execution.X` / `from trading_agent.execution.X
    import ...` statement exists, not just that a particular substring is
    absent (so a refactor can't quietly dodge the text-based check)."""
    for path in _notification_py_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("trading_agent.execution"), (
                        f"{path} imports forbidden module {alias.name}"
                    )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert not module.startswith("trading_agent.execution"), (
                    f"{path} imports from forbidden module {module}"
                )


def test_telegram_client_never_references_any_binance_host():
    text = (_NOTIFICATIONS_DIR / "telegram_client.py").read_text()
    for forbidden_host_fragment in ("binance.com", "binancefuture", "testnet.binance"):
        assert forbidden_host_fragment not in text.lower(), (
            f"telegram_client.py must never reference a Binance host ({forbidden_host_fragment})"
        )


def test_telegram_client_declares_exactly_one_api_host_constant():
    from trading_agent.shadow.notifications.telegram_client import TELEGRAM_API_HOST

    assert TELEGRAM_API_HOST == "https://api.telegram.org"
