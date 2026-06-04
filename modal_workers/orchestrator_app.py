"""Modal app for the v3 orchestrator runtime.

Separate from the v2 `conan-v2` Modal app so v3 can be deployed + iterated
without touching v2 scanner / compute-endpoint registrations. Deploy with:

  modal deploy modal_workers/orchestrator_app.py

Functions:
  asset_linker_run         — run extractor.asset_linker over unlinked docs
                             for one asset (or all is_active assets)
  fact_extractor_run       — disabled placeholder; local skill workflow only
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
        # Phase 3a — earnings_calendar fetcher uses yfinance as the primary
        # source for earnings dates (Polygon is the fallback). Smoke test on
        # 2026-05-25 hit a silent ImportError; pinning at >=0.2.40 since
        # that's the first release with the working get_earnings_dates()
        # signature we depend on.
        #
        # lxml is required because yfinance.Ticker.get_earnings_dates()
        # parses Yahoo's HTML via lxml under the hood — without it the call
        # raises 'Import lxml failed' and the fetcher returns 0 rows.
        # curl_cffi is also pinned to bypass Yahoo's anti-bot detection
        # which started rejecting plain `requests` in mid-2024.
        "yfinance>=0.2.40",
        "lxml>=5.0",
        "curl_cffi>=0.6",
    )
    # v4 Phase 6c (PR #152) deleted the v3 codepath entirely, so the
    # ORCH_V4 env var that Phase 6a (#150) injected here is now dead config.
    # Modal client v1.4.2+ also rejects `.env()` after `add_local_*` as an
    # out-of-order build step, so the line had become a deploy blocker.
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
    """Disabled production entrypoint.

    Asset linking now runs through the project-local Cursor skill so production
    cannot burn the Modal Anthropic API key on document classification.
    """
    return {
        "return_code": 0,
        "disabled": True,
        "reason": "asset_linker_run disabled; use the local asset-linker skill",
    }


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
    """Disabled production entrypoint.

    Pass-2 verification belongs to the same local asset-linker skill workflow.
    """
    return {
        "return_code": 0,
        "disabled": True,
        "reason": "asset_linker_pass2_run disabled; use the local asset-linker skill",
    }


# ============================================================================
# Fact extractor — disabled Sonnet structured fact extraction
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
    """Disabled production entrypoint.

    Fact extraction is moving to local skill execution; production Modal cron
    must not spend the shared Anthropic API key on background extraction.
    """
    return {
        "return_code": 0,
        "disabled": True,
        "reason": "fact_extractor_run disabled; use the local fact-extraction skill",
    }


# ============================================================================
# Asset-alias seed refresh — refreshes fda_asset_aliases from openFDA and
# ClinicalTrials.gov. Cron-fired weekly so newly approved brands and newly
# registered trials get added to the alias index without operator action.
# Zero LLM cost — only public HTTP APIs + Supabase writes. The full
# `seed_fda_asset_aliases.py` script with all four sources is also runnable
# manually for the initial seed pass.
# ============================================================================

@app.function(
    image=image,
    timeout=1800,
    secrets=[supabase_secrets],
)
def seed_fda_asset_aliases_refresh(
    sources: str = "openfda_label,clinicaltrials_v2",
) -> Dict[str, Any]:
    """Refresh fda_asset_aliases from the public-API sources. Designed for
    the v3-asset-alias-weekly-refresh pg_cron job. Defaults to openFDA labels
    + ClinicalTrials.gov; `curated_map` and `extensions_mining` are skipped
    here because they don't change between scheduled runs (curated_map ships
    with the code, extensions_mining catches up via the initial seed pass)."""
    from modal_workers.scripts.seed_fda_asset_aliases import main as seed_main

    argv = ["--sources", sources]
    rc = seed_main(argv)
    return {"return_code": rc, "sources": sources}


# ============================================================================
# Orchestrator — single assessment
# ============================================================================

# Phase 2C prep: 5-role sub-agent pipeline (literature + competitive +
# regulatory_history + options_microstructure + commercial_opportunity) blew
# the 200k aggregate cap on the 2026-05-27 VRDN dry-run — regulatory_history
# saw only 18,521 tokens remaining and short-circuited. `env=` here wins over
# the same key in `anthropic-orchestrator` secret, so this is the canonical
# place to tune the cap going forward. Bump target lifted from
# memory/sub_agent_schema_drift_2026-05-23.md S-3.
@app.function(
    image=image,
    timeout=3600,
    secrets=[anthropic_secrets, supabase_secrets, rag_secrets],
    env={"ORCH_SUB_AGENT_BUDGET_TOKENS": "800000"},
)
def orchestrator_run_one(
    asset_id: str,
    trigger_type: str = "manual",
    enable_sub_agents: bool = False,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Produce one convergence_assessment for the given asset.

    enable_sub_agents: when True, sets ORCH_ENABLE_SUB_AGENTS=1 in process
        env BEFORE orchestrator_runtime imports, which flips the Stage 1
        dispatch_sub_agent tool on. Sub-agents (literature, competitive,
        regulatory_history, options_microstructure) are then called via the
        in-process MCP equivalents and their outputs land in sub_agent_calls.
        Default False (PRD §5: "ORCH_ENABLE_SUB_AGENTS=0 default; flips at
        Phase 2C"). Operator opt-in only until prompt + schema lock.
    dry_run: if True, skip Stage 10 persist — runs the full pipeline
        (Anthropic costs incurred) but does NOT write convergence_assessments,
        post_mortem_queue rows and does NOT trigger reactor fanout. Use to
        smoke-test prompt / sub-agent changes without disturbing live state. Returns
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
    #
    # Phase 2C flip — RE-ENABLED 2026-06-01 after the 3-gap fix cycle.
    # Timeline (memory sub_agent_schema_drift_2026-05-23.md has the full saga):
    #   2026-05-27 09:46 UTC: first enabled via PR #157 (buried in "Diag/stage7"
    #     dump). 18 dispatches, $8.98 burned, all assessment_id=NULL → $0 output.
    #   2026-05-28 09:42 UTC: PR #173 reverted the flip.
    #   2026-06-01 13:01 UTC: PR #174 fixed the 3 root causes (literature skill
    #     drift, orchestrator_run_id join + back-fill, persist defensive defaults).
    #     PR #177 added commercial_opportunity to the role CHECK constraint.
    #   2026-06-01 13:11 UTC: VRDN dry-run with --enable-sub-agents:
    #     competitive/regulatory_history/options_microstructure/commercial_opportunity
    #     all schema_pass=true; literature not dispatched for this asset (TED
    #     indication) — fix is doc-only, low regression risk.
    # Budget cap (350k tokens aggregate per assessment) stays — per-run hard halt
    # fires before any single assessment can burn what 2026-05-27 burned all day.
    # Monitor failed_reactor_events for sub_agent.* sources for 24h post-flip.
    # 2026-06-02: the empty-{} sub-agent failures were root-caused + fixed across
    # #188 (budget/label) #191 (chain_handlers) #192 (persist diag) #193 (force
    # synthesis) #194 (output cap) + parallel #189 (schema-retry + degraded
    # fallback). commercial_opportunity now passes (validated full payload on the
    # coherent-main dry-run) -> RE-ENABLED. literature stays OFF: it never fired in
    # any dry-run, so it is still unvalidated — re-enable only after it fires +
    # shows schema_pass=true on a literature-triggering asset.
    # See memory sub_agent_schema_drift_2026-05-23.md (Round-7).
    env={
        "ORCH_ENABLE_SUB_AGENTS": "1",
        # 800k aggregate (raised from 350k) + 200k/role cap (sub_agent_dispatcher)
        # so dispatch order no longer starves later roles. $15/run hard-kill unchanged.
        "ORCH_SUB_AGENT_BUDGET_TOKENS": "800000",
        "ORCH_DISABLE_LITERATURE": "1",  # unvalidated (never fired) — keep off
    },
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
        PER_RUN_HARD_KILL_USD,
        check_24h_thresholds,
        check_orchestrator_hard_halt,
    )
    from orchestrator_runtime.client import (
        BudgetExceededError, DEFAULT_EXTRACTOR_MODEL, DEFAULT_MODEL,
        OrchestratorClient,
    )
    from orchestrator_runtime.runtime import run_one

    sb = SupabaseClient()

    # 24h rolling-spend hard halt. The helper opens an operator_flag on first
    # breach (see modal_workers/shared/cost_budget.py:194-226), so the breach
    # surfaces in the dashboard without requiring a separate alert path.
    # Pending rows stay pending — they'll drain on the next tick once the
    # rolling 24h cost falls back below the ceiling. This is intentionally
    # cheaper than waiting for per-run kills to add up: per-run kill cannot
    # prevent a runaway loop of small-but-frequent runs.
    halt_state = check_orchestrator_hard_halt(sb)
    if halt_state.get("halt"):
        return {
            "drained": 0,
            "completed": 0,
            "failed": 0,
            "killed_budget": 0,
            "halt": True,
            "total_24h_usd": halt_state.get("total_24h_usd"),
        }

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
# Tier-2 (Cowork bulk) — DELETED in v4 Phase 6b.
# ============================================================================
#
# Previously hosted three Modal functions (tier2_bulk_enqueue, tier2_complete,
# tier2_fail) bridging the Cowork `bulk_orchestrator` skill to the production
# DB. Removed alongside orchestrator_runtime/tier2.py.
#
# Replacement: under v4, re-analysis is purely event-driven via the reactor's
# new_doc / cross_source / operator_refresh triggers → orchestrator_drain_queue.
# Scheduled bulk re-runs are not needed when nothing has changed; when
# something has changed, the reactor catches it. See ~/.claude/plans/proud-
# booping-seal.md §Phase 6.
#
# The fda_assets.watch_priority column stays (still useful for prioritizing
# operator attention in dashboards), but the cadence routine that drained it
# into tier2_bulk_enqueue is gone.
# ============================================================================


# ============================================================================
# Phase 3a / 3b / 4 — calendar + audit + harvest workers.
#
# These are plain @app.function workers (no fastapi_endpoint) that
# compute_v3_dispatch spawns fire-and-forget so the multiplex endpoint
# returns in <1s while the actual job runs up to its own timeout. Pattern
# mirrors orchestrator_drain_queue / feedback_loop_kickoff.
#
# The pg_cron jobs in 20260605000050 (earnings), 20260605000060 (FOMC),
# and 20260612000020 (harvest) POST to compute_v3_dispatch with the
# corresponding action — that's the only entrypoint operators need.
# ============================================================================


@app.function(
    image=image,
    timeout=900,  # 15min: ~400 tickers × ~100ms/ticker + 2s/batch-of-50
    secrets=[supabase_secrets, scanner_secrets],
)
def phase3a_earnings_calendar_fetch_worker(
    window_days: int = 7,
    forward_days: int = 90,
    tickers: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Daily refresh of public.earnings_calendar via yfinance (primary) +
    Polygon (fallback). Targets the tradeable-filter-passed universe by
    default. Phase 3a — Q1 audit feeder."""
    from modal_workers.shared.supabase_client import SupabaseClient
    from modal_workers.fetchers.universe.earnings_calendar import (
        fetch, load_tradeable_tickers,
    )

    sb = SupabaseClient()
    targets = tickers if tickers else load_tradeable_tickers(sb)
    return fetch(
        sb,
        tickers=targets,
        lookback_days=window_days,
        forward_days=forward_days,
        dry_run=False,
    )


@app.function(
    image=image,
    timeout=60,  # single HTTP fetch + parse
    secrets=[supabase_secrets],
)
def phase3a_fomc_calendar_refresh_worker(year: Optional[int] = None) -> Dict[str, Any]:
    """Monthly refresh of public.fomc_calendar via federalreserve.gov
    scrape. Phase 3a — Q1 audit feeder."""
    from modal_workers.shared.supabase_client import SupabaseClient
    from modal_workers.fetchers.universe.fomc_calendar import fetch

    return fetch(SupabaseClient(), year=year, dry_run=False)


@app.function(
    image=image,
    timeout=600,  # 10min: bounded by Polygon SPY pulls per event
    secrets=[supabase_secrets, scanner_secrets],
)
def q1_audit_run_worker(re_audit: bool = False) -> Dict[str, int]:
    """WI-5 Q1 confounder + coverage audit across eval_harness rows.
    Re-audits in place when re_audit=True."""
    from modal_workers.shared.supabase_client import SupabaseClient
    from modal_workers.scripts.audit_event_data_quality import _audit_all

    return _audit_all(SupabaseClient(), re_audit=re_audit, apply=True)


@app.function(
    image=image,
    timeout=120,  # single cohort aggregation
    secrets=[supabase_secrets],
)
def q2_audit_run_worker(profile: str = "binary_catalyst") -> Dict[str, Any]:
    """WI-6 Q2 sample-balance audit for the q1_verdict='clean' cohort.
    Persists a row in eval_sample_balance_audits and returns the verdict
    + axes summary."""
    from modal_workers.shared.supabase_client import SupabaseClient
    from modal_workers.scripts.audit_sample_balance import (
        audit_cohort, persist_q2_verdict,
    )

    sb = SupabaseClient()
    verdict = audit_cohort(sb, profile=profile)
    persist_q2_verdict(sb, verdict)
    return {
        "verdict": verdict.verdict,
        "cohort_hash": verdict.cohort_hash,
        "cohort_size": verdict.cohort_size,
        "phase5_triggers": verdict.phase5_triggers,
        "axes": {k: v.as_dict() for k, v in verdict.axes.items()},
    }


@app.function(
    image=image,
    timeout=1800,
    secrets=[supabase_secrets],
)
def calibration_refit_run_worker(
    min_n: int = 200,
    bootstrap_resamples: int = 10000,
    enable_promotion: bool = False,
    training_source: str = "post_mortem_queue",
) -> Dict[str, Any]:
    """Run the D-103 calibration refit gate in explicit manual-promotion mode."""
    from dataclasses import asdict

    from modal_workers.shared.supabase_client import SupabaseClient
    from modal_workers.scripts.nightly_calibration_refit import run_nightly_refit

    result = run_nightly_refit(
        sb=SupabaseClient(),
        min_n=min_n,
        bootstrap_resamples=bootstrap_resamples,
        enable_promotion=enable_promotion,
        training_source=training_source,
    )
    return asdict(result)


@app.function(
    image=image,
    timeout=900,  # 15min: openFDA paging over a daily window
    secrets=[supabase_secrets, scanner_secrets],
)
def fda_event_harvest_daily_worker(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> Dict[str, Any]:
    """WI-7 M1 FDA-only ongoing harvest. Defaults to (latest_checkpoint+1d
    ... today). After harvest, sweeps fda_assets.next_catalyst_date from
    the new fda_regulatory_events rows."""
    from datetime import date, timedelta
    from dataclasses import asdict

    from modal_workers.shared.supabase_client import SupabaseClient
    from modal_workers.scripts.harvest_fda_events import (
        harvest, latest_checkpoint,
    )

    sb = SupabaseClient()
    today = date.today()
    if end_date:
        end = date.fromisoformat(end_date)
    else:
        end = today
    if start_date:
        start = date.fromisoformat(start_date)
    else:
        last = latest_checkpoint(sb, "openfda")
        start = (last + timedelta(days=1)) if last else (today - timedelta(days=7))

    result = harvest(sb, start_date=start, end_date=end,
                    sources=("openfda",), dry_run=False)
    return asdict(result)


@app.function(
    image=image,
    timeout=300,  # 5min: one bulk fetch + grouped upsert; small dataset today
    secrets=[supabase_secrets],
)
def bc_class_precedent_refresh_worker(
    lookback_years: int = 10,
    apply: bool = True,
) -> Dict[str, Any]:
    """WI-2 follow-up — refresh `fda_class_precedent_base_rates` from
    `fda_regulatory_events`. Reactor reads the resulting rows to fill the
    class_precedent input in the BC convergence pre-gate (was stubbed to 0
    in v1). See `modal_workers/scripts/bc_class_precedent_refresher.py`."""
    from modal_workers.shared.supabase_client import SupabaseClient
    from modal_workers.scripts.bc_class_precedent_refresher import refresh

    return refresh(
        SupabaseClient(),
        lookback_years=lookback_years,
        apply=apply,
    )


# ============================================================================
# Phase 3a / 3b / 4 — calendar + audit + harvest workers.
#
# These are plain @app.function workers (no fastapi_endpoint) that
# compute_v3_dispatch spawns fire-and-forget so the multiplex endpoint
# returns in <1s while the actual job runs up to its own timeout. Pattern
# mirrors orchestrator_drain_queue / feedback_loop_kickoff.
#
# The pg_cron jobs in 20260605000050 (earnings), 20260605000060 (FOMC),
# and 20260612000020 (harvest) POST to compute_v3_dispatch with the
# corresponding action — that's the only entrypoint operators need.
# ============================================================================


@app.function(
    image=image,
    timeout=900,  # 15min: ~400 tickers × ~100ms/ticker + 2s/batch-of-50
    secrets=[supabase_secrets, scanner_secrets],
)
def phase3a_earnings_calendar_fetch_worker(
    window_days: int = 7,
    forward_days: int = 90,
    tickers: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Daily refresh of public.earnings_calendar via yfinance (primary) +
    Polygon (fallback). Targets the tradeable-filter-passed universe by
    default. Phase 3a — Q1 audit feeder."""
    from modal_workers.shared.supabase_client import SupabaseClient
    from modal_workers.fetchers.universe.earnings_calendar import (
        fetch, load_tradeable_tickers,
    )

    sb = SupabaseClient()
    targets = tickers if tickers else load_tradeable_tickers(sb)
    return fetch(
        sb,
        tickers=targets,
        lookback_days=window_days,
        forward_days=forward_days,
        dry_run=False,
    )


@app.function(
    image=image,
    timeout=60,  # single HTTP fetch + parse
    secrets=[supabase_secrets],
)
def phase3a_fomc_calendar_refresh_worker(year: Optional[int] = None) -> Dict[str, Any]:
    """Monthly refresh of public.fomc_calendar via federalreserve.gov
    scrape. Phase 3a — Q1 audit feeder."""
    from modal_workers.shared.supabase_client import SupabaseClient
    from modal_workers.fetchers.universe.fomc_calendar import fetch

    return fetch(SupabaseClient(), year=year, dry_run=False)


@app.function(
    image=image,
    timeout=600,  # 10min: bounded by Polygon SPY pulls per event
    secrets=[supabase_secrets, scanner_secrets],
)
def q1_audit_run_worker(re_audit: bool = False) -> Dict[str, int]:
    """WI-5 Q1 confounder + coverage audit across eval_harness rows.
    Re-audits in place when re_audit=True."""
    from modal_workers.shared.supabase_client import SupabaseClient
    from modal_workers.scripts.audit_event_data_quality import _audit_all

    return _audit_all(SupabaseClient(), re_audit=re_audit, apply=True)


@app.function(
    image=image,
    timeout=120,  # single cohort aggregation
    secrets=[supabase_secrets],
)
def q2_audit_run_worker(profile: str = "binary_catalyst") -> Dict[str, Any]:
    """WI-6 Q2 sample-balance audit for the q1_verdict='clean' cohort.
    Persists a row in eval_sample_balance_audits and returns the verdict
    + axes summary."""
    from modal_workers.shared.supabase_client import SupabaseClient
    from modal_workers.scripts.audit_sample_balance import (
        audit_cohort, persist_q2_verdict,
    )

    sb = SupabaseClient()
    verdict = audit_cohort(sb, profile=profile)
    persist_q2_verdict(sb, verdict)
    return {
        "verdict": verdict.verdict,
        "cohort_hash": verdict.cohort_hash,
        "cohort_size": verdict.cohort_size,
        "phase5_triggers": verdict.phase5_triggers,
        "axes": {k: v.as_dict() for k, v in verdict.axes.items()},
    }


@app.function(
    image=image,
    timeout=900,  # 15min: openFDA paging over a daily window
    secrets=[supabase_secrets, scanner_secrets],
)
def fda_event_harvest_daily_worker(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> Dict[str, Any]:
    """WI-7 M1 FDA-only ongoing harvest. Defaults to (latest_checkpoint+1d
    ... today). After harvest, sweeps fda_assets.next_catalyst_date from
    the new fda_regulatory_events rows."""
    from datetime import date, timedelta
    from dataclasses import asdict

    from modal_workers.shared.supabase_client import SupabaseClient
    from modal_workers.scripts.harvest_fda_events import (
        harvest, latest_checkpoint,
    )

    sb = SupabaseClient()
    today = date.today()
    if end_date:
        end = date.fromisoformat(end_date)
    else:
        end = today
    if start_date:
        start = date.fromisoformat(start_date)
    else:
        last = latest_checkpoint(sb, "openfda")
        start = (last + timedelta(days=1)) if last else (today - timedelta(days=7))

    result = harvest(sb, start_date=start, end_date=end,
                    sources=("openfda",), dry_run=False)
    return asdict(result)


@app.function(
    image=image,
    timeout=300,  # 5min: one bulk fetch + grouped upsert; small dataset today
    secrets=[supabase_secrets],
)
def bc_class_precedent_refresh_worker(
    lookback_years: int = 10,
    apply: bool = True,
) -> Dict[str, Any]:
    """WI-2 follow-up — refresh `fda_class_precedent_base_rates` from
    `fda_regulatory_events`. Reactor reads the resulting rows to fill the
    class_precedent input in the BC convergence pre-gate (was stubbed to 0
    in v1). See `modal_workers/scripts/bc_class_precedent_refresher.py`."""
    from modal_workers.shared.supabase_client import SupabaseClient
    from modal_workers.scripts.bc_class_precedent_refresher import refresh

    return refresh(
        SupabaseClient(),
        lookback_years=lookback_years,
        apply=apply,
    )


@app.function(
    image=image,
    timeout=900,  # per-asset openFDA sponsor lookup + PATCH; headroom as the
                  # active universe grows past today's ~80 assets
    secrets=[supabase_secrets],
)
def enrich_fda_asset_designations_worker(
    stale_hours: int = 20,
    limit: int = 500,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Hydrate the fda_assets binary-catalyst pre-gate inputs (priority_review,
    breakthrough_designation, sponsor_prior_nda_count, first_time_sponsor,
    designations_enriched_at). The reactor's bc-pregate reads these at dispatch;
    un-enriched assets fail-open (pass), so new/changed assets stay ungated until
    this runs. The daily cron passes stale_hours=20 so the 24h cadence re-enriches
    the whole universe while a same-day manual run isn't reprocessed. Enricher +
    inputs land in PR #200; see
    modal_workers/scripts/enrich_fda_asset_designations.py."""
    from modal_workers.scripts.enrich_fda_asset_designations import main

    argv = ["--stale-hours", str(stale_hours), "--limit", str(limit)]
    if dry_run:
        argv.append("--dry-run")
    exit_code = main(argv)
    return {
        "exit_code": exit_code,
        "stale_hours": stale_hours,
        "limit": limit,
        "dry_run": dry_run,
    }


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
#   ic_memo_run:       args={assessment_id, question?, persist?}
#
# Asset linking and fact extraction intentionally are not exposed here. They
# are local skill workflows now, so production cannot spend the Modal Anthropic
# API key on background extraction/classification.
#
# Tier-2 actions (tier2_bulk_enqueue / tier2_complete / tier2_fail) were
# removed in v4 Phase 6b — the Cowork bulk_orchestrator pipeline is sunset.
# Re-analysis under v4 is purely event-driven via the reactor.
#
# Each action's response shape matches the underlying runtime helper's
# return value verbatim — see orchestrator_runtime/ic_memo_runner.py for the
# per-action contract.
# ============================================================================

# Importable for tests (without spinning up Modal at import time).
COMPUTE_V3_ACTIONS = frozenset({
    "ic_memo_run",
    "feedback_loop_kickoff",
    "orchestrator_drain_queue",
    "seed_fda_asset_aliases_refresh",
    # Phase 3a/3b/4 — calendar + audit + harvest workers (all spawned).
    "earnings_calendar_fetch_daily",
    "fomc_calendar_refresh",
    "q1_audit_run",
    "q2_audit_run",
    "calibration_refit_run",
    "fda_event_harvest_daily",
    "bc_class_precedent_refresh",
    "enrich_fda_asset_designations",
})

# Spawn-only actions: dispatch fires the worker and returns immediately. Used
# for any work that exceeds the multiplex endpoint's 120s timeout or that we
# explicitly want async so pg_cron's HTTP POST doesn't sit on a long task.
_SPAWN_ONLY_ACTIONS: Dict[str, str] = {
    "feedback_loop_kickoff": "daily_feedback_loop",
    "orchestrator_drain_queue": "orchestrator_drain_queue",
    "seed_fda_asset_aliases_refresh": "seed_fda_asset_aliases_refresh",
    "earnings_calendar_fetch_daily": "phase3a_earnings_calendar_fetch_worker",
    "fomc_calendar_refresh": "phase3a_fomc_calendar_refresh_worker",
    "q1_audit_run": "q1_audit_run_worker",
    "q2_audit_run": "q2_audit_run_worker",
    "calibration_refit_run": "calibration_refit_run_worker",
    "fda_event_harvest_daily": "fda_event_harvest_daily_worker",
    "bc_class_precedent_refresh": "bc_class_precedent_refresh_worker",
    "enrich_fda_asset_designations": "enrich_fda_asset_designations_worker",
}


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
                  "refit_min_n", "refit_bootstrap_resamples",
                  "category_cohort_days"):
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

    if action == "seed_fda_asset_aliases_refresh":
        # Fire-and-forget spawn of the alias-seed refresh. Pulls fresh
        # openFDA labels + ClinicalTrials.gov entries for every active
        # asset and idempotently upserts into fda_asset_aliases. Fired by
        # pg_cron job `v3-asset-alias-weekly-refresh` (Mon 03:00 UTC).
        # Zero LLM cost — only public HTTP APIs.
        fn = modal.Function.from_name(
            "conan-v3-orchestrator", "seed_fda_asset_aliases_refresh",
        )
        kwargs: Dict[str, Any] = {}
        if "sources" in args:
            kwargs["sources"] = args["sources"]
        handle = fn.spawn(**kwargs)
        return {"spawned": True, "function_call_id": handle.object_id}

    # Phase 3a/3b/4 spawn actions — all wire pg_cron → multiplex → worker.
    # Each accepts a small kwargs whitelist so pg_cron jobs can pass tuning
    # knobs (window_days, year, profile) without exposing the full worker API.
    if action == "earnings_calendar_fetch_daily":
        fn = modal.Function.from_name(
            "conan-v3-orchestrator", "phase3a_earnings_calendar_fetch_worker",
        )
        kwargs: Dict[str, Any] = {}
        for k in ("window_days", "forward_days", "tickers"):
            if k in args:
                kwargs[k] = args[k]
        handle = fn.spawn(**kwargs)
        return {"spawned": True, "function_call_id": handle.object_id}

    if action == "fomc_calendar_refresh":
        fn = modal.Function.from_name(
            "conan-v3-orchestrator", "phase3a_fomc_calendar_refresh_worker",
        )
        kwargs = {}
        if "year" in args:
            kwargs["year"] = args["year"]
        handle = fn.spawn(**kwargs)
        return {"spawned": True, "function_call_id": handle.object_id}

    if action == "q1_audit_run":
        fn = modal.Function.from_name(
            "conan-v3-orchestrator", "q1_audit_run_worker",
        )
        kwargs = {}
        if "re_audit" in args:
            kwargs["re_audit"] = bool(args["re_audit"])
        handle = fn.spawn(**kwargs)
        return {"spawned": True, "function_call_id": handle.object_id}

    if action == "q2_audit_run":
        fn = modal.Function.from_name(
            "conan-v3-orchestrator", "q2_audit_run_worker",
        )
        kwargs = {}
        if "profile" in args:
            kwargs["profile"] = args["profile"]
        handle = fn.spawn(**kwargs)
        return {"spawned": True, "function_call_id": handle.object_id}

    if action == "calibration_refit_run":
        fn = modal.Function.from_name(
            "conan-v3-orchestrator", "calibration_refit_run_worker",
        )
        kwargs = {}
        for k in ("min_n", "bootstrap_resamples", "enable_promotion",
                  "training_source"):
            if k in args:
                kwargs[k] = args[k]
        handle = fn.spawn(**kwargs)
        return {"spawned": True, "function_call_id": handle.object_id}

    if action == "fda_event_harvest_daily":
        fn = modal.Function.from_name(
            "conan-v3-orchestrator", "fda_event_harvest_daily_worker",
        )
        kwargs = {}
        for k in ("start_date", "end_date"):
            if k in args:
                kwargs[k] = args[k]
        handle = fn.spawn(**kwargs)
        return {"spawned": True, "function_call_id": handle.object_id}

    if action == "bc_class_precedent_refresh":
        fn = modal.Function.from_name(
            "conan-v3-orchestrator", "bc_class_precedent_refresh_worker",
        )
        kwargs = {}
        for k in ("lookback_years", "apply"):
            if k in args:
                kwargs[k] = args[k]
        handle = fn.spawn(**kwargs)
        return {"spawned": True, "function_call_id": handle.object_id}

    if action == "enrich_fda_asset_designations":
        fn = modal.Function.from_name(
            "conan-v3-orchestrator", "enrich_fda_asset_designations_worker",
        )
        kwargs = {}
        for k in ("stale_hours", "limit", "dry_run"):
            if k in args:
                kwargs[k] = args[k]
        handle = fn.spawn(**kwargs)
        return {"spawned": True, "function_call_id": handle.object_id}

    from modal_workers.shared.supabase_client import SupabaseClient
    from orchestrator_runtime.ic_memo_runner import run_ic_memo

    sb = SupabaseClient()
    # Only ic_memo_run remains as an inline (non-spawn) action — the Tier-2
    # tier2_bulk_enqueue / tier2_complete / tier2_fail actions were deleted
    # in v4 Phase 6b. Everything else routes via the spawn-only branches
    # above (feedback_loop_kickoff, orchestrator_drain_queue, calendar +
    # audit + harvest workers, seed_fda_asset_aliases_refresh).
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
