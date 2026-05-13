"""Phase 4B Stage-11 IC memo orchestration entry point.

The synthesis sub-agent itself (`modal_workers/sub_agents/ic_memo.py`'s
`ICMemoRunner`) is a Sonnet-shaped runner that consumes a pre-built
`asset_context` and emits a JSON payload validated against
`ic_memo_v1.json`. This module is the orchestration layer ABOVE that
runner: given a `convergence_assessments.id`, it reconstructs the
`asset_context` from existing DB rows (asset metadata + the four
specialists' reviews + Stage 9 thesis fields + reference-class anchor),
invokes the runner, persists the output as an `fda_agent_reviews` row
with `agent_kind='ic_memo'`, and returns the new fda_agent_reviews.id.

This is invoked **on demand** (not part of `runtime.run_one()`):
  - Operator clicks "Generate IC memo" on a watchlist or immediate-band
    assessment in the dashboard.
  - Cron job that runs IC memos nightly on freshly-immediate assessments
    (optional; off by default).

See ic_memo_polish.md (operator-facing prose-polish layer) — that's a
DIFFERENT skill that refines a convergence_assessment's existing
prose. This module is for the synthesis flavor (4 specialist outputs →
new ic_memo_v1.json memo).

Persistence target: `fda_agent_reviews` (keyed by `event_id`, NOT
`sub_agent_calls` keyed by `assessment_id`). Per `ic_memo_v1.json`
schema header AND the `fda_signal_promote_to_thesis()` RPC at
`20260511000000_v3_fda_signal_promote_to_thesis.sql:54-63`, the IC memo
must be event-scoped so the operator's "Promote to thesis" path can
find it. See audit/ic_memo_specialist_pipeline_drift.md §F-IC4.

Migrations (relevant CHECK widenings):
  - 20260510000010_v3_stream6_safety_and_cleanup.sql:114-117 — widens
    `fda_agent_reviews.agent_kind` to accept 'ic_memo'.
  - 20260513000010_v3_phase_4b_sub_agent_calls_ic_memo_role.sql — same
    widening on `sub_agent_calls.role` (legacy; orchestrator no longer
    writes there).
"""

from __future__ import annotations

import hashlib
import json
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

# IC memo persistence target. Per ic_memo_v1.json schema header and the
# fda_signal_promote_to_thesis() RPC (20260511000000_v3_fda_signal_promote_to_thesis.sql:54-63),
# the canonical store is `fda_agent_reviews` keyed by event_id, NOT
# sub_agent_calls keyed by assessment_id. See audit doc
# audit/ic_memo_specialist_pipeline_drift.md §F-IC4 for the full discussion.
IC_MEMO_VERSION = "1"  # matches ic_memo_v1.json "schema_version" const + existing Phase 0 rows

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

# Phase 0 (`fda_agent_reviews.agent_kind`) → Phase 4B (`sub_agent_calls.role`) bridge.
# Used by load_ic_memo_context when sub_agent_calls is empty for the assessment
# but the asset has completed Phase 0 reviews on fda_agent_reviews. Keeping the
# IC memo unblocked while the orchestrator's sub-agent dispatch is still gated
# on Phase 2C (ORCH_ENABLE_SUB_AGENTS_DEFAULT=0).
#
# Mapping is intentionally 1:1 and conservative:
#   - medical        → literature             (Phase 0 medical covers efficacy/
#                                              safety/mechanism/AE; closest in
#                                              spirit to the new literature
#                                              specialist)
#   - regulatory     → regulatory_history     (1:1 semantic)
#   - microstructure → options_microstructure (1:1 semantic)
#   - competitive    → (no Phase 0 equivalent — left empty; build_user_content
#                       renders a "no review available" placeholder)
PHASE0_TO_PHASE4B_ROLES = {
    "medical": "literature",
    "regulatory": "regulatory_history",
    "microstructure": "options_microstructure",
}


class ICMemoOrchestrationError(RuntimeError):
    """Raised when the orchestration cannot proceed (missing assessment,
    missing all four specialists, etc.). Distinct from
    SubAgentSchemaError, which means the LLM produced bad JSON."""


def _load_phase0_specialists(
    sb: SupabaseClient,
    asset_id: str,
) -> Dict[str, Dict[str, Any]]:
    """Phase 0 fallback specialist loader.

    Walks `convergence_assessments.asset_id → fda_regulatory_events.asset_id →
    fda_regulatory_events.id → fda_agent_reviews.event_id` and returns the
    latest schema-passing review per Phase 0 role, remapped to the new 4-role
    taxonomy via PHASE0_TO_PHASE4B_ROLES.

    Returns an empty dict (NOT raises) when the asset has no fda_regulatory_events
    rows OR no completed reviews — the caller upgrades that to ICMemoOrchestrationError
    after both lookups have failed.

    Multiple events per asset is the common case (one per catalyst). We take the
    union of completed reviews across all of the asset's events, keeping the
    newest review per role. Older catalysts' reviews are still informative for
    IC synthesis — the build_user_content layer doesn't know or care which
    event a payload originated from.
    """
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

    # Newest-per-role wins (same dedup semantics as the sub_agent_calls path).
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
        # Phase 0 fallback: production (2026-05-13) still writes specialist
        # reviews into fda_agent_reviews with the 3-role taxonomy (medical/
        # regulatory/microstructure). The orchestrator's Stage 1 dispatch path
        # (which populates sub_agent_calls with the 4-role taxonomy) is gated
        # OFF behind ORCH_ENABLE_SUB_AGENTS_DEFAULT until Phase 2C. Bridge the
        # gap so IC memo synthesis works on whichever specialists are live —
        # without this, every operator "Generate IC memo" click fails.
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


def _resolve_event_id_for_ic_memo(
    sb: SupabaseClient,
    assessment_id: str,
) -> Optional[str]:
    """Resolve the fda_regulatory_events.id this IC memo should be keyed by.

    `convergence_assessments` carries `asset_id` but not `event_id` (F-IC5 in
    audit/ic_memo_specialist_pipeline_drift.md). The IC memo needs an event_id
    for `fda_agent_reviews` (NOT NULL) and for `fda_signal_promote_to_thesis()`
    to find it (the RPC verifies `v_review.event_id = p_event_id`).

    Strategy: pick the asset's most-recently-created PENDING event. Pending
    is the right scope because IC memos drive the operator's "Promote to
    thesis" decision — promoting against a resolved event is meaningless.

    Returns None when the asset has no pending events. Callers should warn
    + skip persist rather than raise, so the rest of the assessment pipeline
    (convergence_assessments writeback, stage metrics) can still complete.
    """
    a_rows = sb._rest(
        "GET", "convergence_assessments",
        params={"select": "asset_id", "id": f"eq.{assessment_id}"},
    ) or []
    if not a_rows:
        return None
    asset_id = a_rows[0].get("asset_id")
    if not asset_id:
        return None

    events = sb._rest(
        "GET", "fda_regulatory_events",
        params={
            "select": "id",
            "asset_id": f"eq.{asset_id}",
            "event_status": "eq.pending",
            "order": "created_at.desc",
            "limit": "1",
        },
    ) or []
    return events[0]["id"] if events else None


def _compute_ic_memo_snapshot_hash(
    event_id: str,
    assessment_id: str,
    output: Dict[str, Any],
) -> str:
    """Stable SHA-256 hash of the synthesis inputs. Matches the 64-char hex
    shape of existing fda_agent_reviews.snapshot_hash rows (Phase 0 specialists
    use the same width). Content-deterministic so re-running synthesis on
    unchanged inputs produces the same hash — operators / dedup logic can
    detect that downstream."""
    payload = json.dumps(
        {"event": event_id, "assessment": assessment_id, "output": output},
        sort_keys=True, default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def persist_ic_memo_result(
    sb: SupabaseClient,
    assessment_id: str,
    question: str,
    result: SubAgentResult,
) -> Optional[str]:
    """Insert one `fda_agent_reviews` row with agent_kind='ic_memo'. Returns
    the new row id, or None when the asset has no pending event to key the
    memo to (honest-empty — the surrounding assessment still completes).

    Was previously writing to `sub_agent_calls.role='ic_memo'`, but no
    downstream consumer reads from there — the dashboard panel and the
    `fda_signal_promote_to_thesis()` RPC both read `fda_agent_reviews`. See
    audit doc §F-IC4 for the full divergence story.

    The caller already validated `result.schema_pass` (`SubAgentRunner.run()`
    raises `SubAgentSchemaError` on failure before we get here)."""
    event_id = _resolve_event_id_for_ic_memo(sb, assessment_id)
    if event_id is None:
        logger.warning(
            "assessment %s has no pending fda_regulatory_events row; "
            "skipping ic_memo persist (memo synthesized but not stored — "
            "operator workflow can't promote without an event anchor)",
            assessment_id,
        )
        return None

    output = result.output or {}
    citations = output.get("citations")
    if not isinstance(citations, list):
        citations = []

    confidence = output.get("confidence")
    try:
        confidence = float(confidence) if confidence is not None else None
    except (TypeError, ValueError):
        confidence = None
    if confidence is not None:
        confidence = max(0.0, min(1.0, confidence))

    snapshot_hash = _compute_ic_memo_snapshot_hash(event_id, assessment_id, output)

    rows = sb._rest(
        "POST", "fda_agent_reviews",
        json_body={
            "event_id": event_id,
            "agent_kind": IC_MEMO_ROLE,
            "version": IC_MEMO_VERSION,
            "structured_output": output,
            "citations": citations,
            "confidence": confidence,
            "snapshot_hash": snapshot_hash,
            "status": "completed",
            "ran_at": datetime.now(timezone.utc).isoformat(),
        },
        prefer="return=representation",
    )
    if not rows:
        raise ICMemoOrchestrationError(
            f"fda_agent_reviews insert returned no row for assessment "
            f"{assessment_id} (event_id={event_id})"
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
      persist: when False, skip the fda_agent_reviews insert (used for
        dry-run smoke checks). Defaults to True.

    Returns:
      {
        "sub_agent_call_id": str | None,   # fda_agent_reviews.id (key name
                                            # kept for backwards compat —
                                            # value is now a Phase 0 review
                                            # row id, NOT sub_agent_calls.id).
                                            # None when persist=False OR
                                            # when the asset has no pending
                                            # fda_regulatory_events row.
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
