"""Replay one HarnessCase through the orchestrator.

A replay reconstructs the document buffer as it existed on the case's
reference_assessment_date (using the snapshotted `document_set` uuids), then
runs the orchestrator pipeline against that buffer at a specific
`orchestrator_version` / `prompt_hash` combination.

Phase 0 STUB: this skeleton defines the replay contract but doesn't actually
invoke an orchestrator (that's Phase 2 work). Tests can call replay_one() with
a stub orchestrator function to validate the harness wiring before the real
runtime exists.

Phase 2 wiring: import orchestrator_runtime.runtime.run_assessment_for_replay
and pass it as the `orchestrator_fn` argument.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from orchestrator_runtime.eval_harness.gold_standard import (
    HarnessCase,
    is_direction_correct,
)

logger = logging.getLogger(__name__)


@dataclass
class ReplayInput:
    """Inputs an orchestrator function gets when called from a replay."""
    case: HarnessCase
    document_ids: List[str]
    reference_date: str                  # ISO date — orchestrator pretends "now" is this
    orchestrator_version: str
    prompt_hash: str


@dataclass
class ReplayOutput:
    """Minimum the orchestrator must return for replay aggregation."""
    conviction_pct: float                # 0–100
    thesis_direction: str                # 'long','short','neutral','straddle'
    band: str                            # 'immediate','watchlist','archive','discard'
    reasoning_summary: str               # short audit string


# Type alias for the orchestrator entry point.
# Real Phase 2 implementation will wrap a Modal function call to the API SDK
# orchestrator. For Phase 0 testing, callers pass a stub.
OrchestratorFn = Callable[[ReplayInput], ReplayOutput]


def replay_one(
    case: HarnessCase,
    orchestrator_version: str,
    prompt_hash: str,
    orchestrator_fn: OrchestratorFn,
) -> Dict[str, Any]:
    """Replay one HarnessCase. Returns a per-assessment-result dict suitable
    for orchestrator_runtime.eval_harness.metrics.aggregate()."""
    inp = ReplayInput(
        case=case,
        document_ids=case.document_set,
        reference_date=case.reference_assessment_date.isoformat(),
        orchestrator_version=orchestrator_version,
        prompt_hash=prompt_hash,
    )
    try:
        out = orchestrator_fn(inp)
    except Exception as exc:  # noqa: BLE001 — broad on purpose; replay never aborts
        logger.exception("replay failed for case %s: %s", case.id, exc)
        return {
            "case_id": case.id,
            "asset_id": case.asset_id,
            "conviction_pct": 50.0,      # neutral default
            "thesis_direction": "neutral",
            "band": "discard",
            "direction_correct": 0,
            "error": str(exc),
        }

    direction_correct = 1 if is_direction_correct(out.thesis_direction, case) else 0

    return {
        "case_id": case.id,
        "asset_id": case.asset_id,
        "conviction_pct": float(out.conviction_pct),
        "thesis_direction": out.thesis_direction,
        "band": out.band,
        "reasoning_summary": out.reasoning_summary,
        "direction_correct": direction_correct,
        "realized_outcome": case.realized_outcome,
    }


def replay_all(
    cases: List[HarnessCase],
    orchestrator_version: str,
    prompt_hash: str,
    orchestrator_fn: OrchestratorFn,
) -> List[Dict[str, Any]]:
    """Replay every case sequentially. Phase 2+ may parallelize via Batch API
    or Modal fan-out; Phase 0 keeps it serial for clarity + reproducibility."""
    results = []
    for case in cases:
        logger.info(
            "replay: case=%s asset=%s ref_date=%s",
            case.id, case.asset_id, case.reference_assessment_date,
        )
        results.append(replay_one(case, orchestrator_version, prompt_hash, orchestrator_fn))
    return results


def stub_orchestrator(_inp: ReplayInput) -> ReplayOutput:
    """Stub orchestrator for harness self-test. Always emits neutral 50% to
    confirm the replay → metrics pipeline is wired correctly. Replace with
    the real orchestrator in Phase 2."""
    return ReplayOutput(
        conviction_pct=50.0,
        thesis_direction="neutral",
        band="archive",
        reasoning_summary="stub-orchestrator: harness wiring self-test",
    )
