"""Modal app for the v3 orchestrator runtime.

Separate from the v2 `conan-v2` Modal app so v3 can be deployed + iterated
without touching v2 scanner / compute-endpoint registrations. Deploy with:

  modal deploy modal_workers/orchestrator_app.py

Functions:
  asset_linker_run         — run extractor.asset_linker over unlinked docs
                             for one asset (or all is_active assets)
  fact_extractor_run       — run extractor.sonnet_fact_extractor over
                             unextracted material links
  orchestrator_run_one     — produce one convergence_assessment for an asset
                             (single-shot, ensemble streaming, or ensemble
                             batch, with optional Stage 7 constitutional)
  orchestrator_drain_queue — pull pending orchestrator_runs and dispatch
                             (scheduled cron @ every 5 min)

Secrets required (set via `modal secret create ...`):
  - anthropic-orchestrator    ANTHROPIC_API_KEY=<rotated key>
  - supabase-secrets          SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
  - scanner-secrets           SEC_USER_AGENT (for ingestion adapters)

Endpoints:
  - operator_refresh_endpoint  POST /operator-refresh — dashboard "Refresh"
                               button creates an orchestrator_runs row
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

import modal

# Distinct Modal app name. Coexists with conan-v2 in the same Modal workspace.
app = modal.App("conan-v3-orchestrator")

# v3-specific image — adds anthropic SDK + RAG providers to the v2 base list.
# RAG SDKs (voyageai, openai, cohere) are installed but their API keys are
# only required when RAG_PROVIDER=voyage or openai_cohere; absent keys
# surface as warnings, not import failures.
image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install(
        "fastapi[standard]",
        "pydantic>=2",
        "requests>=2.31",
        "anthropic>=0.50",
        "voyageai>=0.3",
        "openai>=1.50",
        "cohere>=5.10",
        "mcp[cli]>=1.20",
    )
    .add_local_python_source("modal_workers", "orchestrator_runtime")
)

# Secrets
anthropic_secrets = modal.Secret.from_name("anthropic-orchestrator")
supabase_secrets = modal.Secret.from_name("supabase-secrets")
scanner_secrets = modal.Secret.from_name("scanner-secrets")
# RAG provider keys (VOYAGE_API_KEY, OPENAI_API_KEY, COHERE_API_KEY,
# RAG_PROVIDER). Wired into orchestrator_run_one so Stage 1 RAG retrieval
# (Phase 2B, ORCH_ENABLE_STAGE_1_RAG=1) can dispatch embed queries.
rag_secrets = modal.Secret.from_name("rag-providers")


# ============================================================================
# Asset linker — Sonnet two-pass classifier over documents
# ============================================================================

@app.function(
    image=image,
    timeout=3600,
    secrets=[anthropic_secrets, supabase_secrets],
)
def asset_linker_run(
    asset_id: Optional[str] = None,
    max_docs: int = 200,
    budget_usd: float = 15.0,
) -> Dict[str, Any]:
    """Classify unlinked documents into asset_documents. Use --asset-id to
    restrict to one asset or omit for all is_active=true."""
    from modal_workers.extractor.asset_linker import main as linker_main

    argv = ["--max", str(max_docs), "--budget-usd", str(budget_usd)]
    if asset_id:
        argv.extend(["--asset-id", asset_id])
    rc = linker_main(argv)
    return {"return_code": rc}


# ============================================================================
# Asset linker pass-2 — Haiku verifier on low-confidence pass-1 links
# ============================================================================

@app.function(
    image=image,
    timeout=3600,
    secrets=[anthropic_secrets, supabase_secrets],
)
def asset_linker_pass2_run(
    asset_id: Optional[str] = None,
    max_links: int = 200,
    threshold: float = 0.80,
    budget_usd: float = 2.0,
) -> Dict[str, Any]:
    """Verify low-confidence pass-1 links with Haiku 4.5. Updates
    asset_documents.{verified_by_pass2, pass2_verdict, pass2_confidence,
    pass2_at}; rejected verdicts also flip is_material=false (no DELETE).
    Idempotent — skips rows already verified."""
    from modal_workers.extractor.asset_linker import pass2_main

    argv = [
        "--max-links", str(max_links),
        "--threshold", str(threshold),
        "--budget-usd", str(budget_usd),
    ]
    if asset_id:
        argv.extend(["--asset-id", asset_id])
    rc = pass2_main(argv)
    return {"return_code": rc}


# ============================================================================
# Fact extractor — Sonnet structured fact extraction
# ============================================================================

@app.function(
    image=image,
    timeout=3600,
    secrets=[anthropic_secrets, supabase_secrets],
)
def fact_extractor_run(
    asset_id: Optional[str] = None,
    max_links: int = 200,
    budget_usd: float = 30.0,
) -> Dict[str, Any]:
    """Extract structured facts from material asset_documents links."""
    from modal_workers.extractor.sonnet_fact_extractor import main as extractor_main

    argv = ["--max", str(max_links), "--budget-usd", str(budget_usd)]
    if asset_id:
        argv.extend(["--asset-id", asset_id])
    rc = extractor_main(argv)
    return {"return_code": rc}


# ============================================================================
# Orchestrator — single assessment
# ============================================================================

@app.function(
    image=image,
    timeout=3600,
    secrets=[anthropic_secrets, supabase_secrets, rag_secrets],
)
def orchestrator_run_one(
    asset_id: str,
    trigger_type: str = "manual",
    ensemble_n: int = 1,
    ensemble_mode: str = "streaming",
    constitutional: bool = True,
    constitutional_deterministic_only: bool = False,
    enable_premortem: bool = True,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Produce one convergence_assessment for the given asset.

    ensemble_n=1: single-shot synthesis
    ensemble_n=3+ + ensemble_mode=streaming: N concurrent live calls
    ensemble_n=3+ + ensemble_mode=batch: N via Messages Batches API
    enable_premortem: run Stage 2 (hypothesis enumeration) + Stage 3
        (adversarial pre-mortem). Default True. Disable to fall back to
        v0.2 behavior (Stage 1 + 9 + 7 + 10) if a regression is found.
    dry_run: if True, skip Stage 10 persist — runs the full pipeline
        (Anthropic costs incurred) but does NOT write convergence_assessments,
        hypothesis_enumeration, premortem_assessments, post_mortem_queue rows
        and does NOT trigger reactor fanout. Use to smoke-test prompt /
        sub-agent changes without disturbing live state. Returns
        {"assessment_id": null, "dry_run": true}.
    """
    from modal_workers.shared.supabase_client import SupabaseClient
    from orchestrator_runtime.client import (
        DEFAULT_EXTRACTOR_MODEL, DEFAULT_MODEL, OrchestratorClient,
    )
    from orchestrator_runtime.runtime import run_one

    sb = SupabaseClient()
    a_client = OrchestratorClient()
    # CLI-style invocation: no orchestrator_runs row, kill switch off by
    # default (operator already chose to spawn this run). Pass
    # hard_kill_usd via env var ORCH_HARD_KILL_USD to opt in.
    hard_kill_usd_str = os.environ.get("ORCH_HARD_KILL_USD")
    hard_kill = float(hard_kill_usd_str) if hard_kill_usd_str else None
    aid = run_one(
        sb, a_client,
        asset_id=asset_id,
        trigger_type=trigger_type,
        model=DEFAULT_MODEL,
        extractor_model=DEFAULT_EXTRACTOR_MODEL,
        ensemble_n=ensemble_n,
        ensemble_mode=ensemble_mode,
        run_constitutional=constitutional,
        constitutional_skip_semantic=constitutional_deterministic_only,
        enable_premortem=enable_premortem,
        dry_run=dry_run,
        hard_kill_usd=hard_kill,
    )
    return {"assessment_id": aid, "dry_run": dry_run}


# ============================================================================
# Run-queue drainer — scheduled cron picks up pending orchestrator_runs
# ============================================================================

@app.function(
    image=image,
    timeout=3600,
    secrets=[anthropic_secrets, supabase_secrets, rag_secrets],
    # NOTE (2026-05-07): Modal free-tier caps cron jobs at 5; conan-v2
    # already uses all 5. Drainer ships as on-demand callable for now.
    # Re-enable by upgrading plan and uncommenting:
    #   schedule=modal.Period(minutes=5),
    # Until then trigger via `modal run modal_workers/orchestrator_app.py::orchestrator_drain_queue`
    # or via Supabase pg_cron + _conan_modal_post helper.
)
def orchestrator_drain_queue(max_per_run: int = 5) -> Dict[str, Any]:
    """Drain pending orchestrator_runs rows and dispatch each to the
    orchestrator. Tier 1 (API SDK direct) is the canonical execution path
    here; Tier 2 (Cowork) and Tier 3 (Batch) are dispatched via different
    code paths.

    Picks rows where status='pending' and tier=1, oldest first. Marks each
    as 'running' before dispatch; sets to 'completed' on success or 'failed'
    on exception.
    """
    from datetime import datetime, timezone
    from modal_workers.shared.supabase_client import SupabaseClient
    from modal_workers.shared.cost_budget import (
        PER_RUN_HARD_KILL_USD, check_24h_thresholds,
    )
    from orchestrator_runtime.client import (
        BudgetExceededError, DEFAULT_EXTRACTOR_MODEL, DEFAULT_MODEL,
        OrchestratorClient,
    )
    from orchestrator_runtime.runtime import run_one

    sb = SupabaseClient()
    pending = sb._rest(
        "GET", "orchestrator_runs",
        params={
            "select": "id,asset_id,trigger_type,trigger_doc_id,tier",
            "status": "eq.pending",
            "tier": "eq.1",
            "order": "scheduled_at.asc",
            "limit": str(max_per_run),
        },
    ) or []

    # NB: each run uses a fresh OrchestratorClient so the budget accumulator
    # is isolated. Sharing a client across runs would commingle budgets.
    drained = 0
    completed = 0
    failed = 0
    killed_budget = 0

    for run_row in pending:
        run_id = run_row["id"]
        asset_id = run_row["asset_id"]
        trigger = run_row["trigger_type"]

        # Mark running
        sb._rest(
            "PATCH", "orchestrator_runs",
            params={"id": f"eq.{run_id}"},
            json_body={
                "status": "running",
                "started_at": datetime.now(timezone.utc).isoformat(),
            },
        )

        a_client = OrchestratorClient()
        try:
            aid = run_one(
                sb, a_client,
                asset_id=asset_id,
                trigger_type=trigger,
                model=DEFAULT_MODEL,
                extractor_model=DEFAULT_EXTRACTOR_MODEL,
                ensemble_n=3 if trigger in {"cross_source", "market_move",
                                            "operator_refresh"} else 1,
                ensemble_mode="streaming",
                run_constitutional=True,
                run_id=run_id,
                hard_kill_usd=PER_RUN_HARD_KILL_USD,
            )
            # cost_actual_usd lookup — convergence_assessments.cost_usd is
            # already populated by runtime.run_one() at line 638.
            cost_rows = sb._rest(
                "GET", "convergence_assessments",
                params={"id": f"eq.{aid}", "select": "cost_usd"},
            ) or []
            cost_actual = (
                float(cost_rows[0]["cost_usd"])
                if cost_rows and cost_rows[0].get("cost_usd") is not None
                else None
            )
            sb._rest(
                "PATCH", "orchestrator_runs",
                params={"id": f"eq.{run_id}"},
                json_body={
                    "status": "completed",
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "assessment_id": aid,
                    "cost_actual_usd": cost_actual,
                },
            )
            completed += 1
        except BudgetExceededError as exc:
            # Mid-run hard kill. Write the partial accumulator (we already
            # paid for every call up to and including the trigger) and mark
            # status='killed_budget' so dashboards distinguish from 'failed'.
            partial_cost = a_client.get_accumulated_cost()
            sb._rest(
                "PATCH", "orchestrator_runs",
                params={"id": f"eq.{run_id}"},
                json_body={
                    "status": "killed_budget",
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "cost_actual_usd": round(partial_cost, 4),
                    "error_message": str(exc)[:1000],
                },
            )
            killed_budget += 1
        except Exception as exc:
            sb._rest(
                "PATCH", "orchestrator_runs",
                params={"id": f"eq.{run_id}"},
                json_body={
                    "status": "failed",
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "error_message": str(exc)[:1000],
                },
            )
            failed += 1
        drained += 1

        # End-of-run 24h soft alerts (fire-and-forget). Run regardless of
        # status because killed_budget runs still contributed cost.
        try:
            check_24h_thresholds(sb, asset_id)
        except Exception as exc:  # noqa: BLE001
            # Telemetry should never break drain progression.
            pass

    return {
        "drained": drained,
        "completed": completed,
        "failed": failed,
        "killed_budget": killed_budget,
    }


# ============================================================================
# Operator-refresh endpoint — dashboard "Refresh" button creates a run
# ============================================================================

# NOTE (2026-05-07, deploy-time blocker): Modal free-tier caps web endpoints
# at 8 per workspace; conan-v2's compute RPCs already use all 8. The two
# fastapi_endpoint decorators below are commented out so the core orchestrator
# functions (asset_linker_run, fact_extractor_run, orchestrator_run_one,
# orchestrator_drain_queue, asset_linker_pass2_run) can deploy. Re-enable by
# (a) upgrading the Modal plan or (b) freeing 2 endpoints elsewhere, then
# uncommenting the @modal.fastapi_endpoint decorators below + redeploying.
# Until then: dashboard's Refresh button can call orchestrator_run_one
# directly via Modal CLI, and health is observable via `modal app list`.

# ============================================================================
# Operator-refresh endpoint — dashboard "Refresh" button creates a run
# ============================================================================

@app.function(
    image=image,
    timeout=15,
    secrets=[supabase_secrets],
)
# @modal.fastapi_endpoint(method="POST", label="orchestrator-operator-refresh")
def operator_refresh_endpoint(payload: dict) -> dict:
    """Dashboard 'Refresh' button POSTs {asset_id, [optional]trigger_doc_id}
    here. Inserts an orchestrator_runs row; orchestrator_drain_queue picks it
    up within 5 minutes (or operator can call orchestrator_run_one directly
    via Modal CLI for immediate execution)."""
    from modal_workers.shared.supabase_client import SupabaseClient

    asset_id = payload.get("asset_id")
    if not asset_id:
        return {"error": "asset_id required"}, 400
    trigger_doc_id = payload.get("trigger_doc_id")

    sb = SupabaseClient()
    rows = sb._rest(
        "POST", "orchestrator_runs",
        json_body={
            "asset_id": asset_id,
            "trigger_type": "operator_refresh",
            "trigger_doc_id": trigger_doc_id,
            "tier": 1,
            "status": "pending",
        },
        prefer="return=representation",
    )
    if not rows:
        return {"error": "failed to enqueue"}, 500
    return {"orchestrator_run_id": rows[0]["id"], "status": "queued"}


# ============================================================================
# Health check
# ============================================================================

@app.function(image=image, timeout=10)
# @modal.fastapi_endpoint(method="GET", label="orchestrator-health")
def health() -> dict:
    return {"app": "conan-v3-orchestrator", "ok": True}
