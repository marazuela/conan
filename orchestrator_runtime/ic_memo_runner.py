"""Phase 4B Stage-11 IC memo orchestration entry point.

The synthesis sub-agent itself (`modal_workers/sub_agents/ic_memo.py`'s
`ICMemoRunner`) is a Sonnet-shaped runner that consumes a pre-built
`asset_context` and emits a JSON payload validated against
`ic_memo_v1.json`. This module is the orchestration layer ABOVE that
runner: given a `convergence_assessments.id`, it reconstructs the
`asset_context` from existing DB rows (asset metadata + the four
specialists' `sub_agent_calls.output` rows OR the Phase-0 3-role
`fda_agent_reviews` rows via the read-side bridge + Stage 9 thesis
fields + reference-class anchor), invokes the runner, persists the
output as an `fda_agent_reviews` row keyed by the asset's pending
`fda_regulatory_events.id` with `agent_kind='ic_memo'`, and returns
the new `fda_agent_reviews.id`. Event-scoped persistence (rather than
the older assessment-scoped `sub_agent_calls` row) is what lets
`fda_signal_promote_to_thesis()` consume the memo from the dashboard.

This is invoked **on demand** (not part of `runtime.run_one()`):
  - Operator clicks "Generate IC memo" on a watchlist or immediate-band
    assessment in the dashboard.
  - Cron job that runs IC memos nightly on freshly-immediate assessments
    (optional; off by default).

See ic_memo_polish.md (operator-facing prose-polish layer) — that's a
DIFFERENT skill that refines a convergence_assessment's existing
prose. This module is for the synthesis flavor (4 specialist outputs →
new ic_memo_v1.json memo).

Migrations:
  - 20260513000010_v3_phase_4b_sub_agent_calls_ic_memo_role.sql widens
    `sub_agent_calls.role` CHECK to include 'ic_memo' (legacy; pre-PR #60).
  - `fda_agent_reviews.agent_kind` CHECK already accepts 'ic_memo'; no
    migration needed for the PR #60 write-side switch.

Related history:
  - PR #58 (`8887773`, 2026-05-13) — read-side bridge so the loader
    falls back to `fda_agent_reviews` Phase-0 3-role rows when
    `sub_agent_calls` is empty.
  - PR #60 (`84efa65`, 2026-05-13) — write-side switch: persist to
    `fda_agent_reviews` event-scoped, not `sub_agent_calls`.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from modal_workers.shared.supabase_client import SupabaseClient
from modal_workers.sub_agents.ic_memo import ICMemoRunner
from modal_workers.sub_agents.runtime import (
    SubAgentResult,
    SubAgentSchemaError,
)
from orchestrator_runtime.client import OrchestratorClient

logger = logging.getLogger(__name__)

IC_MEMO_ROLE = "ic_memo"

# Default question text — the runner can be invoked with a custom prompt
# (e.g. an operator override), but a sensible default lets the dashboard's
# one-click "Generate IC memo" path fire without prompt engineering.
DEFAULT_IC_MEMO_QUESTION = (
    "Synthesize the case into an IC-ready memo. Cover thesis direction "
    "and headline, asymmetry vs options-implied move, kill_conditions, "
    "position-sizing logic, and a concise summary. Cite specialist "
    "sources via the citations[] array."
)


class ICMemoOrchestrationError(RuntimeError):
    """Raised when the orchestration cannot proceed (missing assessment,
    missing all four specialists, etc.). Distinct from
    SubAgentSchemaError, which means the LLM produced bad JSON."""


# Phase-0 (fda_agent_reviews.agent_kind, 3-role) → Phase-4B
# (sub_agent_calls.role, 4-role) bridge. 1:1 and conservative; competitive has
# no Phase-0 equivalent and is left to build_user_content's placeholder.
PHASE0_TO_PHASE4B_ROLES = {
    "medical": "literature",
    "regulatory": "regulatory_history",
    "microstructure": "options_microstructure",
}


def _load_phase0_specialists(
    sb: SupabaseClient,
    asset_id: str,
) -> Dict[str, Dict[str, Any]]:
    """Fallback specialist loader: walk asset_id → fda_regulatory_events →
    fda_agent_reviews and return the newest completed review per Phase-0 role,
    remapped to the 4-role taxonomy. Returns {} (not raises) when the asset has
    no events or no completed reviews — the caller raises after both lookups
    fail."""
    events = sb._rest(
        "GET", "fda_regulatory_events",
        params={
            "select": "id",
            "asset_id": f"eq.{asset_id}",
            "order": "created_at.desc",
        },
    ) or []
    if not events:
        return {}
    event_ids = [e["id"] for e in events]

    reviews = sb._rest(
        "GET", "fda_agent_reviews",
        params={
            "select": "agent_kind,structured_output,status,ran_at",
            "event_id": f"in.({','.join(event_ids)})",
            "status": "eq.completed",
            "agent_kind": f"in.({','.join(PHASE0_TO_PHASE4B_ROLES.keys())})",
            "order": "ran_at.desc.nullslast",
        },
    ) or []

    out: Dict[str, Dict[str, Any]] = {}
    for r in reviews:
        old_role = r.get("agent_kind")
        new_role = PHASE0_TO_PHASE4B_ROLES.get(old_role)
        if not new_role or new_role in out:
            continue
        payload = r.get("structured_output")
        if isinstance(payload, dict) and payload:
            out[new_role] = payload
    return out


def load_ic_memo_context(
    sb: SupabaseClient,
    assessment_id: str,
) -> Dict[str, Any]:
    """Reconstruct the `asset_context` an `ICMemoRunner` consumes from
    DB rows associated with `assessment_id`.

    Shape (matches `test_ic_memo_runner.py::test_ic_memo_build_user_content_*`):
      {
        "asset": {"ticker": ..., "drug_name": ..., "indication": ...},
        "specialists": {
            "literature": <output dict>,
            "competitive": <output dict>,
            "regulatory_history": <output dict>,
            "options_microstructure": <output dict>,
        },
        "thesis": {
            "direction": str | None,
            "conviction_pct": float | None,
            "text": str,            # reasoning_summary or thesis_summary
        },
        "reference_class_anchor": {
            "reference_class": str | None,
            "base_rate_pct": float | None,
            "n_cases": int | None,
            "similar_resolved_case_ids": [...],
        } | None,
      }

    Missing specialists are absent from the dict (NOT inserted as null);
    `ICMemoRunner.build_user_content` already renders a "(no review
    available...)" placeholder for any specialist not present in the dict.
    """
    rows = sb._rest(
        "GET", "convergence_assessments",
        params={
            "select": (
                "id,asset_id,thesis_direction,conviction_pct,"
                "thesis_summary,reasoning_trace,reference_class,"
                "reference_class_base_rate,similar_resolved_case_ids"
            ),
            "id": f"eq.{assessment_id}",
        },
    ) or []
    if not rows:
        raise ICMemoOrchestrationError(
            f"convergence_assessments row {assessment_id} not found"
        )
    a = rows[0]
    asset_id = a["asset_id"]

    asset_rows = sb._rest(
        "GET", "fda_assets",
        params={
            "select": (
                "id,ticker,drug_name,generic_name,sponsor_name,"
                "indication,indication_normalized,application_number,"
                "application_type,program_status"
            ),
            "id": f"eq.{asset_id}",
        },
    ) or []
    asset = asset_rows[0] if asset_rows else {"id": asset_id}

    specialist_rows = sb._rest(
        "GET", "sub_agent_calls",
        params={
            "select": "role,output,schema_pass,created_at",
            "assessment_id": f"eq.{assessment_id}",
            "role": (
                "in.(literature,competitive,regulatory_history,"
                "options_microstructure)"
            ),
            "schema_pass": "is.true",
            "order": "created_at.desc",
        },
    ) or []

    # Latest schema-passing row per role wins (newer entries supersede
    # older ones — supports re-runs of a single specialist without
    # invalidating the others).
    specialists: Dict[str, Dict[str, Any]] = {}
    for r in specialist_rows:
        role = r.get("role")
        if not role or role in specialists:
            continue
        out = r.get("output")
        if isinstance(out, dict) and out:
            specialists[role] = out

    if not specialists:
        # Fallback: production writes specialist reviews to fda_agent_reviews
        # with the 3-role taxonomy (medical/regulatory/microstructure). When
        # sub_agent_calls is empty for this assessment (older runs, or Stage-1
        # dispatch that left assessment_id NULL), bridge to those reviews so
        # synthesis still works.
        specialists = _load_phase0_specialists(sb, asset_id)

    if not specialists:
        raise ICMemoOrchestrationError(
            f"assessment {assessment_id} has no schema-passing specialist "
            f"rows in sub_agent_calls (4-role) or fda_agent_reviews (Phase 0 "
            f"3-role); refusing to synthesize IC memo with zero inputs"
        )

    thesis: Dict[str, Any] = {
        "direction": a.get("thesis_direction"),
        "conviction_pct": (
            float(a["conviction_pct"])
            if a.get("conviction_pct") is not None else None
        ),
        "text": a.get("thesis_summary") or a.get("reasoning_trace") or "",
    }

    anchor: Optional[Dict[str, Any]] = None
    if a.get("reference_class") or a.get("reference_class_base_rate") is not None:
        base_rate = a.get("reference_class_base_rate")
        anchor = {
            "reference_class": a.get("reference_class"),
            "base_rate_pct": (
                float(base_rate) * 100.0
                if base_rate is not None else None
            ),
            "similar_resolved_case_ids": (
                a.get("similar_resolved_case_ids") or []
            ),
        }

    return {
        "assessment_id": assessment_id,
        "asset_id": asset_id,
        "asset": asset,
        "specialists": specialists,
        "thesis": thesis,
        "reference_class_anchor": anchor,
    }


def persist_ic_memo_result(
    sb: SupabaseClient,
    assessment_id: str,
    question: str,
    result: SubAgentResult,
) -> Optional[str]:
    """Persist the synthesized IC memo where promotion can read it.

    `fda_signal_promote_to_thesis()` is event-scoped and reads
    `fda_agent_reviews.agent_kind='ic_memo'`, not `sub_agent_calls`. Resolve the
    assessment's current pending event and write the memo there. If no pending
    event exists, skip persist honestly so the assessment can still complete
    without creating an orphaned memo.
    """
    event_id = _resolve_event_id_for_assessment(sb, assessment_id)
    if event_id is None:
        logger.warning(
            "ic_memo: assessment %s has no pending fda_regulatory_events row; "
            "skipping fda_agent_reviews persist",
            assessment_id,
        )
        return None

    rows = sb._rest(
        "POST", "fda_agent_reviews",
        json_body={
            "event_id": event_id,
            "agent_kind": IC_MEMO_ROLE,
            "version": "ic_memo_runner_v1",
            "status": "completed",
            "structured_output": {
                **(result.output if isinstance(result.output, dict) else {}),
                "_orchestrator_meta": {
                    "assessment_id": assessment_id,
                    "question": question,
                    "schema_pass": result.schema_pass,
                    "schema_retries": result.schema_retries,
                    "tokens": result.tokens_input + result.tokens_output,
                    "cost_usd": round(result.cost_usd, 4),
                    "latency_ms": result.latency_ms,
                },
            },
            "citations": result.output.get("citations", []),
            "confidence": result.output.get("confidence"),
            "snapshot_hash": f"assessment:{assessment_id}",
            "ran_at": datetime.now(timezone.utc).isoformat(),
        },
        prefer="return=representation",
    )
    if not rows:
        raise ICMemoOrchestrationError(
            f"fda_agent_reviews insert returned no row for assessment "
            f"{assessment_id}"
        )
    return rows[0]["id"]


def _resolve_event_id_for_assessment(
    sb: SupabaseClient,
    assessment_id: str,
) -> Optional[str]:
    rows = sb._rest(
        "GET",
        "convergence_assessments",
        params={"select": "asset_id", "id": f"eq.{assessment_id}", "limit": "1"},
    ) or []
    if not rows:
        raise ICMemoOrchestrationError(
            f"convergence_assessments row {assessment_id} not found"
        )
    asset_id = rows[0].get("asset_id")
    if not asset_id:
        return None
    events = sb._rest(
        "GET",
        "fda_regulatory_events",
        params={
            "select": "id",
            "asset_id": f"eq.{asset_id}",
            "event_status": "eq.pending",
            "order": "created_at.desc",
            "limit": "1",
        },
    ) or []
    return events[0].get("id") if events else None


def run_ic_memo(
    sb: SupabaseClient,
    assessment_id: str,
    *,
    question: Optional[str] = None,
    a_client: Optional[OrchestratorClient] = None,
    runner: Optional[ICMemoRunner] = None,
    persist: bool = True,
) -> Dict[str, Any]:
    """End-to-end IC memo synthesis for one assessment.

    Args:
      sb: SupabaseClient.
      assessment_id: convergence_assessments.id.
      question: optional override of the default synthesis prompt.
      a_client: optional OrchestratorClient (for budget pinning by the
        caller — e.g. a Modal endpoint that wants to enforce a per-run
        cost cap). Defaults to a fresh client.
      runner: optional ICMemoRunner (test injection point — production
        callers should pass None and let this build one).
      persist: when False, skip the sub_agent_calls insert (used for
        dry-run smoke checks). Defaults to True.

    Returns:
      {
        "sub_agent_call_id": str | None,   # None when persist=False
        "assessment_id": str,
        "output": dict,                    # ic_memo_v1.json payload
        "tokens_input": int,
        "tokens_output": int,
        "cost_usd": float,
        "latency_ms": int,
      }

    Raises:
      ICMemoOrchestrationError: assessment missing or zero specialists.
      SubAgentSchemaError: LLM produced output that failed
        ic_memo_v1.json validation.
    """
    t0 = time.time()
    ctx = load_ic_memo_context(sb, assessment_id)

    runner = runner or ICMemoRunner(client=a_client or OrchestratorClient())
    final_question = question or DEFAULT_IC_MEMO_QUESTION

    logger.info(
        "ic_memo: assessment=%s asset=%s specialists=%s",
        assessment_id, ctx.get("asset_id"),
        sorted(ctx.get("specialists", {}).keys()),
    )

    result = runner.run(
        question=final_question,
        asset_context=ctx,
    )

    sub_agent_call_id: Optional[str] = None
    if persist:
        sub_agent_call_id = persist_ic_memo_result(
            sb, assessment_id, final_question, result,
        )

    return {
        "sub_agent_call_id": sub_agent_call_id,
        "assessment_id": assessment_id,
        "output": result.output,
        "tokens_input": result.tokens_input,
        "tokens_output": result.tokens_output,
        "cost_usd": result.cost_usd,
        "latency_ms": result.latency_ms,
        "wall_seconds": int(time.time() - t0),
    }
