"""Deterministic, non-secret fingerprints for reproducible candidate freezing.

`research/freeze.py` records these fingerprints on every `FrozenCandidateRecord`
so a future attempt to reuse a frozen candidate can FAIL CLOSED the moment
anything that could change its behavior has drifted since it was frozen -
the candidate's own strategy source code, the shared causal indicator
modules it depends on, its declared registry entry, the relevant slice of
`AppConfig`, or the symbol's exchange filters. Every fingerprint here is a
plain SHA-256 hex digest of a stably-serialized (sorted-key JSON) payload,
so two runs of this process on identical inputs always produce identical
fingerprints - no timestamps, randomness, or process-specific state ever
enters a fingerprint.

No secrets or API credentials are ever fingerprinted or snapshotted here:
`AppConfig` (see `config/models.py`) has no field for them at all - Binance
Testnet credentials live exclusively in the separate `Secrets` model,
loaded independently via `config/loader.py::load_secrets` and never merged
into `AppConfig` - so `relevant_config_snapshot` structurally cannot leak
one even by accident.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import subprocess
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

from trading_agent.backtest import engine as backtest_engine
from trading_agent.config.models import AppConfig
from trading_agent.execution import backtest_broker, order_validator
from trading_agent.indicators import moving_averages, volatility
from trading_agent.metrics import diagnostics as metrics_diagnostics
from trading_agent.metrics import extended_report, performance
from trading_agent.portfolio import state as portfolio_state
from trading_agent.research.candidate_registry import CandidateSpec
from trading_agent.risk import engine as risk_engine_module
from trading_agent.risk import limits as risk_limits
from trading_agent.sizing import exchange_filters as exchange_filters_module
from trading_agent.sizing import position_sizer
from trading_agent.sizing.exchange_filters import SymbolFilters
from trading_agent.strategy import base as strategy_base

if TYPE_CHECKING:
    from trading_agent.research.scorecard import ScorecardEntry

#: Every shared module capable of changing a SIMULATED result once a
#: strategy has emitted a Signal: the backtest engine itself, the
#: simulated broker, portfolio/accounting, the risk engine and its
#: context/intent types, position sizing, exchange-filter validation, the
#: order validator the simulation runs every intent through, and the
#: performance/PnL calculations reported from the result. Deliberately
#: SEPARATE from `compute_strategy_implementation_fingerprint` (which
#: covers only what decides WHEN to signal) so a fail-closed drift report
#: names which layer changed - a strategy's own logic being untouched
#: while the engine's fill/accounting logic changed is a materially
#: different (and equally disqualifying) kind of drift.
EXECUTION_SEMANTICS_MODULES: tuple[ModuleType, ...] = (
    backtest_engine,
    backtest_broker,
    portfolio_state,
    risk_engine_module,
    risk_limits,
    position_sizer,
    exchange_filters_module,
    performance,
    extended_report,
    metrics_diagnostics,
    order_validator,
)


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"cannot fingerprint value of type {type(value)!r}")


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=_json_default)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _module_source_fingerprint(modules: list[ModuleType] | tuple[ModuleType, ...]) -> str:
    """Hash of the concatenated Python source of every module in
    `modules`, each tagged with its own name so two modules can never
    collide, and in a fixed (never input-order-dependent) sequence so the
    same module set always hashes identically regardless of how callers
    assembled the list."""
    combined = "\n".join(
        f"### {m.__name__} ###\n{inspect.getsource(m)}" for m in sorted(modules, key=lambda m: m.__name__)
    )
    return _sha256(combined)


def compute_strategy_implementation_fingerprint(candidate: CandidateSpec) -> str:
    """Hash of the actual Python source that decides WHEN this candidate
    signals: its own family module (`research/candidates/*.py`) plus the
    shared causal indicator/strategy-contract modules every candidate
    depends on (`indicators/moving_averages.py`, `indicators/
    volatility.py`, `strategy/base.py`). Changing any byte of any of
    these files - including a comment or docstring - changes this
    fingerprint; that is intentional, since fail-closed loading
    (`assert_frozen_candidate_matches_current_implementation` below) must
    never have to guess whether a source change was "just" cosmetic.

    Deliberately covers ONLY signal-decision code - see
    `compute_execution_semantics_fingerprint` for the separate fingerprint
    covering everything that can change a SIMULATED result once a Signal
    has already been emitted.
    """
    strategy = candidate.build()
    strategy_module = inspect.getmodule(type(strategy))
    if strategy_module is None:
        raise ValueError(f"cannot resolve source module for candidate {candidate.candidate_id!r}")
    modules = [strategy_module, moving_averages, volatility, strategy_base]
    return _module_source_fingerprint(modules)


def compute_execution_semantics_fingerprint() -> str:
    """Hash of the actual Python source of every shared module that can
    change a SIMULATED result once a strategy has already emitted a
    Signal - see `EXECUTION_SEMANTICS_MODULES` for the exact list
    (backtest engine, simulated broker, portfolio/accounting, risk engine
    and its context/intent types, position sizing, exchange-filter
    validation, order validation, and performance/PnL calculations).
    Deliberately kept SEPARATE from `compute_strategy_implementation_fingerprint`:
    a fail-closed drift report must be able to say WHICH layer changed -
    the candidate's own decision logic, or the shared simulation
    machinery every candidate (and the frozen baseline) runs through -
    since these are different bugs with different implications for a
    frozen result's trustworthiness.
    """
    return _module_source_fingerprint(EXECUTION_SEMANTICS_MODULES)


def compute_candidate_registry_fingerprint(candidate: CandidateSpec) -> str:
    """Hash of exactly this candidate's declared id/family/params, as they
    currently stand in `research/candidate_registry.py` - drifts the
    moment anyone edits a previously-frozen candidate's declared
    parameters."""
    payload = {"candidate_id": candidate.candidate_id, "family": candidate.family, "params": candidate.params}
    return _sha256(_stable_json(payload))


def relevant_config_snapshot(config: AppConfig) -> dict[str, Any]:
    """The slice of `AppConfig` that can actually change a candidate's
    simulated behavior: interval, starting equity, fees, slippage,
    sizing, stop-loss, and every risk limit. Deliberately excludes
    `paths` (local filesystem layout, not behavior) and `strategy` (the
    frozen v0.1 baseline's own config, irrelevant to a research
    candidate). Contains no secrets - see module docstring."""
    return {
        "interval": config.market.interval,
        "starting_equity": config.backtest.starting_equity,
        "gap_policy": config.backtest.gap_policy,
        "fees": config.fees.model_dump(),
        "sizing": config.sizing.model_dump(),
        "stop_loss": config.stop_loss.model_dump(),
        "risk": config.risk.model_dump(),
    }


def compute_config_fingerprint(config: AppConfig) -> str:
    return _sha256(_stable_json(relevant_config_snapshot(config)))


def symbol_and_exchange_filters_snapshot(filters: SymbolFilters) -> dict[str, Any]:
    return {key: (str(value) if isinstance(value, Decimal) else value) for key, value in asdict(filters).items()}


def compute_exchange_filters_fingerprint(filters: SymbolFilters) -> str:
    return _sha256(_stable_json(symbol_and_exchange_filters_snapshot(filters)))


def compute_scorecard_result_fingerprint(entry: ScorecardEntry) -> str:
    """Hash of exactly the report/result data that produced this entry's
    status - every criterion (name, pass/fail, detail) plus every scoring
    metric - so a frozen record is tied to the specific evidence that made
    it a RESEARCH_SURVIVOR, not just to the candidate's identity."""
    payload = {
        "candidate_id": entry.candidate_id,
        "status": entry.status.value,
        "criteria": [{"name": c.name, "passed": c.passed, "detail": c.detail} for c in entry.criteria],
        "reasons": entry.reasons,
        "total_trade_count": entry.total_trade_count,
        "blocks_with_a_trade": entry.blocks_with_a_trade,
        "block_count": entry.block_count,
        "median_block_realized_return_pct": entry.median_block_realized_return_pct,
        "worst_block_realized_return_pct": entry.worst_block_realized_return_pct,
        "aggregate_realized_pnl_quote": entry.aggregate_realized_pnl_quote,
        "positive_realized_pnl_block_fraction": entry.positive_realized_pnl_block_fraction,
        "max_best_trade_contribution_pct": entry.max_best_trade_contribution_pct,
        "worst_block_max_drawdown_pct": entry.worst_block_max_drawdown_pct,
    }
    return _sha256(_stable_json(payload))


def get_source_commit_hash(repo_dir: Path | None = None) -> str | None:
    """Best-effort `git rev-parse HEAD` in `repo_dir` (default: this
    file's own directory tree). Returns None - never raises - if git is
    unavailable, this isn't a git checkout, or anything else goes wrong;
    "when available" per the freezing requirement, never a hard
    dependency."""
    cwd = repo_dir if repo_dir is not None else Path(__file__).resolve().parent
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True, timeout=5, check=False
        )
    except Exception:  # noqa: BLE001 - best-effort only, never fatal
        return None
    if result.returncode != 0:
        return None
    commit_hash = result.stdout.strip()
    return commit_hash or None
