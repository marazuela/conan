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
        "jsonschema>=4.0",
    )
    .add_local_python_source("modal_workers", "orchestrator_runtime")
    # Sub-agent JSON schemas live in the sibling conan-cowork-skills repo and
    # are resolved at runtime via Path(__file__).resolve().parents[3] in
    # modal_workers/sub_agents/runtime.py. Inside the container that resolves
    # to /conan-cowork-skills/schemas/, so we mount the host path there.
    .add_local_dir(
        "../conan-cowork-skills/schemas",
        "/conan-cowork-skills/schemas",
    )
)

# Secrets
anthropic_secrets = modal.Secret.from_name("anthropic-orchestrator")
supabase_secrets = modal.Secret.from_name("supabase-secrets")
scanner_secrets = modal.Secret.from_name("scanner-secrets")
# RAG provider keys (VOYAGE_API_KEY, OPENAI_API_KEY, COHERE_API_KEY,
# RAG_PROVIDER). Wired into orchestrator_run_one so Stage 1 RAG retrieval
# (Phase 2B, ORCH_ENABLE_STAGE_1_RAG=1) can dispatch embed queries.
rag_secrets = modal.Secret.from_name("rag-providers")
# Shared compute auth secret. Same value as conan-v2's compute-auth secret +
# Supabase internal_config.compute_secret. Required by the v3 multiplex
# FastAPI endpoint (compute_v3_dispatch).
compute_auth_secrets = modal.Secret.from_name("compute-auth")


# ============================================================================
# Targeted asset ingestion — pull primary docs for one asset on demand.
# Used by Gate 1 to ground sub-agents against real ClinicalTrials.gov + OpenFDA
# data instead of Sonnet's prior knowledge alone.
# ============================================================================

@app.function(
    image=image,
    timeout=3600,
    secrets=[supabase_secrets, scanner_secrets],
)
def ingest_asset_corpus(
    ct_query: Optional[str] = None,
    nct_ids: Optional[str] = None,             # comma-separated
    fda_application_number: Optional[str] = None,
    drug_label_search: Optional[str] = None,   # openfda label keyword
) -> Dict[str, Any]:
    """One-shot grounded-corpus ingest for a target asset.

    Each parameter is independent — pass any subset. Outputs land in `documents`
    (deduped by source_content_hash) and are picked up by `asset_linker_run` on
    the next pass.
    """
    out: Dict[str, Any] = {}

    if ct_query:
        from modal_workers.ingestion.clinicaltrials_ingest import ingest_search
        r = ingest_search(query_term=ct_query, max_pages=2)
        out["ct_search"] = {
            "query": ct_query,
            "seen": r.documents_seen,
            "written": r.documents_written,
            "dedup": r.documents_dedup_hit,
            "errors": r.errors,
        }

    if nct_ids:
        from modal_workers.ingestion.clinicaltrials_ingest import ingest_by_nct
        ids = [s.strip() for s in nct_ids.split(",") if s.strip()]
        r = ingest_by_nct(nct_ids=ids)
        out["ct_nct"] = {
            "ids": ids,
            "seen": r.documents_seen,
            "written": r.documents_written,
            "dedup": r.documents_dedup_hit,
            "errors": r.errors,
        }

    if fda_application_number:
        from modal_workers.ingestion.openfda_ingest import ingest_drugsfda_approvals
        r = ingest_drugsfda_approvals(application_search=fda_application_number)
        out["fda_drugsfda"] = {
            "application_number": fda_application_number,
            "seen": r.documents_seen,
            "written": r.documents_written,
            "dedup": r.documents_dedup_hit,
            "errors": r.errors,
        }

    if drug_label_search:
        # Reuse the recent-label sweep; the openfda search-string is keyword-based
        # so passing a brand+generic narrows quickly without an explicit endpoint.
        from modal_workers.ingestion.openfda_ingest import ingest_drug_label_recent
        # Wider 365d window for a one-off targeted backfill.
        from datetime import date, timedelta
        until = date.today()
        since = until - timedelta(days=365)
        r = ingest_drug_label_recent(since=since, until=until)
        out["fda_label"] = {
            "window_days": 365,
            "seen": r.documents_seen,
            "written": r.documents_written,
            "dedup": r.documents_dedup_hit,
            "errors": r.errors,
            "note": "openfda label sweep is window-scoped; filter happens at asset_linker stage",
        }

    return out


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
    enable_sub_agents: bool = False,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Produce one convergence_assessment for the given asset.

    ensemble_n=1: single-shot synthesis
    ensemble_n=3+ + ensemble_mode=streaming: N concurrent live calls
    ensemble_n=3+ + ensemble_mode=batch: N via Messages Batches API
    enable_premortem: run Stage 2 (hypothesis enumeration) + Stage 3
        (adversarial pre-mortem). Default True. Disable to fall back to
        v0.2 behavior (Stage 1 + 9 + 7 + 10) if a regression is found.
    enable_sub_agents: when True, sets ORCH_ENABLE_SUB_AGENTS=1 in process
        env BEFORE orchestrator_runtime imports, which flips the Stage 1
        dispatch_sub_agent tool on. Sub-agents (literature, competitive,
        regulatory_history, options_microstructure) are then called via the
        in-process MCP equivalents and their outputs land in sub_agent_calls.
        Default False (PRD §5: "ORCH_ENABLE_SUB_AGENTS=0 default; flips at
        Phase 2C"). Operator opt-in only until prompt + schema lock.
    dry_run: if True, skip Stage 10 persist — runs the full pipeline
        (Anthropic costs incurred) but does NOT write convergence_assessments,
        hypothesis_enumeration, premortem_assessments, post_mortem_queue rows
        and does NOT trigger reactor fanout. Use to smoke-test prompt /
        sub-agent changes without disturbing live state. Returns
        {"assessment_id": null, "dry_run": true}.
    """
    # Stage 1 dispatch flag is read at orchestrator_runtime import time, so
    # it must be set BEFORE the lazy imports below.
    if enable_sub_agents:
        os.environ["ORCH_ENABLE_SUB_AGENTS"] = "1"

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
    # No @modal.Period — the drain is triggered by Supabase pg_cron job
    # `v3-orchestrator-drain` (every 5 min) via the compute_v3 multiplex
    # endpoint's `orchestrator_drain_queue` action. See migration
    # supabase/migrations/20260518000010_v3_orchestrator_drain_pg_cron.sql.
    # Rationale: Modal free-tier caps cron decorators at 5/workspace and
    # conan-v2 already uses all 5. Manual one-shot invocation still works:
    #   modal run modal_workers/orchestrator_app.py::orchestrator_drain_queue
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

# NOTE (2026-05-08, slot accounting): Modal free-tier caps web endpoints at
# 8 per workspace. v2 dropped its `health` HTTP endpoint on 2026-05-08
# (now a plain @app.function callable via `modal run conan-v2::health`),
# freeing 1 slot. v3 currently uses 1 slot via `compute_v3_dispatch`
# (the multiplex above). Workspace total: 7 (v2) + 1 (v3) = 8/8.
#
# The two endpoints below stay commented out — adding either would put us
# at 9/8 and the deploy would fail. To re-enable them in the future:
#   (a) drop another v2 endpoint to a plain @app.function, or
#   (b) fold their logic into compute_v3_dispatch as new actions
#       (operator_refresh becomes action='operator_refresh',
#        health becomes action='health'), or
#   (c) upgrade the Modal plan.
# Until then: dashboard's Refresh button calls orchestrator_run_one
# directly via Modal CLI; health smoke is `modal run conan-v3-orchestrator::health`.

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
# Phase 4B — Tier-2 (Cowork bulk) dispatch surface
# ============================================================================
#
# Three sync Modal functions form the contract Cowork calls into. The
# bulk_orchestrator skill itself runs ON the Cowork machine (not on Modal);
# these endpoints are the bridge between Cowork's local skill execution and
# the production DB / queue.
#
# Cowork-side flow (per scheduled cadence):
#   1. Resolve a list of asset_ids due (per fda_assets.watch_priority).
#   2. Modal call: tier2_bulk_enqueue(asset_ids) → for each asset, creates a
#      pending orchestrator_runs row (tier=2) AND returns the input blob the
#      skill consumes. Single round-trip.
#   3. For each asset, run the bulk_orchestrator skill against the blob.
#   4. Modal call: tier2_complete(run_id, payload) per success → validates,
#      persists tier=2 convergence_assessments, applies §Escalation rule
#      (high conviction / direction change / new primary doc), enqueues
#      tier1 escalation if triggered, marks the run completed.
#   5. Modal call: tier2_fail(run_id, error) per skill error → marks failed.
#
# All deterministic logic (validator, persister, escalation rule) lives in
# orchestrator_runtime/tier2.py — these endpoints are thin glue.
# ============================================================================

@app.function(
    image=image,
    timeout=120,
    secrets=[supabase_secrets],
)
def tier2_bulk_enqueue(asset_ids: list) -> Dict[str, Any]:
    """Phase 4B: enqueue Tier-2 scheduled bulk runs. Thin wrapper around
    `orchestrator_runtime.tier2.enqueue_tier2_bulk` — see that function for
    the full contract."""
    from modal_workers.shared.supabase_client import SupabaseClient
    from orchestrator_runtime.tier2 import enqueue_tier2_bulk

    return enqueue_tier2_bulk(SupabaseClient(), asset_ids)


@app.function(
    image=image,
    timeout=120,
    secrets=[supabase_secrets],
)
def tier2_complete(
    run_id: str,
    payload: Dict[str, Any],
    cost_usd: float = 0.0,
    latency_ms: Optional[int] = None,
) -> Dict[str, Any]:
    """Phase 4B: Cowork posts a completed Tier-2 skill run here. Thin
    wrapper around `orchestrator_runtime.tier2.complete_tier2_run`."""
    from modal_workers.shared.supabase_client import SupabaseClient
    from orchestrator_runtime.tier2 import complete_tier2_run

    return complete_tier2_run(
        SupabaseClient(), run_id, payload,
        cost_usd=cost_usd, latency_ms=latency_ms,
    )


@app.function(
    image=image,
    timeout=15,
    secrets=[supabase_secrets],
)
def tier2_fail(run_id: str, error_message: str) -> Dict[str, Any]:
    """Phase 4B: Cowork reports a Tier-2 skill error. Thin wrapper around
    `orchestrator_runtime.tier2.fail_tier2_run`."""
    from modal_workers.shared.supabase_client import SupabaseClient
    from orchestrator_runtime.tier2 import fail_tier2_run

    return fail_tier2_run(SupabaseClient(), run_id, error_message)


# ============================================================================
# compute_v3_dispatch — multiplex FastAPI endpoint
#
# One Modal endpoint slot, N logical compute operations. Cowork-side skills
# (and the Supabase RPC bridges in supabase/migrations/2026051*_v3_compute_rpcs.sql)
# call this endpoint with {"action": "<name>", "args": {...}} bodies; this
# function dispatches to the right helper in orchestrator_runtime.
#
# WHY one slot: Modal's free-tier 8 fastapi_endpoint cap is fully consumed by
# conan-v2's compute RPCs. Spinning up a separate FastAPI endpoint per v3
# compute action would push us over the cap. A single multiplexer keeps v3
# at exactly one HTTP-level slot regardless of how many actions we add.
#
# Auth: requires `x-conan-compute-secret` header (same shared secret as v2).
# Mirrors the v2 `_verify_compute_secret` pattern (constant-time compare,
# 401 on mismatch, 500 on server misconfiguration).
#
# Action contract:
#   tier2_bulk_enqueue: args={asset_ids: [str, ...]}
#   tier2_complete:    args={run_id, payload, cost_usd?, latency_ms?}
#   tier2_fail:        args={run_id, error_message}
#   ic_memo_run:       args={assessment_id, question?, persist?}
#
# Each action's response shape matches the underlying runtime helper's
# return value verbatim — see orchestrator_runtime.tier2 / ic_memo_runner
# for the per-action contracts.
# ============================================================================

# Importable for tests (without spinning up Modal at import time).
COMPUTE_V3_ACTIONS = frozenset({
    "tier2_bulk_enqueue",
    "tier2_complete",
    "tier2_fail",
    "ic_memo_run",
    "feedback_loop_kickoff",
    "orchestrator_drain_queue",
    "asset_linker_run",
    "asset_linker_pass2_run",
    "fact_extractor_run",
})


def _verify_compute_secret(provided: Optional[str]) -> None:
    """Raise FastAPI HTTPException if `provided` doesn't match
    CONAN_COMPUTE_SECRET. Constant-time compare so an attacker can't
    learn the prefix byte-by-byte."""
    import hmac
    from fastapi import HTTPException

    expected = os.environ.get("CONAN_COMPUTE_SECRET", "")
    if not expected:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "server misconfiguration: CONAN_COMPUTE_SECRET not set",
            },
        )
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(
            status_code=401,
            detail={"error": "invalid or missing x-conan-compute-secret"},
        )


def _dispatch_compute_v3_action(action: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Pure dispatcher: route `action` to the right helper. Imported by
    the FastAPI endpoint AND by tests (so we don't have to import the
    Modal app to exercise routing)."""
    if action not in COMPUTE_V3_ACTIONS:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=400,
            detail={
                "error": f"unknown action {action!r}",
                "valid_actions": sorted(COMPUTE_V3_ACTIONS),
            },
        )

    if action == "feedback_loop_kickoff":
        # Lookup-and-spawn against the deployed conan-v3-feedback-loop app
        # so this endpoint returns in <1s while the daily chain runs up to
        # 7200s on its own task. Fired by pg_cron job
        # `v3-feedback-loop-daily` (02:00 UTC).
        fn = modal.Function.from_name(
            "conan-v3-feedback-loop", "daily_feedback_loop",
        )
        kwargs: Dict[str, Any] = {}
        for k in ("drain_batch_size", "monitor_window_days",
                  "refit_min_n", "refit_bootstrap_resamples"):
            if k in args:
                kwargs[k] = args[k]
        handle = fn.spawn(**kwargs)
        return {"spawned": True, "function_call_id": handle.object_id}

    if action == "orchestrator_drain_queue":
        # Fire-and-forget spawn of the in-cluster drainer so pg_cron's
        # HTTP POST returns in <1s while drain itself can run up to
        # 3600s. Replaces the @modal.Period(minutes=5) decorator path
        # so we don't consume a Modal cron slot. Fired by pg_cron job
        # `v3-orchestrator-drain` (*/5 * * * *).
        fn = modal.Function.from_name(
            "conan-v3-orchestrator", "orchestrator_drain_queue",
        )
        kwargs: Dict[str, Any] = {}
        if "max_per_run" in args:
            kwargs["max_per_run"] = args["max_per_run"]
        handle = fn.spawn(**kwargs)
        return {"spawned": True, "function_call_id": handle.object_id}

    if action == "asset_linker_run":
        # Fire-and-forget spawn of the pass-1 Sonnet asset_linker so pg_cron
        # returns in <1s while the linker runs up to 3600s. Fired by pg_cron
        # job `v3-asset-linker-pass1` (every 15 min).
        fn = modal.Function.from_name(
            "conan-v3-orchestrator", "asset_linker_run",
        )
        kwargs: Dict[str, Any] = {}
        for k in ("asset_id", "max_docs", "budget_usd"):
            if k in args:
                kwargs[k] = args[k]
        handle = fn.spawn(**kwargs)
        return {"spawned": True, "function_call_id": handle.object_id}

    if action == "asset_linker_pass2_run":
        # Fire-and-forget spawn of the pass-2 Haiku verifier over
        # low-confidence pass-1 links. Fired by pg_cron job
        # `v3-asset-linker-pass2` (twice hourly at :10/:40, offset from pass-1).
        fn = modal.Function.from_name(
            "conan-v3-orchestrator", "asset_linker_pass2_run",
        )
        kwargs: Dict[str, Any] = {}
        for k in ("asset_id", "max_links", "threshold", "budget_usd"):
            if k in args:
                kwargs[k] = args[k]
        handle = fn.spawn(**kwargs)
        return {"spawned": True, "function_call_id": handle.object_id}

    if action == "fact_extractor_run":
        # Fire-and-forget spawn of the Sonnet fact_extractor over material
        # asset_documents links. Fired by pg_cron job `v3-fact-extractor`
        # (hourly at :20).
        fn = modal.Function.from_name(
            "conan-v3-orchestrator", "fact_extractor_run",
        )
        kwargs: Dict[str, Any] = {}
        for k in ("asset_id", "max_links", "budget_usd"):
            if k in args:
                kwargs[k] = args[k]
        handle = fn.spawn(**kwargs)
        return {"spawned": True, "function_call_id": handle.object_id}

    from modal_workers.shared.supabase_client import SupabaseClient
    from orchestrator_runtime.tier2 import (
        complete_tier2_run,
        enqueue_tier2_bulk,
        fail_tier2_run,
    )
    from orchestrator_runtime.ic_memo_runner import run_ic_memo

    sb = SupabaseClient()
    if action == "tier2_bulk_enqueue":
        return enqueue_tier2_bulk(sb, args["asset_ids"])
    if action == "tier2_complete":
        return complete_tier2_run(
            sb,
            args["run_id"],
            args["payload"],
            cost_usd=args.get("cost_usd", 0.0),
            latency_ms=args.get("latency_ms"),
        )
    if action == "tier2_fail":
        return fail_tier2_run(sb, args["run_id"], args.get("error_message", ""))
    # ic_memo_run
    return run_ic_memo(
        sb,
        args["assessment_id"],
        question=args.get("question"),
        persist=args.get("persist", True),
    )


def _compute_v3_header_default():
    """Late binding so imports don't fail when fastapi isn't installed
    (e.g. during local pytest runs that don't exercise the endpoint)."""
    from fastapi import Header
    return Header(default=None)


@app.function(
    image=image,
    timeout=120,
    secrets=[supabase_secrets, anthropic_secrets, compute_auth_secrets],
)
@modal.fastapi_endpoint(method="POST", label="compute-v3")
def compute_v3_dispatch(
    payload: dict,
    x_conan_compute_secret: Optional[str] = _compute_v3_header_default(),
) -> Dict[str, Any]:
    """Multiplex compute endpoint. Single Modal slot, N actions.

    Body: {"action": "<name>", "args": {...}}.
    Header: x-conan-compute-secret.

    The deployed URL is seeded into Supabase
    `internal_config.modal_url_compute_v3` by the v3 compute RPCs migration;
    SQL `rpc_tier2_*` / `rpc_ic_memo_run` wrappers POST here via pg_net.
    """
    _verify_compute_secret(x_conan_compute_secret)

    if not isinstance(payload, dict):
        from fastapi import HTTPException
        raise HTTPException(
            status_code=400,
            detail={"error": "payload must be a JSON object"},
        )
    action = payload.get("action")
    args = payload.get("args") or {}
    if not isinstance(action, str) or not action:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=400,
            detail={"error": "missing required field: action"},
        )
    if not isinstance(args, dict):
        from fastapi import HTTPException
        raise HTTPException(
            status_code=400,
            detail={"error": "args must be a JSON object"},
        )

    return _dispatch_compute_v3_action(action, args)


# ============================================================================
# Health check
# ============================================================================

@app.function(image=image, timeout=10)
# @modal.fastapi_endpoint(method="GET", label="orchestrator-health")
def health() -> dict:
    return {"app": "conan-v3-orchestrator", "ok": True}
