"""Concrete, inspectable run-level evidence for one continuous backtest run
(a segment in `backtest/engine.py::run_backtest`, or one window in
`run_independent_holdout_evaluation`).

Lives here (not in `backtest/engine.py`) so `metrics/extended_report.py`
can depend on these types without a circular import - `engine.py` builds
and consumes both.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class ShutdownActivation:
    """Evidence for one risk-gate rejection reason code within a single
    continuous run - see the module docstring's discussion of why a
    drawdown shutdown, once triggered while flat, can never self-recover
    within that same run."""

    reason_code: str
    first_activated_time_ms: int
    equity_at_activation: Decimal
    drawdown_pct_at_activation: float
    last_active_time_ms: int
    blocked_buy_count: int
    #: True when no BUY was approved again after this code first activated
    #: - i.e. this run never recovered from it before it ended.
    remained_latched_to_end: bool
    duration_ms: int


@dataclass(frozen=True, slots=True)
class RunDiagnostics:
    """Concrete, inspectable evidence for one continuous run - exists so a
    claim like "the drawdown shutdown caused zero trades" is always backed
    by exact counts and timestamps, never left as inference.
    """

    buy_signals_generated: int
    exit_signals_generated: int
    unexecuted_signals: int
    executed_entries: int
    executed_strategy_exits: int
    executed_stop_loss_exits: int
    rejected_entries_by_reason: dict[str, int]
    first_executed_trade_time_ms: int | None
    last_executed_trade_time_ms: int | None
    max_drawdown_pct: float
    max_drawdown_time_ms: int | None
    shutdown_activations: dict[str, ShutdownActivation]
    starting_equity: Decimal
    ending_equity: Decimal
    ending_cash_quote: Decimal
    ending_base_quantity: Decimal
    ends_with_open_position: bool


@dataclass(frozen=True, slots=True)
class OpenPositionInfo:
    """The still-open position at the end of a run, if any - carried
    separately from `RunDiagnostics` so its entry economics (price paid,
    pre-slippage reference, fee already paid) are available for unrealized
    PnL reporting without inventing an exit."""

    entry_time_ms: int
    entry_price: Decimal
    entry_reference_price: Decimal
    quantity: Decimal
    entry_fee_quote: Decimal
