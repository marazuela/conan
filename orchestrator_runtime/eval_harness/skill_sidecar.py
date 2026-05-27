"""Eval adapter for the single-shot FDA assessment skill sidecar.

The skill writes one JSON file per eval case. This adapter converts those
artifacts into the same ReplayOutput shape as the live orchestrator so Brier,
AUC, and direction-accuracy comparisons use one metrics path.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from orchestrator_runtime.eval_harness.replay import ReplayInput, ReplayOutput
from orchestrator_runtime.runtime import derive_band


class SkillSidecarMissingOutput(RuntimeError):
    """Raised when an eval case has no sidecar JSON artifact."""


def _load_index(output_dir: Path) -> Dict[str, Path]:
    index_path = output_dir / "_index.jsonl"
    if not index_path.exists():
        return {}
    mapping: Dict[str, Path] = {}
    for line in index_path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        case_id = row.get("eval_case_id")
        output_json = row.get("output_json")
        if case_id and output_json:
            mapping[str(case_id)] = Path(output_json)
    return mapping


def load_sidecar_payload(
    output_dir: Path,
    case_id: str,
) -> Dict[str, Any]:
    """Load a skill output for an eval case.

    Resolution order:
      1. `_index.jsonl` row with `eval_case_id` and `output_json`.
      2. `<case_id>.json` directly in `output_dir`.
    """
    output_dir = Path(output_dir)
    indexed = _load_index(output_dir).get(case_id)
    candidates = []
    if indexed is not None:
        candidates.append(indexed if indexed.is_absolute() else output_dir / indexed)
    candidates.append(output_dir / f"{case_id}.json")

    for path in candidates:
        if path.exists():
            return json.loads(path.read_text())
    raise SkillSidecarMissingOutput(
        f"no skill sidecar output found for eval case {case_id} in {output_dir}"
    )


def sidecar_output_to_replay(payload: Dict[str, Any]) -> ReplayOutput:
    conviction = float(payload.get("conviction_pct") or payload.get("p_mid", 0.5) * 100)
    direction = str(payload.get("thesis_direction") or "neutral")
    return ReplayOutput(
        conviction_pct=conviction,
        thesis_direction=direction,
        band=derive_band(conviction),
        reasoning_summary=str(payload.get("thesis_summary") or "")[:240],
    )


def make_skill_sidecar_fn(output_dir: Path):
    """Return an OrchestratorFn compatible with replay.replay_all()."""
    def _fn(inp: ReplayInput) -> ReplayOutput:
        payload = load_sidecar_payload(Path(output_dir), inp.case.id)
        return sidecar_output_to_replay(payload)

    return _fn
