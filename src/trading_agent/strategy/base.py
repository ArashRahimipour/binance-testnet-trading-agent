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
