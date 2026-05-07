"""Phase 4B Stage-11 IC memo orchestration entry point.

The synthesis sub-agent itself (`modal_workers/sub_agents/ic_memo.py`'s
`ICMemoRunner`) is a Sonnet-shaped runner that consumes a pre-built
`asset_context` and emits a JSON payload validated against
`ic_memo_v1.json`. This module is the orchestration layer ABOVE that
runner: given a `convergence_assessments.id`, it reconstructs the
`asset_context` from existing DB rows (asset metadata + the four
specialists' `sub_agent_calls.output` rows + Stage 9 thesis fields +
reference-class anchor), invokes the runner, persists the output as a
fifth `sub_agent_calls` row with `role='ic_memo'`, and returns the new
sub_agent_calls.id.

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
    `sub_agent_calls.role` CHECK to include 'ic_memo'.
"""

from __future__ import annotations

import logging
import time
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
        raise ICMemoOrchestrationError(
            f"assessment {assessment_id} has no schema-passing specialist "
            f"sub_agent_calls rows; refusing to synthesize IC memo with "
            f"zero inputs"
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
) -> str:
    """Insert one `sub_agent_calls` row with role='ic_memo'. Returns the
    new row id. The caller validated `result.schema_pass` already (the
    base `SubAgentRunner.run()` raises `SubAgentSchemaError` on failure
    before we get here)."""
    rows = sb._rest(
        "POST", "sub_agent_calls",
        json_body={
            "assessment_id": assessment_id,
            "role": IC_MEMO_ROLE,
            "query": question,
            "output": result.output,
            "schema_pass": result.schema_pass,
            "schema_retries": result.schema_retries,
            "tokens": result.tokens_input + result.tokens_output,
            "cost_usd": round(result.cost_usd, 4),
            "latency_ms": result.latency_ms,
        },
        prefer="return=representation",
    )
    if not rows:
        raise ICMemoOrchestrationError(
            f"sub_agent_calls insert returned no row for assessment "
            f"{assessment_id}"
        )
    return rows[0]["id"]


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
