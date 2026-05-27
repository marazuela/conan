"""Phase 4A — replay-runner: wraps `run_one()` with a cassette + dry-run.

Two public entry points:

  - `record_assessment(case_id, asset_id, *, cassette_dir, sb, a_client)`:
        Calls `run_one()` with a recording cassette wrapping the real
        OrchestratorClient. Stores a per-case JSONL at
        `cassette_dir/<case_id>.jsonl`. Stage 10 persists a real
        `convergence_assessments` row — recording is meant to be done
        deliberately against a known-good case and a known-good prompt.

  - `replay_assessment(case_id, *, cassette_dir, sb)`:
        Calls `run_one(dry_run=True)` with a replay cassette. Stage 10 is
        skipped; the parsed Stage 9 payload (`conviction_pct`,
        `thesis_direction`, `evidence_quality`, …) is captured via
        `parsed_out` and converted to a `replay.ReplayOutput`. Returns the
        ReplayOutput.

Plus `default_replay_fn` — adapts `replay_assessment` to the
`OrchestratorFn` shape that `eval_harness.replay.replay_one()` expects, so
the existing CLI flow

    python -m orchestrator_runtime.eval_harness.cli replay \\
        --orchestrator-fn orchestrator_runtime.eval_harness.replay_runner:default_replay_fn \\
        --version v3.0 --prompt-hash <sha>

works without touching the dispatcher signature.

Cassette directory contract (one per orchestrator_version + prompt_hash
combination, named after the harness case id):

    cassette_dir/
      <case_id_1>.jsonl     ← entries for asset_id from case 1
      <case_id_2>.jsonl
      ...

Mismatch handling: a `CassetteMismatchError` from the underlying cassette
is surfaced through `run_one()` and caught by `replay.replay_one`'s broad
exception handler, which records the failure as a per-case error row in
the harness output. That keeps the replay aggregate honest — the failed
case shows up in metrics with a documented reason rather than crashing
the entire backtest.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from orchestrator_runtime.client import OrchestratorClient
from orchestrator_runtime.eval_harness.cassette import CassetteClient
from orchestrator_runtime.eval_harness.replay import (
    OrchestratorFn,
    ReplayInput,
    ReplayOutput,
)

logger = logging.getLogger(__name__)


DEFAULT_CASSETTE_ENV_VAR = "ORCH_REPLAY_CASSETTE_DIR"


def _cassette_path(cassette_dir: Path, case_id: str) -> Path:
    return Path(cassette_dir) / f"{case_id}.jsonl"


def _band_from_conviction(conviction: float) -> str:
    """Mirror runtime.derive_band so the replay output's band matches what
    Stage 10 would have written. Imported lazily to avoid circular import."""
    from orchestrator_runtime.runtime import derive_band

    return derive_band(float(conviction))


def replay_assessment(
    case_id: str,
    asset_id: str,
    *,
    cassette_dir: Path,
    sb,
    trigger_type: str = "replay",
    model: Optional[str] = None,
) -> ReplayOutput:
    """Replay one case from a cassette. No live Anthropic calls. Stage 10
    is skipped via dry_run=True — sb is read-only for the duration."""
    from orchestrator_runtime.runtime import (
        DEFAULT_MODEL, run_one,
    )

    path = _cassette_path(cassette_dir, case_id)
    parsed_out: Dict[str, Any] = {}
    with CassetteClient(path, mode="replay") as cassette:
        run_one(
            sb, cassette, asset_id,
            trigger_type=trigger_type,
            model=model or DEFAULT_MODEL,
            dry_run=True,
            hard_kill_usd=None,
            parsed_out=parsed_out,
        )
    if not parsed_out:
        raise RuntimeError(
            f"replay produced no parsed payload for case {case_id} — "
            "the orchestrator returned before Stage 9."
        )
    conviction = float(parsed_out.get("conviction_pct") or 50.0)
    direction = str(parsed_out.get("thesis_direction") or "neutral")
    band = _band_from_conviction(conviction)
    summary = str(parsed_out.get("thesis_summary") or "")[:240]
    return ReplayOutput(
        conviction_pct=conviction,
        thesis_direction=direction,
        band=band,
        reasoning_summary=summary,
    )


def record_assessment(
    case_id: str,
    asset_id: str,
    *,
    cassette_dir: Path,
    sb,
    a_client: Optional[OrchestratorClient] = None,
    trigger_type: str = "replay_record",
    model: Optional[str] = None,
    persist: bool = False,
) -> ReplayOutput:
    """Record one case to a cassette. Calls run_one() with the live
    OrchestratorClient wrapped by a record-mode CassetteClient. Default
    `persist=False` runs with dry_run=True so the recording does NOT write
    a `convergence_assessments` row — flip to True only when you want
    Stage 10 side effects (rare, generally only during initial fixture
    creation against a controlled asset)."""
    from orchestrator_runtime.runtime import DEFAULT_MODEL, run_one

    upstream = a_client or OrchestratorClient()
    path = _cassette_path(cassette_dir, case_id)
    parsed_out: Dict[str, Any] = {}
    with CassetteClient(path, mode="record", upstream=upstream) as cassette:
        run_one(
            sb, cassette, asset_id,
            trigger_type=trigger_type,
            model=model or DEFAULT_MODEL,
            dry_run=not persist,
            hard_kill_usd=None,
            parsed_out=parsed_out,
        )
    if not parsed_out:
        raise RuntimeError(
            f"record produced no parsed payload for case {case_id}"
        )
    conviction = float(parsed_out.get("conviction_pct") or 50.0)
    direction = str(parsed_out.get("thesis_direction") or "neutral")
    band = _band_from_conviction(conviction)
    summary = str(parsed_out.get("thesis_summary") or "")[:240]
    logger.info(
        "recorded cassette %s — direction=%s conviction=%.1f band=%s",
        path, direction, conviction, band,
    )
    return ReplayOutput(
        conviction_pct=conviction,
        thesis_direction=direction,
        band=band,
        reasoning_summary=summary,
    )


def default_replay_fn(replay_input: ReplayInput) -> ReplayOutput:
    """OrchestratorFn-shaped entry point for the existing replay CLI.

    Reads the cassette directory from `ORCH_REPLAY_CASSETTE_DIR` env (set
    by the caller before invoking the CLI), constructs a SupabaseClient,
    and dispatches to replay_assessment. Errors propagate — replay.replay_one
    catches them and emits a per-case error row.
    """
    cassette_dir = os.environ.get(DEFAULT_CASSETTE_ENV_VAR)
    if not cassette_dir:
        raise RuntimeError(
            f"{DEFAULT_CASSETTE_ENV_VAR} is not set — "
            "default_replay_fn cannot locate the cassette directory."
        )
    from modal_workers.shared.supabase_client import SupabaseClient

    sb = SupabaseClient()
    return replay_assessment(
        case_id=replay_input.case.id,
        asset_id=replay_input.case.asset_id,
        cassette_dir=Path(cassette_dir),
        sb=sb,
    )


def make_replay_fn(
    cassette_dir: Path, *, sb=None,
) -> OrchestratorFn:
    """Build an OrchestratorFn closure with a fixed cassette_dir + sb.

    Useful in tests where the caller doesn't want to pollute env vars and
    needs to inject a stub SupabaseClient.
    """
    def _fn(replay_input: ReplayInput) -> ReplayOutput:
        from modal_workers.shared.supabase_client import SupabaseClient

        actual_sb = sb if sb is not None else SupabaseClient()
        return replay_assessment(
            case_id=replay_input.case.id,
            asset_id=replay_input.case.asset_id,
            cassette_dir=cassette_dir,
            sb=actual_sb,
        )
    return _fn
