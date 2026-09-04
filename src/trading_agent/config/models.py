"""Typed, validated configuration models.

`Mode` is intentionally a two-member enum. There is no "live" member, and no
field anywhere in this module accepts a production Binance host. Adding a
third mode or a configurable trading base URL would require editing this
file directly - it cannot be done via a config file or environment variable.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class Mode(str, Enum):
    """The only two supported execution modes in V0.1."""

    BACKTEST = "backtest"
    TESTNET = "testnet"


class MarketConfig(BaseModel):
    symbol: str = "BTCUSDT"
    interval: str = "4h"
    timezone_display: str = "Australia/Sydney"

    @field_validator("interval")
    @classmethod
    def _valid_interval(cls, v: str) -> str:
        allowed = {
            "1m", "3m", "5m", "15m", "30m",
            "1h", "2h", "4h", "6h", "8h", "12h",
            "1d", "3d", "1w", "1M",
        }
        if v not in allowed:
            raise ValueError(f"interval {v!r} is not a valid Binance kline interval")
        return v


class StrategyConfig(BaseModel):
    name: str = "ema_crossover_trend"
    ema_fast: int = Field(gt=0, default=20)
    ema_slow: int = Field(gt=0, default=50)

    @model_validator(mode="after")
    def _fast_below_slow(self) -> StrategyConfig:
        if self.ema_fast >= self.ema_slow:
            raise ValueError("ema_fast must be strictly less than ema_slow")
        return self


class SizingConfig(BaseModel):
    max_allocation_pct: float = Field(gt=0, le=1, default=0.90)
    min_quote_buffer: float = Field(ge=0, default=5.0)


class FeesConfig(BaseModel):
    taker_fee_pct: float = Field(ge=0, default=0.001)
    maker_fee_pct: float = Field(ge=0, default=0.001)
    slippage_pct: float = Field(ge=0, default=0.0005)


class RiskConfig(BaseModel):
    max_position_pct: float = Field(gt=0, le=1, default=0.90)
    max_risk_per_trade_pct: float = Field(gt=0, le=1, default=0.90)
    max_daily_loss_pct: float = Field(gt=0, le=1, default=0.05)
    max_drawdown_pct: float = Field(gt=0, le=1, default=0.15)
    max_trades_per_day: int = Field(gt=0, default=4)
    cooldown_bars_after_loss: int = Field(ge=0, default=2)
    stale_data_max_age_seconds: int = Field(gt=0, default=21600)
    min_quote_balance: float = Field(ge=0, default=5.0)
    max_consecutive_api_errors: int = Field(gt=0, default=3)


class StopLossConfig(BaseModel):
    """A fixed-percentage protective stop, used only in backtesting.

    There is no verified exchange-resident protective order on Testnet in
    this revision (see RISK_POLICY.md), so automatic entry is disabled
    there entirely; this config only drives the backtest engine's
    stop-loss simulation and risk-budget position sizing. A fixed
    percentage is a deliberately simple, explainable placeholder - not a
    claim that it is an optimal or sophisticated stop-placement method
    (e.g. ATR-based stops are a natural future improvement).
    """

    stop_distance_pct: float = Field(gt=0, lt=1, default=0.05)


class BacktestConfig(BaseModel):
    train_fraction: float = Field(gt=0, lt=1, default=0.6)
    validation_fraction: float = Field(gt=0, lt=1, default=0.2)
    test_fraction: float = Field(gt=0, lt=1, default=0.2)
    min_trades_for_significance: int = Field(gt=0, default=20)

    #: How a historical candle series with a confirmed gap is handled.
    #: "reject" is the original strict behavior (any gap raises, exactly
    #: like live/Testnet's validate_candle_sequence). "segment" - the
    #: default for this research-only command - splits the series into
    #: independent contiguous segments around each confirmed gap instead
    #: of discarding the whole series; see backtest/engine.py's module
    #: docstring. Live/Testnet signal generation never reads this field
    #: and always behaves as "reject" - see execution/live_runner.py.
    gap_policy: Literal["reject", "segment"] = "segment"

    #: When True (the default), a segment that ends with a still-open
    #: position (its exit lies beyond a confirmed gap, so this segment's
    #: own data cannot resolve it) is excluded from the aggregate/overall
    #: trade statistics - its open position is a genuinely unresolved
    #: research condition, not a completed trade, and no exit price is
    #: ever invented for it. Set False to include such a segment's
    #: already-completed trades anyway (the unresolved open position
    #: itself is still never given an invented exit).
    exclude_open_position_segments: bool = True

    @model_validator(mode="after")
    def _fractions_sum_to_one(self) -> BacktestConfig:
        total = self.train_fraction + self.validation_fraction + self.test_fraction
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"train/validation/test fractions must sum to 1.0, got {total}")
        return self


class PathsConfig(BaseModel):
    data_dir: Path = Path("data")
    logs_dir: Path = Path("logs")
    db_path: Path = Path("data/trading_agent.db")


class AppConfig(BaseModel):
    mode: Mode
    market: MarketConfig = MarketConfig()
    strategy: StrategyConfig = StrategyConfig()
    sizing: SizingConfig = SizingConfig()
    fees: FeesConfig = FeesConfig()
    risk: RiskConfig = RiskConfig()
    stop_loss: StopLossConfig = StopLossConfig()
    backtest: BacktestConfig = BacktestConfig()
    paths: PathsConfig = PathsConfig()

    model_config = {"extra": "forbid"}


class Secrets(BaseModel):
    """Binance Spot Testnet credentials, loaded from environment only.

    `__repr__`/`__str__` are overridden so these values can never be
    accidentally printed or logged in full.
    """

    testnet_api_key: str = Field(min_length=1)
    testnet_api_secret: str = Field(min_length=1)

    model_config = {"extra": "forbid"}

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "Secrets(testnet_api_key=***redacted***, testnet_api_secret=***redacted***)"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.__repr__()
