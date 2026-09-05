"""Side-by-side comparison of the ORIGINAL round-1 blocked chronological
evaluation (`research/blocked_chronological_evaluation.py`, UNMODIFIED -
this module never edits, monkey-patches, or otherwise alters its behavior)
against the NEW duration-normalized sensitivity variant
(`research/fixed_duration_evaluation.py`), for the SAME fixed candidate on
the SAME pre-cutoff data.

Both sides are scored by the SAME, also UNMODIFIED, `research/scorecard.py
::score_candidate` - there is exactly one scoring function in this
codebase, reused verbatim for both methodologies, so any difference in the
two `ScorecardEntry` objects reflects ONLY the block-construction
methodology, never a different pass/fail rule.

`round_1_original_evaluation` is a byte-for-byte REPRODUCTION of the
already-completed, already-reported round-1 result (re-running the exact
same deterministic, unmodified function on the exact same data reproduces
it exactly - the same principle `research/post_mortem.py` and `research/
frozen_baseline.py` already rely on) - it is a label, never a recomputation
that could differ. Nothing here overwrites, mutates, or supersedes it: no
original result, scorecard, diagnosis, or frozen artifact is ever changed
by importing or calling anything in this module.

`duration_normalized_sensitivity` is EXPLICITLY NON-BINDING: it never
changes a candidate's original verdict and never creates a retroactive
`RESEARCH_SURVIVOR` - see `SENSITIVITY_NON_BINDING_NOTE`, attached to
every comparison.
"""

from __future__ import annotations

from dataclasses import dataclass

from trading_agent.config.models import AppConfig
from trading_agent.data.models import Candle
from trading_agent.journal.journal import Journal
from trading_agent.research.blocked_chronological_evaluation import (
    run_candidate_blocked_chronological_evaluation,
)
from trading_agent.research.candidate_registry import CandidateSpec
from trading_agent.research.fixed_duration_evaluation import (
    InsufficientDurationFragment,
    LeftoverPartialWindow,
    run_candidate_fixed_duration_evaluation,
)
from trading_agent.research.scorecard import ScorecardEntry, score_candidate
from trading_agent.sizing.exchange_filters import SymbolFilters

ROUND_1_LABEL = "round_1_original_evaluation"

SENSITIVITY_METHODOLOGY_NOTE = (
    "Defect being tested for: the original method (`block_count=5` blocks per gap-free segment, "
    "split purely by CANDLE COUNT) gives a tiny fragment segment the SAME five voting blocks as the "
    "dominant, multi-year segment - zero-trade micro-blocks from a small fragment can distort "
    "positive_realized_pnl_block_fraction and other per-block aggregates identically to blocks built "
    "from years of real history. This sensitivity report re-scores the SAME candidate using "
    "DURATION-NORMALIZED blocks (a fixed 365-day span each) instead - a segment too short for even "
    "one complete duration block gets ZERO voting blocks (reported as an insufficient-duration "
    "fragment), never five negative zero-trade votes."
)

SENSITIVITY_NON_BINDING_NOTE = (
    "This duration-normalized sensitivity result is NON-BINDING: it NEVER changes, overrides, or "
    "supersedes round_1_original_evaluation's status, and it NEVER retroactively creates a "
    "RESEARCH_SURVIVOR. Its only purpose is to show whether the qualitative picture (positive/negative "
    "expectancy, stability across history) is sensitive to the block-construction methodology itself."
)


@dataclass(frozen=True, slots=True)
class CandidateSensitivityComparison:
    candidate_id: str
    family: str
    params: dict[str, float | int]
    round_1_original_evaluation: ScorecardEntry
    duration_normalized_sensitivity: ScorecardEntry
    fragments: list[InsufficientDurationFragment]
    leftovers: list[LeftoverPartialWindow]
    round_1_label: str = ROUND_1_LABEL
    methodology_note: str = SENSITIVITY_METHODOLOGY_NOTE
    non_binding_note: str = SENSITIVITY_NON_BINDING_NOTE


def build_candidate_sensitivity_comparison(
    candidate: CandidateSpec,
    pre_cutoff_candles: list[Candle],
    config: AppConfig,
    filters: SymbolFilters,
    journal: Journal | None = None,
) -> CandidateSensitivityComparison:
    original_result = run_candidate_blocked_chronological_evaluation(candidate, pre_cutoff_candles, config, filters, journal)
    original_entry = score_candidate(original_result)

    fixed_duration_result = run_candidate_fixed_duration_evaluation(candidate, pre_cutoff_candles, config, filters, journal)
    sensitivity_entry = score_candidate(fixed_duration_result.as_blocked_chronological_result())

    return CandidateSensitivityComparison(
        candidate_id=candidate.candidate_id,
        family=candidate.family,
        params=candidate.params,
        round_1_original_evaluation=original_entry,
        duration_normalized_sensitivity=sensitivity_entry,
        fragments=fixed_duration_result.fragments,
        leftovers=fixed_duration_result.leftovers,
    )
