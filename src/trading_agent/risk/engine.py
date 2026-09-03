"""The independent risk engine.

Every proposed trade (BUY or EXIT) passes through `RiskEngine.evaluate`
before it may reach the order validator or the broker. The strategy never
calls the broker directly and never sees these limits - it only produces a
Signal (Phase 2); this engine is what actually decides whether acting on it
is currently allowed.

Design note on "maximum risk per trade": the baseline strategy (Phase 2)
has no stop-loss price - its only exit mechanism is the trend-reversal
signal itself. Without a stop distance, there is no principled way to
compute "amount at risk" the way a stop-based strategy could. `max_risk_per_
trade_pct` is therefore enforced conservatively as an upper bound on the
trade's notional value as a fraction of equity, same as `max_position_pct`
but intended to be set tighter - effectively "how much of the account can
this one trade ever represent". This is documented explicitly rather than
presented as true stop-distance risk sizing. See RISK_POLICY.md.

Universal gates (kill switch, stale data, duplicate order, consecutive API
errors) block both BUY and EXIT, because trading on bad data or during API
instability is unsafe regardless of direction. Portfolio-performance gates
(drawdown, daily loss, trade count, cooldown, minimum balance, position
sizing limits) apply only to BUY - an EXIT reduces risk and is capital-
preservation-positive, so it is never blocked by having "used up" a daily
allowance.
"""

from __future__ import annotations

from decimal import Decimal

from trading_agent.config.models import RiskConfig
from trading_agent.risk.limits import RiskContext, RiskDecision, TradeIntent
from trading_agent.strategy.base import SignalType


class RiskEngine:
    def __init__(self, config: RiskConfig) -> None:
        self._config = config

    def evaluate(self, intent: TradeIntent, context: RiskContext) -> RiskDecision:
        universal = self._check_universal_gates(context)
        if universal is not None:
            return universal

        if intent.signal_type == SignalType.EXIT:
            return RiskDecision(True, "APPROVED_EXIT_ALWAYS_ALLOWED")

        return self._check_buy_gates(intent, context)

    def _check_universal_gates(self, context: RiskContext) -> RiskDecision | None:
        if context.kill_switch_engaged:
            return RiskDecision(False, "KILL_SWITCH_ENGAGED")
        if context.consecutive_api_errors >= self._config.max_consecutive_api_errors:
            return RiskDecision(False, "CONSECUTIVE_API_ERROR_SHUTDOWN")
        if context.data_age_seconds > self._config.stale_data_max_age_seconds:
            return RiskDecision(False, "STALE_DATA")
        if context.is_duplicate_order:
            return RiskDecision(False, "DUPLICATE_ORDER_BLOCKED")
        return None

    def _check_buy_gates(self, intent: TradeIntent, context: RiskContext) -> RiskDecision:
        if context.current_drawdown_pct >= self._config.max_drawdown_pct:
            return RiskDecision(False, "MAX_DRAWDOWN_SHUTDOWN")
        if context.daily_realized_pnl_pct <= -self._config.max_daily_loss_pct:
            return RiskDecision(False, "MAX_DAILY_LOSS_SHUTDOWN")
        if context.trades_today >= self._config.max_trades_per_day:
            return RiskDecision(False, "MAX_TRADES_PER_DAY_REACHED")
        if context.cooldown_bars_remaining > 0:
            return RiskDecision(False, "COOLDOWN_AFTER_LOSS_ACTIVE")
        if context.quote_balance < Decimal(str(self._config.min_quote_balance)):
            return RiskDecision(False, "BELOW_MIN_QUOTE_BALANCE")
        if context.equity <= 0:
            return RiskDecision(False, "INVALID_EQUITY")

        notional = intent.quantity * intent.price
        position_pct = notional / context.equity
        if position_pct > Decimal(str(self._config.max_position_pct)):
            return RiskDecision(False, "EXCEEDS_MAX_POSITION_PCT")
        if position_pct > Decimal(str(self._config.max_risk_per_trade_pct)):
            return RiskDecision(False, "EXCEEDS_MAX_RISK_PER_TRADE")

        return RiskDecision(True, "APPROVED")
