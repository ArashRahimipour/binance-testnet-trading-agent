"""Freezing a RESEARCH_SURVIVOR before any future paper testing.

A RESEARCH_SURVIVOR (`research/scorecard.py`) has only ever seen pre-cutoff
development data. Before it may be tested again, it must be FROZEN: its
exact candidate id/family/params, the scorecard verdict that produced the
survivor status, and - as of this pre-real-evaluation code review
correction - a full set of REPRODUCIBILITY FINGERPRINTS (`research/
fingerprint.py`) covering everything that could silently change its
behavior between now and any future paper test: the strategy's own
implementation source, the shared EXECUTION-SEMANTICS modules that decide
what happens to a Signal once emitted (backtest engine, simulated broker,
portfolio/accounting, risk engine, sizing, exchange-filter validation,
order validation, performance/PnL calculations - kept as a SEPARATE
fingerprint from the strategy's own source so a drift report can say which
layer changed), its declared registry entry, the relevant slice of config
(interval, starting equity, fees, slippage, sizing, stop-loss, every risk
limit), and the symbol's exchange filters. A forward-only
freeze boundary is recorded too - the earliest timestamp any future test
candle must be at or after. Previously observed data (both the pre-cutoff
development data AND the already-consumed 2025-05-16..2026-09-04 period)
can NEVER become a new "untouched" holdout for a frozen candidate -
`validate_future_paper_test` enforces this by rejecting any candle before
the freeze boundary.

FAIL-CLOSED reloading: `assert_frozen_candidate_matches_current_implementation`
recomputes every fingerprint fresh from the CURRENT strategy code, registry
entry, config, and exchange filters, and raises
`FrozenCandidateImplementationDriftError` the moment any one of them no
longer matches what was actually frozen. A frozen candidate whose
implementation or configuration has since changed must NEVER be silently
reused for a "next" test on the assumption that the change was harmless -
the only sanctioned response is `new_candidate_version_migration_id`: mint
a new candidate id/version and re-run the full evaluation + scorecard
under it, never edit or silently overwrite the old frozen record.
`save_frozen_candidate` itself refuses (`FrozenCandidateVersionConflict`)
to overwrite an existing frozen file whose fingerprints differ from the
one being saved, for the same reason.

This module does not fetch data, schedule anything, or place any order -
consistent with this phase's explicit prohibitions (no Testnet BUY, no
production execution, no scheduling, no leverage/futures/shorting/forex/
ML/news trading/copy trading).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from trading_agent.config.models import AppConfig
from trading_agent.data.models import Candle
from trading_agent.research.candidate_registry import CandidateSpec
from trading_agent.research.cutoff import RESEARCH_CUTOFF_MS
from trading_agent.research.fingerprint import (
    compute_candidate_registry_fingerprint,
    compute_config_fingerprint,
    compute_exchange_filters_fingerprint,
    compute_execution_semantics_fingerprint,
    compute_scorecard_result_fingerprint,
    compute_strategy_implementation_fingerprint,
    get_source_commit_hash,
    relevant_config_snapshot,
    symbol_and_exchange_filters_snapshot,
)
from trading_agent.research.scorecard import ScorecardEntry, ScorecardStatus
from trading_agent.sizing.exchange_filters import SymbolFilters

#: The first version of any candidate id's frozen lineage. A later,
#: intentional implementation/config change must mint a NEW id via
#: `new_candidate_version_migration_id` with an incremented version -
#: never bump this in place on an existing frozen record.
INITIAL_CANDIDATE_VERSION = 1


class FrozenCandidateForwardTestViolation(Exception):
    """Raised when a candle predating a frozen candidate's freeze boundary
    is offered as part of its "next" paper test."""


class FrozenCandidateImplementationDriftError(Exception):
    """Raised when a frozen candidate's stored fingerprints no longer
    match the CURRENT strategy implementation, execution semantics
    (backtest engine/broker/portfolio/risk/sizing/exchange-filter-
    validation/order-validation/performance code), candidate registry
    entry, relevant configuration, or exchange filters. Deliberately fail-closed:
    a future paper-test attempt must be blocked outright, never silently
    run against a strategy or config that has since changed from what was
    actually evaluated and frozen. See `new_candidate_version_migration_id`
    for the sanctioned way to respond to an intentional change.
    """


class FrozenCandidateVersionConflict(Exception):
    """Raised by `save_frozen_candidate` when a frozen file already exists
    at the target path with DIFFERENT fingerprints than the record being
    saved. A frozen record is never silently overwritten with a changed
    one - mint a new candidate id/version instead (see
    `new_candidate_version_migration_id`)."""


@dataclass(frozen=True, slots=True)
class FrozenCandidateRecord:
    candidate_id: str
    family: str
    params: dict[str, Any]
    frozen_at_ms: int
    #: Earliest timestamp (epoch ms) any future test candle for this
    #: candidate must be at or after. Never data this or any prior
    #: evaluation already observed.
    freeze_boundary_ms: int
    scorecard_status: str
    scorecard_reasons: list[str]

    #: Monotonically-increasing version for THIS candidate id's frozen
    #: lineage - see `new_candidate_version_migration_id`.
    candidate_version: int
    #: The immutable research cutoff (`research/cutoff.py::
    #: RESEARCH_CUTOFF_MS`) in effect when this candidate was frozen.
    research_cutoff_ms: int

    #: Reproducibility fingerprints (SHA-256 hex digests) - see
    #: `research/fingerprint.py`. ANY mismatch on reload must fail closed.
    #: `strategy_implementation_fingerprint` and
    #: `execution_semantics_fingerprint` are deliberately SEPARATE so a
    #: drift report can say which layer changed: the candidate's own
    #: decision logic, or the shared simulation machinery (engine/broker/
    #: portfolio/risk/sizing/exchange-filter-validation/order-validation/
    #: performance) every candidate and the frozen baseline run through.
    strategy_implementation_fingerprint: str
    execution_semantics_fingerprint: str
    candidate_registry_fingerprint: str
    config_fingerprint: str
    exchange_filters_fingerprint: str
    scorecard_result_fingerprint: str

    #: Human-readable, non-secret snapshots corresponding to the
    #: fingerprints above - present so a frozen record can be inspected
    #: without recomputing anything. Never contains secrets or API
    #: credentials (`AppConfig` has no field for them at all - see
    #: `research/fingerprint.py`'s module docstring).
    config_snapshot: dict[str, Any]
    symbol: str
    exchange_filters_snapshot: dict[str, Any]

    #: Best-effort `git rev-parse HEAD` at freeze time - None when
    #: unavailable (not a git checkout, git missing, etc.). Never required
    #: for fail-closed matching; the fingerprints above are authoritative.
    source_commit_hash: str | None


def freeze_candidate(
    entry: ScorecardEntry,
    frozen_at_ms: int,
    freeze_boundary_ms: int,
    candidate: CandidateSpec,
    config: AppConfig,
    filters: SymbolFilters,
    candidate_version: int = INITIAL_CANDIDATE_VERSION,
) -> FrozenCandidateRecord:
    """Freeze a RESEARCH_SURVIVOR scorecard entry, together with every
    fingerprint needed to detect drift before any future paper test.
    Raises `ValueError` for any other status - a REJECTED or
    INSUFFICIENT_EVIDENCE candidate is never frozen (there is nothing to
    preserve for future paper testing). `candidate` MUST be the same
    `CandidateSpec` (id/family/params) that produced `entry`."""
    if entry.status != ScorecardStatus.RESEARCH_SURVIVOR:
        raise ValueError(f"only a RESEARCH_SURVIVOR may be frozen, got {entry.status.value} for {entry.candidate_id!r}")
    if candidate.candidate_id != entry.candidate_id:
        raise ValueError(
            f"candidate {candidate.candidate_id!r} does not match scorecard entry {entry.candidate_id!r}"
        )
    return FrozenCandidateRecord(
        candidate_id=entry.candidate_id,
        family=entry.family,
        params=dict(entry.params),
        frozen_at_ms=frozen_at_ms,
        freeze_boundary_ms=freeze_boundary_ms,
        scorecard_status=entry.status.value,
        scorecard_reasons=list(entry.reasons),
        candidate_version=candidate_version,
        research_cutoff_ms=RESEARCH_CUTOFF_MS,
        strategy_implementation_fingerprint=compute_strategy_implementation_fingerprint(candidate),
        execution_semantics_fingerprint=compute_execution_semantics_fingerprint(),
        candidate_registry_fingerprint=compute_candidate_registry_fingerprint(candidate),
        config_fingerprint=compute_config_fingerprint(config),
        exchange_filters_fingerprint=compute_exchange_filters_fingerprint(filters),
        scorecard_result_fingerprint=compute_scorecard_result_fingerprint(entry),
        config_snapshot=relevant_config_snapshot(config),
        symbol=filters.symbol,
        exchange_filters_snapshot=symbol_and_exchange_filters_snapshot(filters),
        source_commit_hash=get_source_commit_hash(),
    )


def new_candidate_version_migration_id(candidate_id: str, new_version: int) -> str:
    """The ONLY sanctioned way to react to an intentional strategy
    implementation or config change for an already-frozen candidate: mint
    a NEW candidate id (never edit or silently overwrite the old frozen
    file) so the old and new frozen artifacts coexist side by side, each
    permanently traceable to exactly the fingerprints that were true when
    IT was frozen. The caller must then re-run the full blocked
    chronological evaluation and scorecard under this new id before
    freezing it with `candidate_version=new_version`.
    """
    if new_version <= INITIAL_CANDIDATE_VERSION:
        raise ValueError(f"new_version must be > {INITIAL_CANDIDATE_VERSION}, got {new_version}")
    return f"{candidate_id}__v{new_version}"


def _fingerprints_match(record: FrozenCandidateRecord, other: FrozenCandidateRecord) -> bool:
    return (
        record.strategy_implementation_fingerprint == other.strategy_implementation_fingerprint
        and record.execution_semantics_fingerprint == other.execution_semantics_fingerprint
        and record.candidate_registry_fingerprint == other.candidate_registry_fingerprint
        and record.config_fingerprint == other.config_fingerprint
        and record.exchange_filters_fingerprint == other.exchange_filters_fingerprint
    )


def save_frozen_candidate(record: FrozenCandidateRecord, directory: Path) -> Path:
    """Persist `record`. Refuses (`FrozenCandidateVersionConflict`) to
    overwrite an existing frozen file at the same path whose fingerprints
    differ from `record` - re-saving an identical record (e.g. a retried
    freeze of the same run) is fine, but a DIFFERENT record must go
    through `new_candidate_version_migration_id` instead."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{record.candidate_id}.json"
    if path.exists():
        existing = load_frozen_candidate(path)
        if not _fingerprints_match(record, existing):
            raise FrozenCandidateVersionConflict(
                f"a frozen candidate record already exists at {path!s} with DIFFERENT fingerprints "
                f"than the one being saved for {record.candidate_id!r}. A frozen record is never "
                "silently overwritten - use `new_candidate_version_migration_id` to mint a new "
                "candidate id/version, re-run the full evaluation under it, and freeze that instead."
            )
    path.write_text(json.dumps(asdict(record), indent=2, sort_keys=True))
    return path


def load_frozen_candidate(path: Path) -> FrozenCandidateRecord:
    data = json.loads(path.read_text())
    return FrozenCandidateRecord(**data)


def validate_future_paper_test(record: FrozenCandidateRecord, candles: list[Candle]) -> None:
    """Raise `FrozenCandidateForwardTestViolation` if ANY candle predates
    `record.freeze_boundary_ms`. A frozen candidate's next valid test must
    use ONLY genuinely new data that arrived after the freeze - never data
    this or any prior evaluation already observed.

    This checks DATA freshness only. Before ever calling this, the caller
    must ALSO call `assert_frozen_candidate_matches_current_implementation`
    - a fresh, never-before-seen candle offered to a frozen candidate
    whose implementation has since drifted is still not a valid test.
    """
    offending = [c for c in candles if c.open_time_ms < record.freeze_boundary_ms]
    if offending:
        raise FrozenCandidateForwardTestViolation(
            f"{len(offending)} candle(s) predate frozen candidate {record.candidate_id!r}'s freeze "
            f"boundary ({record.freeze_boundary_ms}ms). Previously observed data can never become a "
            "new 'untouched' holdout for a frozen candidate - its next valid test requires candles "
            "that arrived strictly after the freeze."
        )


def assert_frozen_candidate_matches_current_implementation(
    record: FrozenCandidateRecord, candidate: CandidateSpec, config: AppConfig, filters: SymbolFilters
) -> None:
    """FAIL CLOSED: recompute every fingerprint fresh from the CURRENT
    strategy implementation, candidate registry entry, relevant config,
    and exchange filters, and raise `FrozenCandidateImplementationDriftError`
    the moment any of them no longer matches what was recorded when
    `record` was frozen. Must be called - together with
    `validate_future_paper_test` - before any future paper test of a
    frozen candidate is ever run; neither check alone is sufficient.
    """
    if candidate.candidate_id != record.candidate_id:
        raise ValueError(f"candidate {candidate.candidate_id!r} does not match frozen record {record.candidate_id!r}")

    mismatches: list[str] = []
    if compute_strategy_implementation_fingerprint(candidate) != record.strategy_implementation_fingerprint:
        mismatches.append("strategy implementation (candidate source code or a shared indicator module changed)")
    if compute_execution_semantics_fingerprint() != record.execution_semantics_fingerprint:
        mismatches.append(
            "execution semantics (backtest engine, broker, portfolio/accounting, risk engine, sizing, "
            "exchange-filter validation, order validation, or performance/PnL calculations changed)"
        )
    if compute_candidate_registry_fingerprint(candidate) != record.candidate_registry_fingerprint:
        mismatches.append("candidate registry entry (declared id/family/params changed)")
    if compute_config_fingerprint(config) != record.config_fingerprint:
        mismatches.append("relevant configuration (interval/starting_equity/fees/sizing/stop_loss/risk changed)")
    if compute_exchange_filters_fingerprint(filters) != record.exchange_filters_fingerprint:
        mismatches.append("symbol/exchange-filter snapshot (exchange filters changed)")

    if mismatches:
        raise FrozenCandidateImplementationDriftError(
            f"frozen candidate {record.candidate_id!r} (version {record.candidate_version}) no longer "
            f"matches the current: {'; '.join(mismatches)}. Loading it for a future paper test is "
            "REFUSED (fail-closed) - the frozen result was produced by a different implementation or "
            "configuration and cannot be trusted to describe the current one. Use "
            "`new_candidate_version_migration_id` to mint a new candidate version, re-run the full "
            "blocked chronological evaluation and scorecard under it, and freeze that instead - never "
            "force this record through unchanged."
        )
