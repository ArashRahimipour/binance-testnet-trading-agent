"""Freezing a RESEARCH_SURVIVOR before any future paper testing.

A RESEARCH_SURVIVOR (`research/scorecard.py`) has only ever seen pre-cutoff
development data. Before it may be tested again, it must be FROZEN: its
exact candidate id/family/params and the scorecard verdict that produced
the survivor status are recorded immutably, together with a forward-only
boundary - the earliest timestamp any future test candle must be at or
after. Previously observed data (both the pre-cutoff development data AND
the already-consumed 2025-05-16..2026-09-04 period) can NEVER become a new
"untouched" holdout for a frozen candidate - `validate_future_paper_test`
enforces this by rejecting any candle before the freeze boundary.

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

from trading_agent.data.models import Candle
from trading_agent.research.scorecard import ScorecardEntry, ScorecardStatus


class FrozenCandidateForwardTestViolation(Exception):
    """Raised when a candle predating a frozen candidate's freeze boundary
    is offered as part of its "next" paper test."""


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


def freeze_candidate(entry: ScorecardEntry, frozen_at_ms: int, freeze_boundary_ms: int) -> FrozenCandidateRecord:
    """Freeze a RESEARCH_SURVIVOR scorecard entry. Raises `ValueError` for
    any other status - a REJECTED or INSUFFICIENT_EVIDENCE candidate is
    never frozen (there is nothing to preserve for future paper testing)."""
    if entry.status != ScorecardStatus.RESEARCH_SURVIVOR:
        raise ValueError(f"only a RESEARCH_SURVIVOR may be frozen, got {entry.status.value} for {entry.candidate_id!r}")
    return FrozenCandidateRecord(
        candidate_id=entry.candidate_id,
        family=entry.family,
        params=dict(entry.params),
        frozen_at_ms=frozen_at_ms,
        freeze_boundary_ms=freeze_boundary_ms,
        scorecard_status=entry.status.value,
        scorecard_reasons=list(entry.reasons),
    )


def save_frozen_candidate(record: FrozenCandidateRecord, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{record.candidate_id}.json"
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
    """
    offending = [c for c in candles if c.open_time_ms < record.freeze_boundary_ms]
    if offending:
        raise FrozenCandidateForwardTestViolation(
            f"{len(offending)} candle(s) predate frozen candidate {record.candidate_id!r}'s freeze "
            f"boundary ({record.freeze_boundary_ms}ms). Previously observed data can never become a "
            "new 'untouched' holdout for a frozen candidate - its next valid test requires candles "
            "that arrived strictly after the freeze."
        )
