"""Strategy signal contract.

A strategy is a pure function of (completed candles, current position) ->
Signal. It never touches a broker, never sees API keys, and never decides
order size - it only says what it would like to happen and why. Position
sizing, risk limits, and order validation (later phases) decide whether
that intent is actually acted on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable

from trading_agent.data.models import Candle


class SignalType(str, Enum):
    BUY = "buy"
    EXIT = "exit"
    HOLD = "hold"


class PositionSide(str, Enum):
    """V0.1 supports exactly two states: fully in cash, or fully long."""

    FLAT = "flat"
    LONG = "long"


@dataclass(frozen=True, slots=True)
class Signal:
    type: SignalType
    reason_code: str
    candle_close_time_ms: int
    inputs: dict = field(default_factory=dict)


class InsufficientDataError(Exception):
    """Raised when there are not enough completed candles to form a signal."""


@runtime_checkable
class SignalGenerator(Protocol):
    """The ONLY interface the backtest engine (`backtest/engine.py::
    run_segment`) requires of a strategy - a pure function of (completed
    candles, current position) -> Signal. Deliberately narrow: a strategy
    object implementing only this method has no way to reach the broker,
    fill assumptions, fees, slippage, position sizing, the risk engine, or
    accounting - all of that lives inside the engine and is never passed to
    or reachable from a strategy. Both the frozen `EmaCrossoverTrendStrategy`
    baseline and every research candidate (`research/candidates/`) satisfy
    this structurally, without needing to inherit from it.
    """

    def generate_signal(self, candles: list[Candle], current_position: PositionSide) -> Signal: ...


@runtime_checkable
class CandidateStrategy(SignalGenerator, Protocol):
    """A research candidate additionally declares its own required warm-up
    length, so a generic walk-forward evaluator (`research/walk_forward.py`)
    can size each candidate's warm-up correctly without hardcoding any
    family's specific indicator periods."""

    min_required_candles: int
