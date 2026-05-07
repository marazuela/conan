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

# v3-specific image — adds anthropic SDK to the v2 base list.
image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install(
        "fastapi[standard]",
        "pydantic>=2",
        "requests>=2.31",
        "anthropic>=0.50",
    )
    .add_local_python_source("modal_workers", "orchestrator_runtime")
)

# Secrets
anthropic_secrets = modal.Secret.from_name("anthropic-orchestrator")
supabase_secrets = modal.Secret.from_name("supabase-secrets")
scanner_secrets = modal.Secret.from_name("scanner-secrets")


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
    secrets=[anthropic_secrets, supabase_secrets],
)
def orchestrator_run_one(
    asset_id: str,
    trigger_type: str = "manual",
    ensemble_n: int = 1,
    ensemble_mode: str = "streaming",
    constitutional: bool = True,
    constitutional_deterministic_only: bool = False,
) -> Dict[str, Any]:
    """Produce one convergence_assessment for the given asset.

    ensemble_n=1: single-shot synthesis
    ensemble_n=3+ + ensemble_mode=streaming: N concurrent live calls
    ensemble_n=3+ + ensemble_mode=batch: N via Messages Batches API
    """
    from modal_workers.shared.supabase_client import SupabaseClient
    from orchestrator_runtime.client import (
        DEFAULT_EXTRACTOR_MODEL, DEFAULT_MODEL, OrchestratorClient,
    )
    from orchestrator_runtime.runtime import run_one

    sb = SupabaseClient()
    a_client = OrchestratorClient()
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
    )
    return {"assessment_id": aid}


# ============================================================================
# Run-queue drainer — scheduled cron picks up pending orchestrator_runs
# ============================================================================

@app.function(
    image=image,
    timeout=3600,
    secrets=[anthropic_secrets, supabase_secrets],
    schedule=modal.Period(minutes=5),
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
    from orchestrator_runtime.client import (
        DEFAULT_EXTRACTOR_MODEL, DEFAULT_MODEL, OrchestratorClient,
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

    a_client = OrchestratorClient()
    drained = 0
    completed = 0
    failed = 0

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
            )
            sb._rest(
                "PATCH", "orchestrator_runs",
                params={"id": f"eq.{run_id}"},
                json_body={
                    "status": "completed",
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "assessment_id": aid,
                },
            )
            completed += 1
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

    return {"drained": drained, "completed": completed, "failed": failed}


# ============================================================================
# Operator-refresh endpoint — dashboard "Refresh" button creates a run
# ============================================================================

@app.function(
    image=image,
    timeout=15,
    secrets=[supabase_secrets],
)
@modal.fastapi_endpoint(method="POST", label="orchestrator-operator-refresh")
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

@app.function(image=image, timeout=5)
@modal.fastapi_endpoint(method="GET", label="orchestrator-health")
def health() -> dict:
    return {"app": "conan-v3-orchestrator", "ok": True}
