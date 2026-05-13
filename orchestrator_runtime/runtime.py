"""Orchestrator runtime — MVP single-stage assessment.

Reads (asset, extracted_facts, key documents, market context, prior memory)
and emits one `convergence_assessments` row.

This is a SIMPLIFIED v0.3 implementation of the plan's 10-stage pipeline. The
fully-built pipeline (Stages 0-10 with sub-agent dispatch + Citations API +
memory tool + isotonic calibration) is the next-iteration deliverable. v0.3
demonstrates the core synthesis loop with hypothesis-grounded reasoning
end-to-end on the VRDN / AXS-05 MVP.

What v0.3 includes:
  Stage 0  — load asset metadata + extracted_facts (no full memory hierarchy)
  Stage 1  — Sonnet synthesis (cited prose, fact_id-anchored)
  Stage 2  — hypothesis enumeration ({bull, base, bear} + kill_conditions)
  Stage 3  — adversarial pre-mortem (per-hypothesis verdict, cap on all_falsified)
  Stage 4  — reference-class anchoring (base rate + similar resolved cases)
  Stage 6  — Batch / streaming ensemble + dispersion (when ensemble_n > 1)
  Stage 7  — Sonnet constitutional pass with citation-resolution check
             (extended in v0.3 to walk Stage 2/3 citations)
  Stage 9  — Sonnet structured-output extraction → schema-validated JSON
             (post-hoc cap: conviction_pct ≤ 30 when Stage 3 returns all_falsified)
  Stage 10 — write convergence_assessments row + hypothesis_enumeration +
             premortem_assessments + post_mortem_queue stub

What v0.3 skips (next iteration):
  Stage 5   — Phase 5 sub-agents (literature / competitive / regulatory_history /
              options_microstructure) dispatched from Stage 1
  Stage 8   — isotonic calibration (curve-fitting math lives in
              modal_workers.shared.compute; no curve fitted yet —
              conviction_pct_calibrated == raw_conviction_pct until refit)

Run:
  ANTHROPIC_API_KEY=... SUPABASE_URL=... \\
    python3 -m orchestrator_runtime.runtime --asset-id <uuid> [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from modal_workers.shared.compute import (
    Stage4Anchor,
    apply_isotonic_calibration,
    build_stage_4_anchor,
    format_anchor_for_prompt,
    get_active_calibration_curve,
)
from modal_workers.shared.supabase_client import SupabaseClient
from orchestrator_runtime.client import (
    DEFAULT_EXTRACTOR_MODEL,
    DEFAULT_MODEL,
    OrchestratorClient,
    estimate_cost,
    parse_json_or_none,
)
from orchestrator_runtime.ensemble import (
    EnsembleResult,
    run_batch_ensemble,
    run_streaming_ensemble,
)
from orchestrator_runtime.constitutional import (
    SEMANTIC_SYSTEM_PROMPT,
    ConstitutionalResult,
    run_constitutional_check,
)
from orchestrator_runtime.memory import MemoryStore, MemoryBlobs
from orchestrator_runtime.sub_agent_dispatcher import (
    DISPATCH_TOOL_DEF,
    dispatch_sub_agent_tool,
    reset_budget as reset_sub_agent_budget,
)
from orchestrator_runtime.hypothesis import (
    STAGE_2_SYSTEM,
    HypothesisResult,
    renormalize_priors,
    run_hypothesis_enumeration,
)
from orchestrator_runtime.premortem import (
    STAGE_3_SYSTEM,
    PreMortemResult,
    run_premortem,
)

logger = logging.getLogger(__name__)

ORCHESTRATOR_VERSION = "orch-v0.4.0-mvp"


# ===========================================================================
# Abort-path exceptions
#
# Both are caught by modal_workers.orchestrator_app.drain_queue and converted
# to orchestrator_runs.status patches BEFORE the catch-all `except Exception`.
# Using dedicated types keeps these abort paths distinguishable in dashboards
# and prevents the silent "run_one returned None → status='completed' with
# assessment_id=null" misclassification that the old return-None contract
# allowed.
# ===========================================================================


class ConstitutionalFailure(Exception):
    """Raised when Stage 7 constitutional check returns pass_=False.

    D-117 requires structural Stage 2/3 errors and unresolved citations to
    gate the assessment. The catcher writes status='failed_constitutional'.
    No convergence_assessments row exists when this fires; partial cost is
    persisted via OrchestratorClient.get_accumulated_cost().
    """

    def __init__(self, findings: Optional[List[Any]] = None, message: str = ""):
        self.findings = findings or []
        super().__init__(
            message
            or f"constitutional check failed ({len(self.findings)} error finding(s))"
        )


class Stage9ParseError(Exception):
    """Raised when Stage 9 structured extraction returns unparseable JSON.

    Replaces the legacy `return None` contract — that path was silently
    classified as a successful run by orchestrator_app.drain_queue. The
    catcher writes status='failed' with error_message='stage_9_parse_error'.
    """


# Stream 3.6: Stage 1 sub-agent dispatch is feature-flagged. When ON, Stage 1
# runs an Anthropic tool-use loop with `dispatch_sub_agent` available. Default
# OFF to minimize risk of regressions; enable per-assessment via env or by
# passing `enable_sub_agents=True` to run_one().
ENABLE_SUB_AGENTS_DEFAULT = os.environ.get("ORCH_ENABLE_SUB_AGENTS") == "1"

# Phase 2B: when set, stage_1_rag_retrieve() injects local-corpus retrieval
# results into ctx before Stage 1. Off by default during ramp; flip to "1"
# once the RAG backfill (Phase 1A) is run against live data.
ENABLE_STAGE_1_RAG_DEFAULT = os.environ.get("ORCH_ENABLE_STAGE_1_RAG") == "1"
STAGE_1_RAG_K = int(os.environ.get("ORCH_STAGE_1_RAG_K", "8"))
SUB_AGENT_LOOP_MAX_TURNS = 4

# D-119: shared system prefix lifted from per-stage user content. All stages
# in one assessment send the same asset preamble + anchor + fact layer as the
# FIRST system block with cache_control: ephemeral. Subsequent calls within
# the 5-minute TTL hit cache at 10% input cost. Per-stage instructions go in
# the SECOND system block (after the cache marker), so they don't invalidate.
CACHEABLE_PREFIX_HEADER = (
    "# Shared assessment context (cached prefix)\n\n"
    "The blocks below are identical across every stage of this assessment. "
    "Treat them as fixed reference; per-stage instructions follow in the "
    "next system block.\n"
)

# Stage 9 post-hoc cap: when Stage 3 returns all_falsified, conviction_pct
# is forced to ≤ this ceiling. Plan §"D2: All-falsified handling".
# Wave 6.1 — env-overridable. Default 30.0 matches the legacy literal.
# Sane range is [10, 50]; values outside that are clamped at parse time.
ALL_FALSIFIED_CONVICTION_CEILING = max(
    0.0,
    min(100.0, float(os.environ.get("ORCH_ALL_FALSIFIED_CEILING", "30.0"))),
)

# Per-asset doc-buffer construction caps (keep below Tier-1 rate limit
# of 30k input tokens/min on the new API key). Env-overridable for A/B
# without code change — see plan-this-for-optimal-zippy-allen.md Wave 1.4.
MAX_FACTS_IN_PROMPT = int(os.environ.get("ORCH_MAX_FACTS_IN_PROMPT", "80"))
MAX_DOC_EXCERPTS = int(os.environ.get("ORCH_MAX_DOC_EXCERPTS", "8"))
DOC_EXCERPT_CHARS = int(os.environ.get("ORCH_DOC_EXCERPT_CHARS", "4000"))

# Number of prior non-superseded convergence_assessments to surface in the
# cached system prefix. Stage 1 sees them so it can ground "what did we
# previously think?" without a separate tool call.
MAX_PRIOR_ASSESSMENTS = int(os.environ.get("ORCH_MAX_PRIOR_ASSESSMENTS", "3"))

# Wave 10.2 — IC memo auto-trigger bar. After Stage 10 persists, if the
# calibrated conviction crosses this AND the band is operator-actionable,
# we automatically synthesize the IC memo. Set to 0.0 to disable the auto
# path entirely; set above 100 to force operator-only triggering.
IC_MEMO_AUTO_CONVICTION_THRESHOLD = max(
    0.0,
    min(101.0, float(
        os.environ.get("ORCH_IC_MEMO_CONVICTION_THRESHOLD", "75.0")
    )),
)
# Bands eligible for auto IC memo. 'archive' and 'discard' never trigger
# because the operator wouldn't act on those anyway.
IC_MEMO_AUTO_BANDS = frozenset({"immediate", "watchlist"})

# Wave 1.1 composite ranking — higher = more important; ties break by
# extraction_confidence DESC, then created_at DESC. The asset_documents
# CHECK constraint enumerates these five link_types.
LINK_TYPE_PRIORITY: Dict[str, int] = {
    "primary": 5,
    "safety_signal": 4,
    "pipeline_context": 3,
    "mentions": 2,
    "literature": 1,
}

# Band thresholds (derived from conviction_pct). Plan §"probabilistic +
# calibrated, not categorical" — these are configurable, not hardcoded
# inputs to the model.
BAND_THRESHOLDS = [
    (80.0, "immediate"),
    (60.0, "watchlist"),
    (40.0, "archive"),
    (0.0, "discard"),
]


@dataclass
class StageMetric:
    stage_name: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    status: str = "completed"
    notes: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AssessmentRun:
    asset_id: str
    trigger_type: str
    trigger_doc_id: Optional[str] = None
    # Wave 4 deep-fix Phase B.2 — idempotency key passed to persist_assessment_v3.
    # When set, a retried run resolves to the existing convergence_assessments
    # row instead of inserting a duplicate. None for ad-hoc CLI runs (which
    # aren't gated by orchestrator_runs and don't need retry-safety).
    orchestrator_run_id: Optional[str] = None
    document_window_start: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc) - timedelta(days=180))
    document_window_end: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc))
    stage_metrics: List[StageMetric] = field(default_factory=list)


# ===========================================================================
# Stage 0 — load context
# ===========================================================================

def stage_0_load(client: SupabaseClient, asset_id: str) -> Dict[str, Any]:
    """Load asset + facts + documents + market context."""
    asset_rows = client._rest(
        "GET", "fda_assets",
        params={
            "select": ("id,ticker,drug_name,generic_name,sponsor_name,indication,"
                       "indication_normalized,reference_class_signature,"
                       "application_number,application_type,program_status,"
                       "watch_priority"),
            "id": f"eq.{asset_id}",
        },
    ) or []
    if not asset_rows:
        raise ValueError(f"Asset {asset_id} not found")
    asset = asset_rows[0]

    # Pull extracted_facts (top by confidence, scoped to this asset)
    facts = client._rest(
        "GET", "extracted_facts",
        params={
            "select": ("id,document_id,fact_type,fact_text,evidence_quote,"
                       "citation_span,confidence,extracted_at"),
            "asset_id": f"eq.{asset_id}",
            "order": "confidence.desc.nullslast,extracted_at.desc",
            "limit": str(MAX_FACTS_IN_PROMPT),
        },
    ) or []

    # Pull material documents linked to this asset, then rank by
    # link_type priority → extraction_confidence → created_at (Wave 1.1).
    # Pure recency-ordering (the prior implementation) let recent boilerplate
    # displace older 'primary' filings, starving Stage 1 of high-signal text.
    asset_docs = client._rest(
        "GET", "asset_documents",
        params={
            "select": ("document_id,link_type,extraction_confidence,"
                       "extracted_spans,created_at"),
            "asset_id": f"eq.{asset_id}",
            "is_material": "is.true",
            "order": "created_at.desc",
        },
    ) or []

    def _doc_rank_key(row: Dict[str, Any]) -> tuple:
        # Sorted ascending → best first. ISO-8601 strings sort
        # lexicographically; placing negative ord-1 makes newer-first,
        # but it's simpler to pre-flip the asset_docs list (already
        # ordered newest-first by PostgREST) then use Python's stable
        # sort with just the two non-temporal keys.
        link_pri = LINK_TYPE_PRIORITY.get(row.get("link_type") or "", 0)
        conf = row.get("extraction_confidence")
        try:
            conf_f = float(conf) if conf is not None else 0.0
        except (TypeError, ValueError):
            conf_f = 0.0
        return (-link_pri, -conf_f)

    # PostgREST already returned newest-first; Python's sort is stable, so
    # rows that tie on (link_priority, confidence) retain that recency order.
    asset_docs.sort(key=_doc_rank_key)
    doc_ids = [r["document_id"] for r in asset_docs[:MAX_DOC_EXCERPTS]]

    docs: List[Dict[str, Any]] = []
    if doc_ids:
        ids_filter = ",".join(doc_ids)
        rows = client._rest(
            "GET", "documents",
            params={
                # Stream 3.3: include anthropic_file_id + is_pdf so Stage 1 can
                # emit native Citations-API document blocks where available.
                "select": ("id,source,doc_type,title,url,published_at,raw_text,"
                           "extensions,anthropic_file_id,is_pdf"),
                "id": f"in.({ids_filter})",
            },
        ) or []
        # Preserve order from asset_docs (newest first)
        by_id = {r["id"]: r for r in rows}
        docs = [by_id[did] for did in doc_ids if did in by_id]

    # Stream 3.4: hierarchical memory — parallel reads of asset + indication +
    # reviewer_panel + sub_agent scopes from the memory_files index. Falls back
    # to the legacy single asset.memory_path read when the new path is empty
    # (eases backfill — old assets without an entry still load OK).
    memory_store = MemoryStore(client)
    sub_agent_key = (
        f"summary/{asset_id}" if asset_id else None
    )
    memory_blobs = memory_store.load_all(
        asset_id=asset_id,
        indication=asset.get("indication_normalized") or asset.get("indication"),
        reviewer_panel_id=asset.get("reviewer_panel_id"),
        sub_agent_key=sub_agent_key,
    )
    memory_text = memory_blobs.as_text() if not memory_blobs.is_empty() else None
    if memory_text is None:
        legacy_path = asset.get("memory_path")
        if legacy_path:
            # Wave 1.3 — narrow from blanket Exception. Real failure modes here
            # are storage-not-found (treat as empty), permission/UTF-8 decoding;
            # everything else (e.g. an SDK bug) should surface, not get buried.
            try:
                blob = client.read_cache("memory", legacy_path.lstrip("/"))
                if blob:
                    memory_text = blob.decode("utf-8", errors="replace")
            except (IOError, KeyError, UnicodeDecodeError) as exc:
                logger.debug("legacy memory %s not found: %s", legacy_path, exc)

    # Wave 1.2 — surface the most recent N convergence_assessments for this
    # asset so Stage 1's cached prefix carries "what did we previously
    # conclude?" context. Only non-superseded rows; ordered newest-first.
    prior_assessments = client._rest(
        "GET", "convergence_assessments",
        params={
            "select": ("created_at,thesis_direction,conviction_pct,"
                       "conviction_pct_calibrated,band,evidence_quality,"
                       "pre_mortem_verdict"),
            "asset_id": f"eq.{asset_id}",
            "superseded_at": "is.null",
            "order": "created_at.desc",
            "limit": str(MAX_PRIOR_ASSESSMENTS),
        },
    ) or []

    return {
        "asset": asset,
        "facts": facts,
        "documents": docs,
        "memory_text": memory_text,
        "memory_blobs": memory_blobs,
        "asset_doc_links": asset_docs,
        "prior_assessments": prior_assessments,
        "reference_class_anchor": None,  # populated by stage_4_anchor
    }


# ===========================================================================
# Stage 4 — reference-class anchoring
# ===========================================================================

def stage_4_anchor(
    sb: SupabaseClient,
    ctx: Dict[str, Any],
) -> tuple[Stage4Anchor, StageMetric]:
    """Look up the empirical base rate + similar resolved cases for the
    asset's reference_class_signature. Result is attached to ctx so the
    Stage 1 prompt builder can render an anchor section, and threaded
    into Stage 7 / Stage 10 downstream.
    """
    t0 = time.monotonic()
    asset = ctx["asset"]
    reference_class = asset.get("reference_class_signature")
    anchor = build_stage_4_anchor(
        sb,
        reference_class=reference_class,
        exclude_asset_id=asset.get("id"),
    )
    ctx["reference_class_anchor"] = anchor

    # Wave 7 — surface data-quality signals so SQL audits can quantify how
    # often the anchor is degraded. We deliberately do NOT emit operator_flags
    # here: today 34/35 active assets have a null reference_class_signature,
    # so per-assessment flags would be pure noise. The note fields below let
    # a single dashboard query (FILTER WHERE notes->>'missing_reference_class')
    # surface the same fact, once.
    missing_ref_class = not bool(reference_class)
    n_similar = len(anchor.similar_cases)
    sparse_similar = (n_similar < 5)   # Wave 7.2

    # Wave 7.3 — base-rate refit staleness. Today there are 0 base_rate rows
    # so this stays False; once nightly refits populate, an anchor older than
    # 30 days surfaces as stale on every assessment that uses it.
    refit_at_iso: Optional[str] = (
        anchor.base_rate.refit_at if anchor.base_rate else None)
    refit_stale_30d = False
    if refit_at_iso:
        try:
            refit_dt = datetime.fromisoformat(refit_at_iso.replace("Z", "+00:00"))
            refit_stale_30d = (datetime.now(timezone.utc) - refit_dt) > timedelta(days=30)
        except (TypeError, ValueError):
            # Bad ISO string — treat as stale to force operator attention.
            refit_stale_30d = True

    metric = StageMetric(
        stage_name="stage_4_reference_class_anchor",
        model="deterministic",
        latency_ms=int((time.monotonic() - t0) * 1000),
        notes={
            "reference_class": reference_class,
            "has_base_rate": anchor.base_rate is not None,
            "n_similar_cases": n_similar,
            "n_cases_in_class": (anchor.base_rate.n_cases
                                 if anchor.base_rate else None),
            "approval_rate_pct": (round(anchor.base_rate.as_pct(), 2)
                                  if anchor.base_rate else None),
            # Wave 7 telemetry
            "missing_reference_class": missing_ref_class,
            "sparse_similar_cases": sparse_similar,
            "refit_at": refit_at_iso,
            "refit_stale_30d": refit_stale_30d,
        },
    )
    return anchor, metric


# ===========================================================================
# Stage 1 RAG retrieval (Phase 2B) — runs between Stage 4 and Stage 1
# ===========================================================================

def stage_1_rag_retrieve(
    sb: SupabaseClient,
    ctx: Dict[str, Any],
    *,
    k: int = STAGE_1_RAG_K,
    asset_scoped: bool = False,
) -> StageMetric:
    """Retrieve top-k chunks from the local RAG corpus and store them in
    ``ctx["rag_chunks"]`` for ``_build_stage_1_user_content`` to render.

    The retrieved chunks land in the user message (NOT the cached system
    prefix) so retrieval drift between runs does not bust the asset-level
    cache. Per D-119, the system prefix only contains things that are
    deterministic given the asset.

    Query construction: indication + drug_name (best heuristic seed for the
    high-recall retrieval pass; the model can then iterate via the sub-agent
    `internal_rag_hybrid_search` tool for narrower follow-up queries).

    Cold-start safe: if the corpus is empty, hybrid_search returns [] and the
    user content omits the retrieved-context section entirely.
    """
    asset = ctx.get("asset") or {}
    drug = (asset.get("drug_name") or "").strip()
    indication = (
        asset.get("indication_normalized") or asset.get("indication") or ""
    ).strip()
    query = " ".join(filter(None, [indication, drug])) or asset.get("ticker")
    if not query:
        ctx["rag_chunks"] = []
        return StageMetric(
            stage_name="stage_1_rag_retrieve",
            model="rag",
            input_tokens=0, output_tokens=0,
            cost_usd=0.0, latency_ms=0,
            notes={"skipped": "no_query"},
        )

    t0 = time.time()
    try:
        from orchestrator_runtime import rag_handle
        chunks = rag_handle.hybrid_search(
            sb, query,
            corpus="all",
            k=k,
            asset_id=asset.get("id") if asset_scoped else None,
        )
    except Exception as exc:  # noqa: BLE001
        # RAG is best-effort. If the corpus or RPCs aren't ready, log and
        # emit an empty list so Stage 1 falls through to the legacy path.
        logger.warning("stage_1_rag_retrieve: %s — degrading to no RAG", exc)
        chunks = []

    ctx["rag_chunks"] = chunks
    return StageMetric(
        stage_name="stage_1_rag_retrieve",
        model="rag",
        input_tokens=0, output_tokens=0,
        cost_usd=0.0,
        latency_ms=int((time.time() - t0) * 1000),
        notes={
            "n_chunks": len(chunks), "k": k,
            "asset_scoped": asset_scoped, "query": query[:200],
        },
    )


# ===========================================================================
# Stage 1 — Sonnet synthesis
# ===========================================================================

STAGE_1_SYSTEM = """You are an FDA-event analyst producing an investment thesis on \
one tracked drug asset. You synthesize from a structured fact layer + raw \
document excerpts + (when available) prior assessment memory.

Your output is CITED PROSE — every material claim references a fact_id from \
the structured layer (in [F:<fact_id_short>] notation, e.g. [F:abc123]) or a \
document_id (in [D:<doc_id_short>]). Uncited claims will be rejected by the \
constitutional check.

Required output structure (verbatim section headers, in this order):

## Asset summary
2-3 sentences identifying the asset, indication, and current regulatory state.

## Catalyst landscape
The pending catalyst (PDUFA date, AdComm, readout, etc.) and what's known \
about it. Cite specific facts.

## Evidence for approval / positive direction
Bullet list. Each bullet cites the specific fact(s) that support it.

## Evidence for CRL / negative direction
Bullet list. Each bullet cites contradicting facts. If you cannot find \
contrary evidence, say "no contrary evidence found in the document set" \
explicitly.

## Key uncertainties
Bullet list of open questions where the evidence is ambiguous. Each \
uncertainty: what's unknown, why it matters, what would resolve it.

## Reasoning trace
3-5 sentences walking through how you weighted the evidence to reach your \
direction + conviction.

## Conclusion
- thesis_direction: long | short | neutral | straddle
- conviction_pct: 0-100 (probability your direction is correct)
- evidence_quality: 0.0-1.0 (how confident are you in the underlying \
evidence base — separate from direction)

Direction calibration:
  long: expect approval / positive market move
  short: expect CRL / negative market move
  neutral: outcome is too uncertain to take directional position
  straddle: expect large move but cannot determine direction; bet on \
volatility

Conviction calibration:
  90+: strong consensus across multiple primary sources, no material \
contradicting evidence
  70-89: clear lean, minor uncertainties manageable
  50-69: meaningful uncertainty, lean is plausible but contestable
  30-49: highly uncertain, lean is weak
  <30: should be 'neutral' — don't force a direction"""


def stage_1_synthesize(
    a_client: OrchestratorClient,
    ctx: Dict[str, Any],
    model: str,
    *,
    enable_sub_agents: bool = ENABLE_SUB_AGENTS_DEFAULT,
    assessment_id: Optional[str] = None,
) -> tuple[str, StageMetric]:
    """Stage 1 synthesis. Single-shot by default.

    Stream 3.6: when `enable_sub_agents=True`, Claude is given the
    `dispatch_sub_agent` tool and the call enters a tool-use loop. Claude can
    issue parallel tool calls for literature / competitive / regulatory_history /
    options_microstructure within one assistant turn; results land back as
    tool_result blocks and Claude continues until it produces final cited
    prose.
    """
    user_content = _build_stage_1_user_content(ctx)
    facts = ctx["facts"]
    docs = ctx["documents"]
    system_blocks = build_system_blocks(
        build_shared_system_prefix(ctx), STAGE_1_SYSTEM,
        static_prefix=build_static_prefix(ctx),
    )

    # Stream 3.3: prefer native Citations API content blocks when any document
    # in ctx has been uploaded to Anthropic Files API; falls back to the
    # text-only user_content otherwise.
    has_file_ids = any(d.get("anthropic_file_id") for d in docs)
    user_payload: Any = (
        _build_stage_1_user_content_blocks(ctx) if has_file_ids else user_content
    )

    if not enable_sub_agents:
        result = a_client.call(
            system=system_blocks,
            messages=[{"role": "user", "content": user_payload}],
            model=model,
            max_tokens=4096,
        )
        metric = StageMetric(
            stage_name="stage_1_synthesis",
            model=result.model,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            thinking_tokens=result.thinking_tokens,
            cache_read_tokens=result.cache_read_tokens,
            cache_creation_tokens=result.cache_creation_tokens,
            cost_usd=result.cost_usd,
            latency_ms=result.latency_ms,
            notes={"n_facts": len(facts), "n_docs": len(docs)},
        )
        return result.text, metric

    # Tool-use loop variant
    return _stage_1_synthesize_with_dispatch(
        a_client, ctx, model, system_blocks, user_payload, assessment_id,
    )


def _stage_1_synthesize_with_dispatch(
    a_client: OrchestratorClient,
    ctx: Dict[str, Any],
    model: str,
    system_blocks: List[Dict[str, Any]],
    user_payload: Any,
    assessment_id: Optional[str],
) -> tuple[str, StageMetric]:
    from modal_workers.sub_agents.runtime import _block_to_dict as _b2d

    reset_sub_agent_budget()
    asset_context = {
        "asset_id": ctx["asset"]["id"],
        "ticker": ctx["asset"].get("ticker"),
        "drug_name": ctx["asset"].get("drug_name"),
        "indication": ctx["asset"].get("indication"),
        "reference_class": ctx["asset"].get("reference_class_signature"),
    }
    if isinstance(user_payload, str):
        initial_content: Any = [{"type": "text", "text": user_payload}]
    else:
        initial_content = user_payload  # already a list of blocks
    messages: List[Dict[str, Any]] = [
        {"role": "user", "content": initial_content}
    ]

    total_in = total_out = 0
    total_thinking = 0
    total_cache_read = total_cache_create = 0
    total_cost = 0.0
    total_latency = 0
    dispatch_log: List[Dict[str, Any]] = []
    final_text = ""

    # Wave 9.1 — config_overrides["sub_agent_max_turns"] lets an operator-refresh
    # (or future per-asset config) raise/lower the dispatch loop ceiling without
    # a redeploy. Coerced to int, clamped to [1, 16] to keep accidental typos
    # from running away.
    cfg = ctx.get("config_overrides") or {}
    try:
        max_turns = int(cfg.get("sub_agent_max_turns", SUB_AGENT_LOOP_MAX_TURNS))
    except (TypeError, ValueError):
        max_turns = SUB_AGENT_LOOP_MAX_TURNS
    max_turns = max(1, min(16, max_turns))
    if max_turns != SUB_AGENT_LOOP_MAX_TURNS:
        logger.info(
            "Stage 1 sub-agent loop: max_turns=%d (overridden from default %d)",
            max_turns, SUB_AGENT_LOOP_MAX_TURNS,
        )

    for turn in range(max_turns):
        result = a_client.call(
            system=system_blocks,
            messages=messages,
            model=model,
            max_tokens=4096,
            tools=[DISPATCH_TOOL_DEF],
        )
        total_in += result.input_tokens
        total_out += result.output_tokens
        total_thinking += result.thinking_tokens
        total_cache_read += result.cache_read_tokens
        total_cache_create += result.cache_creation_tokens
        total_cost += result.cost_usd
        total_latency += result.latency_ms

        msg = result.raw_message
        if msg is None:
            final_text = result.text
            break

        messages.append({
            "role": "assistant",
            "content": [_b2d(b) for b in msg.content],
        })

        stop_reason = getattr(msg, "stop_reason", "end_turn")
        if stop_reason != "tool_use":
            final_text = result.text
            break

        tool_results: List[Dict[str, Any]] = []
        for block in msg.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            if block.name != "dispatch_sub_agent":
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps({"error": f"unknown tool {block.name}"}),
                    "is_error": True,
                })
                continue
            inp = dict(block.input or {})
            dispatch_log.append({"role": inp.get("role"), "question": inp.get("question"),
                                 "turn": turn})
            try:
                out = dispatch_sub_agent_tool(
                    inp, asset_context=asset_context, assessment_id=assessment_id,
                )
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(out, default=str)[:50000],
                })
            except Exception as exc:  # noqa: BLE001
                logger.warning("Stage 1 dispatch failed: %s", exc)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps({"error": str(exc)}),
                    "is_error": True,
                })

        messages.append({"role": "user", "content": tool_results})
    else:
        logger.warning("Stage 1 hit SUB_AGENT_LOOP_MAX_TURNS=%d without end_turn",
                       SUB_AGENT_LOOP_MAX_TURNS)

    metric = StageMetric(
        stage_name="stage_1_synthesis",
        model=model,
        input_tokens=total_in,
        output_tokens=total_out,
        thinking_tokens=total_thinking,
        cache_read_tokens=total_cache_read,
        cache_creation_tokens=total_cache_create,
        cost_usd=total_cost,
        latency_ms=total_latency,
        notes={
            "n_facts": len(ctx["facts"]),
            "n_docs": len(ctx["documents"]),
            "sub_agent_dispatches": dispatch_log,
            "loop_turns": turn + 1,
        },
    )
    return final_text, metric


# ===========================================================================
# Stage 9 — extraction (structured outputs)
# ===========================================================================

STAGE_9_SYSTEM = """You convert a cited-prose investment thesis into a strict \
JSON object matching the schema below. Do not add commentary; emit JSON only.

Schema:
{
  "thesis_direction": "long" | "short" | "neutral" | "straddle",
  "conviction_pct": <number 0-100>,
  "evidence_quality": <number 0.0-1.0>,
  "thesis_summary": "<1-3 sentence summary>",
  "key_facts": [
    {"text": "<short claim>", "fact_id_short": "<8-char id from [F:...] cite>"}
  ],
  "uncertainties": [
    {"question": "<what's unknown>", "why_matters": "<short>", "how_to_resolve": "<short>"}
  ],
  "cited_prose_blocks": [
    {"section": "<section header>", "text": "<paragraph>", "fact_citations": ["<8-char id>"], "doc_citations": ["<8-char id>"]}
  ],
  "reasoning_summary": "<2-3 sentence reasoning trace>"
}

Rules:
- thesis_direction MUST be one of the four values
- conviction_pct + evidence_quality MUST be numeric (no strings)
- key_facts: 5-15 items, each grounded in a [F:...] cite from the prose
- uncertainties: 2-5 items
- cited_prose_blocks: one per section header in the prose; preserve all \
[F:...] / [D:...] cites you find
- Output ONLY the JSON object — no markdown fences, no commentary"""


STAGE_9_RETRY_REMINDER = (
    "\n\n## IMPORTANT — RETRY\n\n"
    "Your previous response was not valid JSON. Emit ONLY a JSON object "
    "matching the schema above. Do not include markdown fences, prose "
    "commentary, or any text outside the JSON braces. Start with `{` and "
    "end with `}`."
)


def stage_9_extract(
    a_client: OrchestratorClient,
    cited_prose: str,
    model: str,
) -> tuple[Optional[Dict[str, Any]], StageMetric]:
    """Wave 5.1 — one retry on JSON parse failure. The retry call appends an
    explicit "emit valid JSON" reminder and keeps the same cited prose. Both
    calls are tallied into the StageMetric so cost accounting is honest.
    """
    user_content = f"Cited prose to extract:\n\n{cited_prose}"
    result = a_client.call(
        system=STAGE_9_SYSTEM,
        messages=[{"role": "user", "content": user_content}],
        model=model,
        max_tokens=8192,
    )
    parsed = parse_json_or_none(result.text)

    # Aggregate counters — start with the first call
    agg_input = result.input_tokens
    agg_output = result.output_tokens
    agg_thinking = result.thinking_tokens
    agg_cache_read = result.cache_read_tokens
    agg_cache_create = result.cache_creation_tokens
    agg_cost = result.cost_usd
    agg_latency = result.latency_ms
    n_attempts = 1
    retry_succeeded = False

    if not parsed:
        # First-shot parse failed — try once more with an explicit reminder.
        # Cited prose is unchanged; only the user message gets the reminder
        # appended. STAGE_9_SYSTEM is unchanged so the cached prefix still
        # hits.
        logger.warning(
            "Stage 9 first-shot JSON parse failed; retrying with explicit "
            "JSON reminder (output_tokens=%d)",
            result.output_tokens,
        )
        retry_content = user_content + STAGE_9_RETRY_REMINDER
        retry_result = a_client.call(
            system=STAGE_9_SYSTEM,
            messages=[{"role": "user", "content": retry_content}],
            model=model,
            max_tokens=8192,
        )
        parsed = parse_json_or_none(retry_result.text)
        agg_input += retry_result.input_tokens
        agg_output += retry_result.output_tokens
        agg_thinking += retry_result.thinking_tokens
        agg_cache_read += retry_result.cache_read_tokens
        agg_cache_create += retry_result.cache_creation_tokens
        agg_cost += retry_result.cost_usd
        agg_latency += retry_result.latency_ms
        n_attempts = 2
        retry_succeeded = bool(parsed)
        logger.info(
            "Stage 9 retry %s (output_tokens=%d)",
            "succeeded" if retry_succeeded else "ALSO failed",
            retry_result.output_tokens,
        )

    metric = StageMetric(
        stage_name="stage_9_extraction",
        model=result.model,
        input_tokens=agg_input,
        output_tokens=agg_output,
        thinking_tokens=agg_thinking,
        cache_read_tokens=agg_cache_read,
        cache_creation_tokens=agg_cache_create,
        cost_usd=agg_cost,
        latency_ms=agg_latency,
        status="completed" if parsed else "failed",
        notes={
            "parsed": bool(parsed),
            "n_attempts": n_attempts,
            "retry_succeeded": retry_succeeded,
        },
    )
    return parsed, metric


# ===========================================================================
# Stage 10 — writeback
# ===========================================================================

def derive_band(conviction_pct: float) -> str:
    for thresh, band in BAND_THRESHOLDS:
        if conviction_pct >= thresh:
            return band
    return "discard"


def stage_10_persist(
    sb: SupabaseClient,
    asset_id: str,
    run: AssessmentRun,
    cited_prose: str,
    parsed: Dict[str, Any],
    ctx: Dict[str, Any],
    model: str,
    extractor_model: str,
    ensemble_payload: Optional[Dict[str, Any]] = None,
    constitutional_result: Optional[ConstitutionalResult] = None,
    hypothesis_result: Optional[HypothesisResult] = None,
    premortem_result: Optional[PreMortemResult] = None,
) -> str:
    fact_ids = [f["id"] for f in ctx["facts"]]
    document_ids = [d["id"] for d in ctx["documents"]]

    # Resolve short fact_ids back to full UUIDs
    short_to_full = {f["id"][:8]: f["id"] for f in ctx["facts"]}
    short_to_full_doc = {d["id"][:8]: d["id"] for d in ctx["documents"]}

    # Wave 5.2 — track unresolved short-ids during hydration. Previously the
    # `dict.get(s, s)` fallback silently kept the 8-char short id, producing
    # dangling references in cited_prose_blocks and orphan key_facts.fact_id
    # entries. Now we drop the citation cleanly and accumulate the short-ids
    # into stage_10_unhydrated_short_ids for assessment_stage_metrics.notes.
    unhydrated_fact_shorts: set[str] = set()
    unhydrated_doc_shorts: set[str] = set()

    def _hydrate_fact(short: str) -> Optional[str]:
        full = short_to_full.get(short)
        if full is None:
            unhydrated_fact_shorts.add(short)
        return full

    def _hydrate_doc(short: str) -> Optional[str]:
        full = short_to_full_doc.get(short)
        if full is None:
            unhydrated_doc_shorts.add(short)
        return full

    # Hydrate citations in cited_prose_blocks. Drop unresolved entries cleanly
    # so downstream consumers don't see 8-char shorts where UUIDs are expected.
    hydrated_blocks = []
    for blk in (parsed.get("cited_prose_blocks") or []):
        fact_uuids = [
            uuid for uuid in (_hydrate_fact(s) for s in (blk.get("fact_citations") or []))
            if uuid is not None
        ]
        doc_uuids = [
            uuid for uuid in (_hydrate_doc(s) for s in (blk.get("doc_citations") or []))
            if uuid is not None
        ]
        hydrated_blocks.append({
            "section": blk.get("section"),
            "text": blk.get("text"),
            "fact_citations": fact_uuids,
            "doc_citations": doc_uuids,
        })

    hydrated_key_facts = []
    for kf in (parsed.get("key_facts") or []):
        short = kf.get("fact_id_short", "") or ""
        full = _hydrate_fact(short) if short else None
        if full is None:
            # Skip key_facts whose short-id doesn't resolve to a real
            # extracted_facts row — writing the short string into the
            # fact_id column would fail FK validation anyway.
            continue
        hydrated_key_facts.append({
            "text": kf.get("text"),
            "fact_id": full,
        })

    if unhydrated_fact_shorts or unhydrated_doc_shorts:
        logger.warning(
            "Stage 10 hydration: %d unresolved fact shorts, %d unresolved doc "
            "shorts; citations dropped: facts=%s docs=%s",
            len(unhydrated_fact_shorts), len(unhydrated_doc_shorts),
            sorted(unhydrated_fact_shorts), sorted(unhydrated_doc_shorts),
        )

    # Wave 5.2 — emit a stage_10_hydration metric row so the dropped-citation
    # signal lands in assessment_stage_metrics where audits can pick it up.
    # Status="completed" if no drops, "degraded" if any. Tokens/cost 0 (no
    # LLM call here) but the notes carry the diagnostic shorts.
    run.stage_metrics.append(StageMetric(
        stage_name="stage_10_hydration",
        model="deterministic",
        status="degraded" if (unhydrated_fact_shorts or unhydrated_doc_shorts)
                else "completed",
        notes={
            "n_unhydrated_fact_shorts": len(unhydrated_fact_shorts),
            "n_unhydrated_doc_shorts": len(unhydrated_doc_shorts),
            "unhydrated_fact_shorts": sorted(unhydrated_fact_shorts),
            "unhydrated_doc_shorts": sorted(unhydrated_doc_shorts),
            "n_blocks_persisted": len(hydrated_blocks),
            "n_key_facts_persisted": len(hydrated_key_facts),
        },
    ))

    conviction = float(parsed.get("conviction_pct") or 50.0)
    conviction = max(0.0, min(100.0, conviction))
    # D-117: when Stage 3 capped the conviction, raw_conviction_pct should
    # record the pre-cap (Stage 5/6) value, not the capped one.
    pre_cap_conviction = ctx.get("pre_premortem_conviction")
    if pre_cap_conviction is not None:
        try:
            raw_conviction = max(0.0, min(100.0, float(pre_cap_conviction)))
        except (TypeError, ValueError):
            raw_conviction = conviction
    else:
        raw_conviction = conviction
    direction = parsed.get("thesis_direction") or "neutral"
    if direction not in {"long", "short", "neutral", "straddle"}:
        direction = "neutral"
    evidence_quality = parsed.get("evidence_quality")
    try:
        evidence_quality = float(evidence_quality) if evidence_quality is not None else None
    except (TypeError, ValueError):
        evidence_quality = None
    if evidence_quality is not None:
        evidence_quality = max(0.0, min(1.0, evidence_quality))

    # Stage 8 — isotonic calibration if a curve is active.
    # `conviction` is the RAW model conviction here (B4 fix: the Stage 3
    # all_falsified branch no longer mutates parsed["conviction_pct"];
    # instead it sets ctx["conviction_capped_by_premortem"] and stages
    # the raw value on ctx["pre_premortem_conviction"]).
    #
    # Wave 8.1 — calibration_status disambiguates "why is curve_version NULL?"
    # at audit time. Enum: applied | no_active_curve | no_curve_data.
    active_curve = get_active_calibration_curve(sb)
    calibration_curve_version: Optional[str] = None
    if active_curve and active_curve.get("curve_data"):
        calibrated = apply_isotonic_calibration(
            conviction / 100.0, active_curve["curve_data"]) * 100.0
        calibrated = max(0.0, min(100.0, calibrated))
        calibration_curve_version = active_curve.get("version")
        calibration_status = "applied"
    elif active_curve:
        # Row exists but no usable curve_data — degenerate state worth flagging.
        calibrated = conviction
        calibration_status = "no_curve_data"
    else:
        calibrated = conviction
        calibration_status = "no_active_curve"

    # B4: apply the Stage 3 all_falsified cap AFTER calibration. Capping
    # before calibration distorted conviction_pct_calibrated for the
    # all_falsified case: it produced isotonic(min(raw, 30)/100) instead of
    # the correct min(isotonic(raw/100), 30). The raw value still flows
    # into raw_conviction_pct via ctx["pre_premortem_conviction"] so the
    # feedback loop sees the un-capped model output (D-117 invariant).
    if ctx.get("conviction_capped_by_premortem"):
        pre_cap_calibrated = calibrated
        calibrated = min(calibrated, ALL_FALSIFIED_CONVICTION_CEILING)
        if calibrated < pre_cap_calibrated:
            logger.info(
                "Stage 10: all_falsified cap applied AFTER calibration "
                "(%.2f -> %.2f)", pre_cap_calibrated, calibrated,
            )

    band = derive_band(calibrated)

    # Stage 4 anchor (populated upstream; safe-degrade to Nones if absent)
    anchor: Optional[Stage4Anchor] = ctx.get("reference_class_anchor")
    reference_class_value: Optional[str] = (
        anchor.reference_class if anchor and anchor.reference_class else None)
    base_rate_value: Optional[float] = (
        anchor.base_rate.approval_rate if anchor and anchor.base_rate else None)
    similar_case_ids: List[str] = (
        [c.eval_harness_id for c in anchor.similar_cases]
        if anchor and anchor.similar_cases else [])

    total_input = sum(m.input_tokens for m in run.stage_metrics)
    total_output = sum(m.output_tokens for m in run.stage_metrics)
    total_thinking = sum(m.thinking_tokens for m in run.stage_metrics)
    total_cache_read = sum(m.cache_read_tokens for m in run.stage_metrics)
    total_cache_create = sum(m.cache_creation_tokens for m in run.stage_metrics)
    total_cost = sum(m.cost_usd for m in run.stage_metrics)
    total_latency = sum(m.latency_ms for m in run.stage_metrics)

    # Stage 2/3 denormalized payloads for the convergence_assessments row
    # (structured per-hypothesis rows live in hypothesis_enumeration +
    # premortem_assessments).
    hypotheses_summary: Optional[List[Dict[str, Any]]] = None
    pre_mortem_summary: Optional[str] = None
    adversarial_summary: Optional[List[Dict[str, Any]]] = None
    pre_mortem_verdict_value: Optional[str] = None
    surviving_ids_value: List[str] = []
    if hypothesis_result is not None:
        hypotheses_summary = [
            {
                "hypothesis_id": h.hypothesis_id,
                "label": h.label,
                "claim": h.claim,
                "direction": h.direction,
                "kill_conditions": h.kill_conditions,
                "prior_estimate_pct": h.prior_estimate_pct,
            }
            for h in hypothesis_result.hypotheses
        ]
    if premortem_result is not None:
        pre_mortem_verdict_value = premortem_result.overall_verdict
        surviving_ids_value = list(premortem_result.surviving_hypothesis_ids)
        adversarial_summary = [
            {
                "hypothesis_id": v.hypothesis_id,
                "verdict": v.verdict,
                "n_failure_modes": len(v.failure_modes),
                "kill_count": sum(1 for fm in v.failure_modes if fm.severity == "kill"),
                "weaken_count": sum(1 for fm in v.failure_modes if fm.severity == "weaken"),
                "tail_count": sum(1 for fm in v.failure_modes if fm.severity == "tail"),
            }
            for v in premortem_result.verdicts
        ]
        # Plain-text pre_mortem narrative for dashboard rendering.
        # D-120: cap each failure-mode line at 500 chars and the total at
        # 8000 to avoid pathological "1MB pre_mortem text" rows.
        lines: List[str] = [f"Overall verdict: {premortem_result.overall_verdict}"]
        if surviving_ids_value:
            lines.append(f"Surviving: {', '.join(surviving_ids_value)}")
        for v in premortem_result.verdicts:
            lines.append(f"\n[{v.hypothesis_id}] {v.verdict}")
            for fm in v.failure_modes:
                tag = "[spec]" if fm.speculative else ""
                line = f"  - ({fm.severity}){tag} {fm.description}"
                lines.append(line[:500])
        pre_mortem_summary = "\n".join(lines)[:8000]
    elif hypothesis_result is not None:
        pre_mortem_verdict_value = "skipped"

    row = {
        "asset_id": asset_id,
        "orchestrator_version": ORCHESTRATOR_VERSION,
        "model_id": model,
        "trigger_type": run.trigger_type,
        "trigger_doc_id": run.trigger_doc_id,
        "document_window_start": run.document_window_start.isoformat(),
        "document_window_end": run.document_window_end.isoformat(),
        "document_ids": document_ids,
        "fact_ids": fact_ids,
        "evidence_ledger": {
            "n_facts": len(fact_ids),
            "n_documents": len(document_ids),
            "fact_types_covered": sorted(set(f["fact_type"] for f in ctx["facts"])),
            "conviction_capped_by_premortem": bool(
                ctx.get("conviction_capped_by_premortem", False)),
        },
        "reasoning_trace": cited_prose,
        "cited_prose_blocks": hydrated_blocks,
        "key_facts": hydrated_key_facts,
        "uncertainties": parsed.get("uncertainties") or [],
        "raw_conviction_pct": raw_conviction,
        "thesis_direction": direction,
        "thesis_summary": parsed.get("thesis_summary") or "",
        "ensemble_n": (ensemble_payload or {}).get("n", 1),
        "ensemble_runs": (ensemble_payload or {}).get("runs"),
        "ensemble_mean": (ensemble_payload or {}).get("raw_mean", conviction),
        "ensemble_dispersion": (ensemble_payload or {}).get("dispersion", 0.0),
        "shrinkage_factor": (ensemble_payload or {}).get("shrinkage_factor", 0.0),
        "constitutional_pass": (
            constitutional_result.pass_ if constitutional_result else None),
        "constitutional_findings": (
            [{"severity": f.severity, "check": f.check, "detail": f.detail,
              "affected_id": f.affected_id}
             for f in constitutional_result.findings]
            if constitutional_result else None),
        "hypotheses": hypotheses_summary,
        "pre_mortem": pre_mortem_summary,
        "adversarial_challenges": adversarial_summary,
        "pre_mortem_verdict": pre_mortem_verdict_value,
        "surviving_hypothesis_ids": surviving_ids_value,
        "reference_class": reference_class_value,
        "reference_class_base_rate": (
            round(base_rate_value, 3) if base_rate_value is not None else None),
        "similar_resolved_case_ids": similar_case_ids or None,
        "conviction_pct_calibrated": round(calibrated, 2),
        "calibration_curve_version": calibration_curve_version,
        "calibration_status": calibration_status,   # Wave 8.1
        "conviction_pct": round(calibrated, 2),
        "evidence_quality": evidence_quality,
        "band": band,
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_thinking_tokens": total_thinking,
        "total_cache_read_tokens": total_cache_read,
        "total_cache_creation_tokens": total_cache_create,
        "cost_usd": round(total_cost, 4),
        "latency_ms": total_latency,
    }

    # Wave 4 deep-fix Phase B — single atomic RPC. Replaces the prior
    # "INSERT parent + INSERT secondaries + DELETE-on-failure" two-call dance.
    # Idempotency: re-driving the same orchestrator_run_id returns the
    # existing assessment id without inserting a duplicate. Supersedence:
    # the RPC stamps superseded_at on any prior live assessment for this
    # asset and writes superseded_by to point back at the new row.
    secondaries = _build_stage_10_secondaries(
        run, ctx, parsed, hypothesis_result, premortem_result, short_to_full,
    )

    # Wave 4 deep-fix Phase C — catalyst lookup. Done by the caller (not the
    # pure payload builder) because it requires the sb client. Result stamps
    # the post_mortem_stub fields the RPC will INSERT.
    outcome_window_end, catalyst_marker = _resolve_catalyst_window(sb, asset_id)
    secondaries["post_mortem_stub"]["outcome_window_end"] = (
        outcome_window_end.isoformat()
    )
    secondaries["post_mortem_stub"]["catalyst_resolution_marker"] = catalyst_marker

    rpc_payload = {
        "payload": {
            "orchestrator_run_id": run.orchestrator_run_id,
            "assessment": row,
            "stage_metrics": secondaries["stage_metrics"],
            "hypotheses": secondaries["hypotheses"],
            "premortem_verdicts": secondaries["premortem_verdicts"],
            "post_mortem_stub": secondaries["post_mortem_stub"],
        }
    }
    rpc_result = sb._rest(
        "POST", "rpc/persist_assessment_v3",
        json_body=rpc_payload,
    )
    # PostgREST returns the scalar uuid directly. Some clients wrap it in a
    # list, some return the bare string — handle both shapes defensively.
    if isinstance(rpc_result, list):
        rpc_result = rpc_result[0] if rpc_result else None
    if isinstance(rpc_result, dict):
        # If PostgREST decides to return a named-column object, the function
        # signature names the return column 'persist_assessment_v3'.
        rpc_result = (
            rpc_result.get("persist_assessment_v3")
            or rpc_result.get("uuid")
            or next(iter(rpc_result.values()), None)
        )
    if not rpc_result:
        raise RuntimeError(
            "persist_assessment_v3 returned no assessment id "
            f"(orchestrator_run_id={run.orchestrator_run_id})"
        )
    assessment_id = str(rpc_result)

    # Wave 4.2 — memory writeback runs OUTSIDE the rollback scope. A storage
    # failure here MUST NOT delete the parent assessment; the memory blob is
    # a read optimization, not part of the correctness contract.
    _write_asset_memory_best_effort(
        sb,
        asset_id=asset_id,
        assessment_id=assessment_id,
        asset=ctx["asset"],
        parsed=parsed,
        cited_prose=cited_prose,
        calibrated=calibrated,
        band=band,
        direction=direction,
    )

    # Wave 10.3 — supersede the IC memo attached to the prior live assessment
    # for this asset (if any). The supersedence chain on
    # convergence_assessments is stamped elsewhere; here we just keep the
    # memo's `superseded_at` in sync so the dashboard's "live memo" surface
    # stays accurate without re-walking the parent chain.
    _supersede_prior_ic_memo_best_effort(
        sb, asset_id=asset_id, new_assessment_id=assessment_id,
    )

    # Wave 10.2 — auto-trigger IC memo synthesis on high-conviction
    # assessments. Today (2026-05-13) the IC memo runner has fired 0 times
    # in 30d because nothing automatically invokes it. Bar: calibrated
    # conviction >= ORCH_IC_MEMO_CONVICTION_THRESHOLD AND band in {immediate,
    # watchlist}. Best-effort: a memo failure is logged but doesn't impact
    # the assessment's success — the operator can manually re-trigger via
    # the dashboard.
    _maybe_trigger_ic_memo_best_effort(
        sb,
        assessment_id=assessment_id,
        calibrated=calibrated,
        band=band,
    )

    return assessment_id


# Wave 4 deep-fix Phase C — event types eligible for catalyst-resolution marker.
# Pdufa was the original (Wave 4.3). Expanded here to cover the rest of the
# FDA-event-driven post-mortem window classes. Order doesn't matter; the SQL
# ORDER BY event_date.asc.nullslast picks the soonest one regardless.
CATALYST_EVENT_TYPES = ("pdufa", "advisory_committee", "eop2", "readout")

# Days back from today to consider "just-resolved" events. Without this,
# a PDUFA that resolved last week would silently fall through to the
# default_<N>d_fallback even though the catalyst signal is still meaningful.
CATALYST_LOOKBACK_DAYS = int(
    os.environ.get("ORCH_CATALYST_LOOKBACK_DAYS", "30")
)

# Default outcome window when no eligible catalyst event is on file. The +60d
# default was hardcoded in the original Wave 4.3 path; Phase C makes it env-
# tunable so different asset types (smol approvals vs ASCO readouts vs AdComm
# meetings) can be re-windowed without a redeploy.
DEFAULT_POST_MORTEM_WINDOW_DAYS = int(
    os.environ.get("ORCH_DEFAULT_POST_MORTEM_WINDOW_DAYS", "60")
)


def _resolve_catalyst_window(
    sb: SupabaseClient, asset_id: str,
) -> tuple[datetime, str]:
    """Wave 4 Phase C.1 — pick the soonest eligible FDA catalyst (pending OR
    recently-resolved-within-CATALYST_LOOKBACK_DAYS, across all
    CATALYST_EVENT_TYPES). Returns the (outcome_window_end, marker) pair.

    Marker grammar: ``<event_type>:<event_id>`` for catalyst-anchored windows;
    ``default_<N>d_fallback`` when nothing matches. The nightly calibration
    refit uses this string to weight (or exclude) default-window stubs.
    """
    lookback_cutoff = (
        datetime.now(timezone.utc).date() - timedelta(days=CATALYST_LOOKBACK_DAYS)
    ).isoformat()
    types_filter = ",".join(CATALYST_EVENT_TYPES)
    rows = sb._rest(
        "GET", "fda_regulatory_events",
        params={
            "select": "id,event_date,event_type,event_status",
            "asset_id": f"eq.{asset_id}",
            "event_type": f"in.({types_filter})",
            "event_status": "in.(pending,resolved)",
            "event_date": f"gte.{lookback_cutoff}",
            "order": "event_date.asc.nullslast",
            "limit": "1",
        },
    ) or []
    if rows and rows[0].get("event_date"):
        evt = rows[0]
        outcome_window_end = (
            datetime.fromisoformat(evt["event_date"]).replace(tzinfo=timezone.utc)
            + timedelta(days=2)
        )
        marker = f"{evt['event_type']}:{evt['id']}"
        return outcome_window_end, marker
    outcome_window_end = (
        datetime.now(timezone.utc)
        + timedelta(days=DEFAULT_POST_MORTEM_WINDOW_DAYS)
    )
    marker = f"default_{DEFAULT_POST_MORTEM_WINDOW_DAYS}d_fallback"
    return outcome_window_end, marker


def _build_stage_10_secondaries(
    run: AssessmentRun,
    ctx: Dict[str, Any],
    parsed: Dict[str, Any],
    hypothesis_result: Optional[HypothesisResult],
    premortem_result: Optional[PreMortemResult],
    short_to_full: Dict[str, str],
) -> Dict[str, Any]:
    """Wave 4 deep-fix Phase B — pure payload builder for the
    persist_assessment_v3 RPC. Replaces _stage_10_write_secondaries (which
    did its own POSTs). Output dict has four arrays + one stub object,
    matching the RPC's expected payload shape.

    No DB writes happen here. All effectful work is downstream in the RPC.
    """
    # Per-stage metrics payload (assessment_id is filled in by the RPC).
    stage_metrics_payload = [
        {
            "stage_name": m.stage_name,
            "model": m.model,
            "input_tokens": m.input_tokens,
            "output_tokens": m.output_tokens,
            "thinking_tokens": m.thinking_tokens,
            "cache_read_tokens": m.cache_read_tokens,
            "cache_creation_tokens": m.cache_creation_tokens,
            "cost_usd": round(m.cost_usd, 4),
            "latency_ms": m.latency_ms,
            "status": m.status,
            "notes": m.notes,
        }
        for m in run.stage_metrics
    ]

    # Stage 2 hypothesis_enumeration payload. The model cites by 8-char short
    # id; resolve to full UUIDs for the uuid[] columns. Unresolved shorts get
    # dropped here (matches the Wave 5.2 cited-prose hydration policy).
    hypotheses_payload: List[Dict[str, Any]] = []
    if hypothesis_result is not None and hypothesis_result.hypotheses:
        for h in hypothesis_result.hypotheses:
            supporting_uuids = [
                short_to_full[s.lower()]
                for s in h.supporting_fact_ids
                if s.lower() in short_to_full
            ]
            contradicting_uuids = [
                short_to_full[s.lower()]
                for s in h.contradicting_fact_ids
                if s.lower() in short_to_full
            ]
            hypotheses_payload.append({
                "hypothesis_id": h.hypothesis_id,
                "label": h.label,
                "claim": h.claim,
                "mechanism": h.mechanism,
                "direction": h.direction,
                "supporting_fact_ids": supporting_uuids,
                "contradicting_fact_ids": contradicting_uuids,
                "kill_conditions": h.kill_conditions,
                "prior_estimate_pct": h.prior_estimate_pct,
                "prior_estimate_pct_pre_anchor": h.prior_estimate_pct_pre_anchor,
            })

    # Stage 3 premortem_assessments payload.
    premortem_payload: List[Dict[str, Any]] = []
    if premortem_result is not None and premortem_result.verdicts:
        for v in premortem_result.verdicts:
            failure_modes_jsonb = [
                {
                    "description": fm.description,
                    "severity": fm.severity,
                    "evidence_fact_ids": fm.evidence_fact_ids,
                    "speculative": fm.speculative,
                }
                for fm in v.failure_modes
            ]
            premortem_payload.append({
                "hypothesis_id": v.hypothesis_id,
                "verdict": v.verdict,
                "failure_modes": failure_modes_jsonb,
                "disconfirming_searches": v.disconfirming_searches,
                "update_triggers": v.update_triggers,
            })

    # Post-mortem queue stub. NOTE: this still does a SELECT against
    # fda_regulatory_events because the catalyst lookup is purely a read
    # (no transactional dependency on the parent INSERT). It's safe to run
    # outside the RPC scope.
    direction = parsed.get("thesis_direction") or "neutral"
    if direction not in {"long", "short", "neutral", "straddle"}:
        direction = "neutral"
    conviction = float(parsed.get("conviction_pct") or 50.0)
    conviction = max(0.0, min(100.0, conviction))
    # Phase C resolves the catalyst window. Done by the caller (which has
    # the sb client). We build the stub WITHOUT the catalyst lookup here
    # so this function stays pure; the caller stamps the resolved fields
    # before passing to the RPC.
    post_mortem_stub = {
        "asset_id": run.asset_id,
        "predicted_outcome": _direction_to_outcome(direction),
        "predicted_conviction_pct": conviction,
        "predicted_direction": direction,
        # Caller fills these from _resolve_catalyst_window():
        #   "outcome_window_end": <iso>,
        #   "catalyst_resolution_marker": <text>,
    }

    return {
        "stage_metrics": stage_metrics_payload,
        "hypotheses": hypotheses_payload,
        "premortem_verdicts": premortem_payload,
        "post_mortem_stub": post_mortem_stub,
    }


def _write_asset_memory_best_effort(
    sb: SupabaseClient,
    *,
    asset_id: str,
    assessment_id: str,
    asset: Dict[str, Any],
    parsed: Dict[str, Any],
    cited_prose: str,
    calibrated: float,
    band: str,
    direction: str,
) -> None:
    """Wave 4.2 — memory writeback runs OUTSIDE the Wave 4.1 rollback scope.

    A storage write failure must NOT delete the parent assessment (memory
    blob is a Stage-0 read optimization, not part of the correctness
    contract). Failures emit a warn operator_flag so a degraded loop becomes
    operator-discoverable instead of silently rotting.

    D-123 C5: distilled blob = Stage 9 reasoning_summary + headline metadata
    + recent-assessments append (idempotent on assessment_id, capped at
    RECENT_ASSESSMENTS_CAP).
    """
    try:
        store = MemoryStore(sb)
        prior_blobs = store.load_all(asset_id=asset_id)
        prior_text = (prior_blobs.asset or "") if prior_blobs else ""
        memory_summary = _build_asset_memory_summary(
            asset=asset,
            parsed=parsed,
            cited_prose=cited_prose,
            conviction_calibrated=calibrated,
            band=band,
            direction=direction,
            assessment_id=assessment_id,
            prior_text=prior_text,
        )
        store.write(scope="asset", scope_id=asset_id, content=memory_summary)
    except Exception as exc:  # noqa: BLE001
        logger.warning("memory writeback failed for asset=%s: %s", asset_id, exc)
        try:
            sb._rest(
                "POST", "operator_flags",
                json_body={
                    "severity": "warn",
                    "source": "memory_writeback",
                    "kind": "asset_memory_write_failed",
                    "title": (
                        f"Memory writeback failed for asset {asset_id[:8]}"
                    ),
                    "body": str(exc)[:1000],
                    "evidence": {
                        "asset_id": asset_id,
                        "assessment_id": assessment_id,
                    },
                },
            )
        except Exception as flag_exc:  # noqa: BLE001
            logger.warning(
                "memory writeback operator_flag emit failed: %s", flag_exc,
            )


def _supersede_prior_ic_memo_best_effort(
    sb: SupabaseClient,
    *,
    asset_id: str,
    new_assessment_id: str,
) -> None:
    """Wave 10.3 — when a new assessment lands for an asset, mark any
    not-yet-superseded IC memo that was attached to a prior (now stale)
    assessment as `superseded_at = now()`.

    The dashboard surfaces this so the operator sees "stale memo — refresh
    from the live assessment". Pure observability bookkeeping; never
    mutates the memo contents.

    Best-effort: a stamp failure here is logged but never blocks the
    parent assessment from returning.
    """
    try:
        # Find any LIVE IC memo (sub_agent_calls.superseded_at IS NULL,
        # role='ic_memo') whose assessment is on this asset and is NOT the
        # one we just wrote. One PATCH covers all of them — there should
        # typically be at most one, but we don't enforce it here.
        prior_memos = sb._rest(
            "GET", "sub_agent_calls",
            params={
                "select": "id,assessment_id",
                "role": "eq.ic_memo",
                "superseded_at": "is.null",
                "assessment_id": f"neq.{new_assessment_id}",
            },
        ) or []
        if not prior_memos:
            return
        # Filter to memos on assessments belonging to THIS asset. We need
        # the join because sub_agent_calls doesn't carry asset_id directly.
        prior_ids = [m["assessment_id"] for m in prior_memos]
        in_filter = ",".join(prior_ids)
        same_asset_rows = sb._rest(
            "GET", "convergence_assessments",
            params={
                "select": "id",
                "id": f"in.({in_filter})",
                "asset_id": f"eq.{asset_id}",
            },
        ) or []
        same_asset_ids = {r["id"] for r in same_asset_rows}
        targets = [
            m["id"] for m in prior_memos
            if m["assessment_id"] in same_asset_ids
        ]
        if not targets:
            return
        sb._rest(
            "PATCH", "sub_agent_calls",
            params={"id": f"in.({','.join(targets)})"},
            json_body={
                "superseded_at": datetime.now(timezone.utc).isoformat(),
            },
            prefer="return=minimal",
        )
        logger.info(
            "ic_memo: marked %d prior memo(s) superseded for asset %s",
            len(targets), asset_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "supersede_prior_ic_memo failed for asset=%s: %s", asset_id, exc,
        )


def _maybe_trigger_ic_memo_best_effort(
    sb: SupabaseClient,
    *,
    assessment_id: str,
    calibrated: float,
    band: str,
) -> None:
    """Wave 10.2 — fire the IC memo synthesis automatically when an
    assessment is high-conviction AND operator-actionable.

    Bar: `calibrated >= IC_MEMO_AUTO_CONVICTION_THRESHOLD` (env-tunable,
    default 75) AND `band in IC_MEMO_AUTO_BANDS` ({immediate, watchlist}).

    Synchronous + best-effort. Synchronous because the IC memo cost
    (~1-3 specialist Sonnet calls) is bounded and the dashboard wants
    the memo visible alongside the assessment without a separate poll.
    Best-effort because: (a) IC memo failures should NOT mark the
    assessment as failed — the assessment is correct, only its memo is
    missing; (b) operator can always manually re-trigger via the
    dashboard or `compute_v3 ic_memo_run` endpoint.

    On success, also stamps `convergence_assessments.ic_memo_call_id`
    so the dashboard `v_assessment_with_ic_memo` view joins cleanly.
    """
    if calibrated < IC_MEMO_AUTO_CONVICTION_THRESHOLD:
        return
    if band not in IC_MEMO_AUTO_BANDS:
        return

    # Lazy import to avoid pulling the ic_memo_runner module (which imports
    # sub_agents.runtime → mcp schemas) into orchestrator_runtime hot path
    # for assessments that never trigger a memo.
    try:
        from orchestrator_runtime.ic_memo_runner import (
            ICMemoOrchestrationError,
            run_ic_memo,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "ic_memo auto-trigger: failed to import runner (%s); skipping. "
            "Assessment %s still persists.",
            exc, assessment_id,
        )
        return

    try:
        logger.info(
            "ic_memo auto-trigger: assessment=%s conviction=%.1f band=%s",
            assessment_id, calibrated, band,
        )
        result = run_ic_memo(sb, assessment_id, persist=True)
        sub_agent_call_id = result.get("sub_agent_call_id")
        if sub_agent_call_id:
            # Stamp the FK on the parent assessment row.
            sb._rest(
                "PATCH", "convergence_assessments",
                params={"id": f"eq.{assessment_id}"},
                json_body={"ic_memo_call_id": sub_agent_call_id},
                prefer="return=minimal",
            )
            logger.info(
                "ic_memo auto-trigger: persisted call_id=%s cost=$%.3f",
                sub_agent_call_id, result.get("cost_usd", 0.0),
            )
    except ICMemoOrchestrationError as exc:
        # Expected failure mode: assessment has 0 specialists (e.g. cold
        # start, sub-agent dispatch disabled). Don't escalate; log + move on.
        logger.info(
            "ic_memo auto-trigger: skipped (%s) for assessment %s",
            exc, assessment_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "ic_memo auto-trigger: failed for assessment %s: %s",
            assessment_id, exc,
        )


RECENT_ASSESSMENTS_CAP = 5


def _parse_recent_assessments(prior_text: str) -> List[str]:
    """Extract the bullet entries inside `## Recent assessments` from a
    prior memory file. Returns the list of bullet lines (without the leading
    '- '). Idempotent: if the section is missing, returns []."""
    if not prior_text:
        return []
    lines = prior_text.splitlines()
    out: List[str] = []
    in_section = False
    for ln in lines:
        if ln.strip().startswith("## "):
            if in_section:
                break  # next section started
            if ln.strip().lower() == "## recent assessments":
                in_section = True
            continue
        if not in_section:
            continue
        if ln.strip().startswith("- "):
            out.append(ln.strip()[2:])
    return out


def _build_asset_memory_summary(
    *,
    asset: Dict[str, Any],
    parsed: Dict[str, Any],
    cited_prose: str,
    conviction_calibrated: float,
    band: str,
    direction: str,
    assessment_id: str,
    prior_text: str = "",
) -> str:
    """Compact asset-scope memory blob written by Stage 10.

    D-123 Contract C5: `## Recent assessments` is append-only newest-first,
    idempotent on assessment_id (re-running the same assessment doesn't
    duplicate the entry), capped at RECENT_ASSESSMENTS_CAP.
    """
    reasoning = (parsed.get("reasoning_summary") or "")[:1200]
    uncertainties = parsed.get("uncertainties") or []
    unc_lines = [
        f"- {u.get('question', '')[:200]}"
        for u in uncertainties[:5]
        if isinstance(u, dict)
    ]
    timestamp = datetime.now(timezone.utc).isoformat()

    # Append-merge ## Recent assessments — newest first, dedupe by id.
    new_entry = (
        f"{timestamp[:19].replace('T', ' ')}Z · "
        f"id={assessment_id[:8]} · band={band} · dir={direction} · "
        f"conv={conviction_calibrated:.1f}"
    )
    prior = _parse_recent_assessments(prior_text)
    # Idempotency marker: assessment_id substring.
    seen_id = f"id={assessment_id[:8]}"
    deduped = [e for e in prior if seen_id not in e]
    merged = [new_entry] + deduped
    merged = merged[:RECENT_ASSESSMENTS_CAP]
    recent_lines = "\n".join(f"- {e}" for e in merged)

    return (
        f"# Asset memory — {asset.get('drug_name') or asset.get('ticker') or asset.get('id')}\n\n"
        f"_Last updated: {timestamp}_\n\n"
        f"- last_assessment_id: {assessment_id}\n"
        f"- last_band: {band}\n"
        f"- last_direction: {direction}\n"
        f"- last_conviction_calibrated: {conviction_calibrated:.1f}\n"
        f"- indication: {asset.get('indication') or '(unknown)'}\n"
        f"- reference_class: {asset.get('reference_class_signature') or '(unknown)'}\n\n"
        f"## Reasoning summary\n\n{reasoning}\n\n"
        f"## Open uncertainties (top 5)\n\n"
        + ("\n".join(unc_lines) if unc_lines else "_(none recorded)_")
        + f"\n\n## Recent assessments\n\n{recent_lines}\n"
    )


def _direction_to_outcome(direction: str) -> str:
    return {
        "long": "approved",
        "short": "crl",
        "neutral": "no_strong_outcome",
        "straddle": "any_large_move",
    }.get(direction, "no_strong_outcome")


# ===========================================================================
# Main
# ===========================================================================

def build_shared_system_prefix(ctx: Dict[str, Any]) -> str:
    """D-119: cacheable shared content sent as the first system block of every
    stage in an assessment. Identical bytes across Stage 1/2/3/7 so the prefix
    cache hits on calls 2..N within the 5-minute TTL.

    Contains: asset metadata, Stage 4 anchor (when present), and the full
    structured fact layer. Per-stage instructions (STAGE_X_SYSTEM strings) go
    in the second system block AFTER the cache marker, so they can differ
    without invalidating the cached prefix.
    """
    asset = ctx["asset"]
    facts = ctx["facts"]
    facts_section = "\n".join(
        f"- F:{f['id'][:8]} ({f['fact_type']}, conf={f.get('confidence')}, "
        f"doc=D:{f['document_id'][:8]}): {f['fact_text']}\n"
        f"  evidence: \"{f['evidence_quote'][:300]}\""
        for f in facts
    )
    anchor = ctx.get("reference_class_anchor")
    anchor_block = format_anchor_for_prompt(anchor) if anchor is not None else None
    anchor_section = (f"\n## Reference-class anchor\n\n{anchor_block}\n\n"
                      if anchor_block else "")

    # Wave 1.2 — render prior assessments (most recent non-superseded N).
    # Empty for cold-start assets; section header omitted in that case so
    # we don't waste a cache line on "(none)".
    prior = ctx.get("prior_assessments") or []
    if prior:
        prior_lines = []
        for row in prior:
            day = (row.get("created_at") or "")[:10]
            direction = row.get("thesis_direction") or "?"
            conv = row.get("conviction_pct_calibrated")
            if conv is None:
                conv = row.get("conviction_pct")
            conv_str = f"{float(conv):.0f}%" if conv is not None else "?"
            band = row.get("band") or "?"
            ev = row.get("evidence_quality")
            ev_str = f"{float(ev):.2f}" if ev is not None else "?"
            verdict = row.get("pre_mortem_verdict") or "?"
            prior_lines.append(
                f"- {day}: {direction}, conviction {conv_str} "
                f"(band={band}, evidence={ev_str}, pre_mortem={verdict})"
            )
        prior_section = (
            "\n## Prior assessments (most recent {n}, non-superseded)\n\n"
            "{lines}\n\n"
        ).format(n=len(prior_lines), lines="\n".join(prior_lines))
    else:
        prior_section = ""

    return f"""{CACHEABLE_PREFIX_HEADER}
## Tracked asset

  asset_id: {asset['id']}
  ticker: {asset.get('ticker')}
  drug_name: {asset.get('drug_name')}
  generic_name: {asset.get('generic_name') or '(unknown)'}
  sponsor_name: {asset.get('sponsor_name')}
  indication: {asset.get('indication')}
  indication_normalized: {asset.get('indication_normalized') or '(unknown)'}
  reference_class: {asset.get('reference_class_signature') or '(unknown)'}
  application_number: {asset.get('application_number') or '(unknown)'}
  program_status: {asset.get('program_status') or '(unknown)'}
{anchor_section}{prior_section}## Structured fact layer ({len(facts)} facts, ranked by confidence then \
recency)

{facts_section}"""


def build_static_prefix(ctx: Dict[str, Any]) -> Optional[str]:
    """Stream 3.5 — 1h-TTL system block A.

    Holds content that is invariant across many assessments of the same asset
    or indication: the loaded memory hierarchy (asset / indication / reviewer
    panel / sub-agent scopes). Returns None when memory is empty so callers
    skip the block (avoids paying cache-creation tokens for empty content).
    """
    blobs: Optional[MemoryBlobs] = ctx.get("memory_blobs")
    if blobs is None or blobs.is_empty():
        return None
    return f"## Memory hierarchy (static)\n\n{blobs.as_text()}\n"


def build_system_blocks(
    shared_prefix: str,
    stage_system: str,
    *,
    static_prefix: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Construct system blocks with mixed TTL caching (Stream 3.5).

    Layout when static_prefix is supplied:
      [block A: static_prefix, ttl=1h]   ← memory hierarchy + future taxonomy
      [block B: shared_prefix, ttl=5m]   ← per-asset facts + Stage 4 anchor
      [block C: stage_system, no cache]  ← per-stage instructions

    Layout when static_prefix is None (back-compat for callers that don't load
    memory — eg. eval_harness fixtures):
      [block A: shared_prefix, ttl=5m]
      [block B: stage_system, no cache]

    The 5m TTL is implicit (no `ttl` key) — Anthropic's default ephemeral TTL.
    """
    blocks: List[Dict[str, Any]] = []
    if static_prefix:
        blocks.append({
            "type": "text",
            "text": static_prefix,
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        })
    blocks.append({
        "type": "text",
        "text": shared_prefix,
        "cache_control": {"type": "ephemeral"},
    })
    blocks.append({"type": "text", "text": stage_system})
    return blocks


def _build_stage_1_user_content(ctx: Dict[str, Any]) -> str:
    """Stage 1 user content — the dynamic part only (docs + memory + produce
    instruction). The asset preamble, anchor, and structured fact layer have
    moved to the cached system prefix per D-119.

    Reused by single-shot Stage 1 + ensemble.

    Stream 3.3 note: when at least one document has an `anthropic_file_id`,
    callers should prefer `_build_stage_1_user_content_blocks` which emits
    native Citations-API document blocks. This text-only variant remains the
    fallback for documents that haven't been uploaded.
    """
    docs = ctx["documents"]
    memory_text = ctx["memory_text"]
    rag_chunks = ctx.get("rag_chunks") or []

    docs_section_parts = []
    for d in docs:
        text = d.get("raw_text") or ""
        excerpt = (text[:DOC_EXCERPT_CHARS] +
                   ("\n[…trim…]\n" if len(text) > DOC_EXCERPT_CHARS else ""))
        docs_section_parts.append(
            f"### D:{d['id'][:8]} — {d['source']}/{d['doc_type']} — "
            f"{d.get('title') or '(untitled)'} — {d.get('published_at')}\n"
            f"{excerpt}"
        )
    docs_section = "\n\n".join(docs_section_parts)
    memory_section = (f"\n\n## Prior assessment memory\n\n{memory_text}\n"
                      if memory_text else "")

    rag_section = ""
    if rag_chunks:
        from orchestrator_runtime import rag_handle
        rendered = rag_handle.format_chunks_for_prompt(rag_chunks)
        rag_section = (
            f"\n## Retrieved context ({len(rag_chunks)} chunks from local "
            f"primary-source corpus — cite via [D:<doc>] or [C:<chunk>])\n\n"
            f"{rendered}\n"
        )

    return f"""Document window: last 180 days (most recent {len(docs)} material \
documents shown below; full set has more)
{rag_section}
## Document excerpts ({len(docs)} documents, head-only excerpts)

{docs_section}{memory_section}

Produce the cited prose synthesis per the system prompt. End with the \
Conclusion section in the exact format specified."""


def _build_stage_1_user_content_blocks(ctx: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Stream 3.3 — Citations-API user content.

    For each document that has been uploaded to Anthropic Files API
    (documents.anthropic_file_id IS NOT NULL), emit a native document block
    with `citations: {enabled: true}`. Documents without an uploaded file_id
    keep the legacy text-excerpt path inside a single text block.

    Returns a list of blocks suitable for `messages[0].content`.

    The native citations metadata that comes back in Claude's response is
    walked by Stage 7's constitutional check (`extract_native_citations`).
    [F:short] / [D:short] notation remains supported as a fallback for facts
    (which aren't documents) and for documents that aren't uploaded yet.
    """
    docs = ctx["documents"]
    memory_text = ctx.get("memory_text")

    blocks: List[Dict[str, Any]] = []
    fallback_doc_parts: List[str] = []

    for d in docs:
        file_id = d.get("anthropic_file_id")
        title = d.get("title") or f"D:{d['id'][:8]} — {d['source']}/{d['doc_type']}"
        if file_id:
            blocks.append({
                "type": "document",
                "source": {"type": "file", "file_id": file_id},
                "title": title[:255],
                "context": (
                    f"doc_id={d['id'][:8]} source={d['source']} "
                    f"doc_type={d['doc_type']} published_at={d.get('published_at')}"
                ),
                "citations": {"enabled": True},
            })
        else:
            text = d.get("raw_text") or ""
            excerpt = (text[:DOC_EXCERPT_CHARS] +
                       ("\n[…trim…]\n" if len(text) > DOC_EXCERPT_CHARS else ""))
            fallback_doc_parts.append(
                f"### D:{d['id'][:8]} — {d['source']}/{d['doc_type']} — "
                f"{title} — {d.get('published_at')}\n"
                f"{excerpt}"
            )

    fallback_section = "\n\n".join(fallback_doc_parts)
    memory_section = (
        f"\n\n## Prior assessment memory\n\n{memory_text}\n"
        if memory_text else ""
    )
    text_payload = (
        f"Document window: last 180 days "
        f"(documents with native Citations API: {len(blocks)}; "
        f"documents shown as text excerpt below: {len(fallback_doc_parts)})\n\n"
    )
    if fallback_section:
        text_payload += (
            f"## Document excerpts (fallback for un-uploaded docs)\n\n"
            f"{fallback_section}"
        )
    text_payload += memory_section
    text_payload += (
        "\n\nProduce the cited prose synthesis per the system prompt. End "
        "with the Conclusion section in the exact format specified."
    )
    blocks.append({"type": "text", "text": text_payload})
    return blocks


def run_one(sb: SupabaseClient, a_client: OrchestratorClient,
            asset_id: str, trigger_type: str = "manual",
            model: str = DEFAULT_MODEL,
            extractor_model: str = DEFAULT_EXTRACTOR_MODEL,
            ensemble_n: int = 1,
            ensemble_mode: str = "streaming",     # streaming | batch
            run_constitutional: bool = True,
            constitutional_skip_semantic: bool = False,
            enable_premortem: bool = True,
            dry_run: bool = False,
            run_id: Optional[str] = None,
            hard_kill_usd: Optional[float] = 15.0,
            parsed_out: Optional[Dict[str, Any]] = None,
            config_overrides: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Build one convergence_assessments row.

    run_id + hard_kill_usd activate the per-run cost ceiling (Stream 6
    step 4). When set, OrchestratorClient.call() raises BudgetExceededError
    once cumulative cost exceeds hard_kill_usd; the caller (drain_queue)
    converts that into status='killed_budget' on orchestrator_runs.
    Pass hard_kill_usd=None to disable the kill switch (useful for
    backtests / one-off CLI runs).

    Phase 4A (D-127): pass a mutable dict via `parsed_out` to receive the
    Stage 9 parsed payload (`thesis_direction`, `conviction_pct`,
    `evidence_quality`, etc.) before persistence. The replay harness uses
    this to convert a `dry_run=True` invocation into a ReplayOutput
    without touching the DB.
    """
    if hard_kill_usd is not None:
        a_client.attach_budget(run_id, hard_kill_usd)
    try:
        return _run_one_inner(
            sb, a_client, asset_id, trigger_type, model, extractor_model,
            ensemble_n, ensemble_mode, run_constitutional,
            constitutional_skip_semantic, enable_premortem, dry_run,
            parsed_out, config_overrides, run_id=run_id,
        )
    finally:
        if hard_kill_usd is not None:
            a_client.detach_budget()


def _run_one_inner(sb: SupabaseClient, a_client: OrchestratorClient,
                   asset_id: str, trigger_type: str,
                   model: str, extractor_model: str,
                   ensemble_n: int, ensemble_mode: str,
                   run_constitutional: bool,
                   constitutional_skip_semantic: bool,
                   enable_premortem: bool,
                   dry_run: bool,
                   parsed_out: Optional[Dict[str, Any]] = None,
                   config_overrides: Optional[Dict[str, Any]] = None,
                   run_id: Optional[str] = None) -> Optional[str]:
    run = AssessmentRun(
        asset_id=asset_id,
        trigger_type=trigger_type,
        orchestrator_run_id=run_id,  # Wave 4 Phase B — idempotency key
    )

    logger.info("=== Stage 0: load context ===")
    ctx = stage_0_load(sb, asset_id)
    # Wave 9.1 — stash config_overrides on ctx so downstream stages
    # (Stage 1 dispatch loop, future Stage 3/4 knobs) can read them
    # without needing the param threaded individually.
    ctx["config_overrides"] = dict(config_overrides or {})
    asset = ctx["asset"]
    logger.info("Asset: %s / %s (%s, %s); facts=%d, docs=%d",
                asset.get("ticker"), asset.get("drug_name"),
                asset.get("indication"), asset.get("application_number") or "no_app#",
                len(ctx["facts"]), len(ctx["documents"]))

    logger.info("=== Stage 4: reference-class anchor ===")
    anchor, m4 = stage_4_anchor(sb, ctx)
    run.stage_metrics.append(m4)

    if ENABLE_STAGE_1_RAG_DEFAULT:
        logger.info("=== Stage 1 RAG retrieve (k=%d) ===", STAGE_1_RAG_K)
        m_rag = stage_1_rag_retrieve(sb, ctx, k=STAGE_1_RAG_K)
        run.stage_metrics.append(m_rag)
        logger.info(
            "Stage 1 RAG: %d chunks", len(ctx.get("rag_chunks") or []),
        )

    if anchor.has_signal:
        br = anchor.base_rate
        logger.info(
            "Stage 4: class=%s base_rate=%s n=%s similar=%d",
            anchor.reference_class,
            (f"{br.as_pct():.1f}%" if br else "n/a"),
            (br.n_cases if br else "n/a"),
            len(anchor.similar_cases),
        )
    else:
        logger.info("Stage 4: no anchor signal for class=%s",
                    anchor.reference_class or "(unknown)")

    user_content = _build_stage_1_user_content(ctx)
    # D-119: shared system prefix is built once per assessment and reused as
    # the cached first system block across Stage 1 ensemble + Stage 2/3/7.
    # Stream 3.5: static_prefix (memory hierarchy) layered above the per-asset
    # prefix with a 1h TTL — survives many assessments of the same asset.
    shared_prefix = build_shared_system_prefix(ctx)
    static_prefix = build_static_prefix(ctx)
    stage_1_system_blocks = build_system_blocks(
        shared_prefix, STAGE_1_SYSTEM, static_prefix=static_prefix,
    )

    if ensemble_n > 1:
        logger.info("=== Stage 1+9 ensemble (%s, n=%d) ===", ensemble_mode, ensemble_n)
        if ensemble_mode == "batch":
            ensemble = run_batch_ensemble(
                a_client,
                stage_1_system=stage_1_system_blocks,
                stage_1_user_content=user_content,
                stage_9_system=STAGE_9_SYSTEM,
                n=ensemble_n,
                model=model,
                extractor_model=extractor_model,
            )
        else:
            ensemble = run_streaming_ensemble(
                a_client,
                stage_1_system=stage_1_system_blocks,
                stage_1_user_content=user_content,
                stage_9_system=STAGE_9_SYSTEM,
                n=ensemble_n,
                model=model,
                extractor_model=extractor_model,
            )
        cited_prose = ensemble.cited_prose_winner
        # Pick the parsed JSON from the run closest to the mean
        winner_run = min(ensemble.runs,
                         key=lambda r: abs(r.conviction_pct - ensemble.raw_mean_conviction))
        parsed = winner_run.parsed_json
        # Override fields with aggregated values
        parsed["thesis_direction"] = ensemble.direction
        parsed["conviction_pct"] = ensemble.final_conviction
        if ensemble.evidence_quality_mean is not None:
            parsed["evidence_quality"] = ensemble.evidence_quality_mean
        # Aggregated facts + uncertainties
        parsed["key_facts"] = ensemble.aggregated_key_facts
        parsed["uncertainties"] = ensemble.aggregated_uncertainties

        run.stage_metrics.append(StageMetric(
            stage_name=f"stage_1_synthesis_x{ensemble.n}",
            model=model,
            input_tokens=ensemble.total_input_tokens,
            output_tokens=ensemble.total_output_tokens,
            thinking_tokens=ensemble.total_thinking_tokens,
            cache_read_tokens=ensemble.total_cache_read_tokens,
            cache_creation_tokens=ensemble.total_cache_creation_tokens,
            cost_usd=ensemble.total_cost_usd,
            latency_ms=ensemble.total_latency_ms,
            notes={
                "ensemble_n": ensemble.n,
                "ensemble_mode": ensemble.mode,
                "direction_distribution": ensemble.direction_distribution,
                "raw_mean_conviction": ensemble.raw_mean_conviction,
                "dispersion": ensemble.dispersion,
                "shrinkage_factor": ensemble.shrinkage_factor,
                "final_conviction": ensemble.final_conviction,
                # Wave 3.3 — observability for the retry path
                "n_attempted": ensemble.n_attempted,
                "retries_used": ensemble.retries_used,
                "degraded": ensemble.degraded,
            },
        ))
        logger.info(
            "Ensemble: dist=%s mean=%.1f dispersion=%.1f final=%.1f "
            "cost=$%.3f (successful=%d/%d retries=%d degraded=%s)",
            ensemble.direction_distribution, ensemble.raw_mean_conviction,
            ensemble.dispersion, ensemble.final_conviction,
            ensemble.total_cost_usd, ensemble.n, ensemble.n_attempted,
            ensemble.retries_used, ensemble.degraded,
        )
        # Stash ensemble_runs payload for Stage 10
        run_ensemble_payload = {
            "n": ensemble.n,
            "n_attempted": ensemble.n_attempted,
            "retries_used": ensemble.retries_used,
            "degraded": ensemble.degraded,
            "mode": ensemble.mode,
            "direction_distribution": ensemble.direction_distribution,
            "raw_mean": ensemble.raw_mean_conviction,
            "dispersion": ensemble.dispersion,
            "shrinkage_factor": ensemble.shrinkage_factor,
            "final_conviction": ensemble.final_conviction,
            "runs": [
                {"run_idx": r.run_idx, "direction": r.direction,
                 "conviction_pct": r.conviction_pct,
                 "evidence_quality": r.evidence_quality}
                for r in ensemble.runs
            ],
        }
    else:
        logger.info("=== Stage 1: synthesis (%s) ===", model)
        cited_prose, m1 = stage_1_synthesize(a_client, ctx, model)
        run.stage_metrics.append(m1)
        logger.info("Stage 1: %dms / %d in / %d out / $%.3f",
                    m1.latency_ms, m1.input_tokens, m1.output_tokens, m1.cost_usd)

        logger.info("=== Stage 9: structured extraction (%s) ===", extractor_model)
        parsed, m9 = stage_9_extract(a_client, cited_prose, extractor_model)
        run.stage_metrics.append(m9)
        if not parsed:
            # Replaces the legacy `return None` contract — orchestrator_app
            # silently classified a None return as a successful run with
            # assessment_id=null. Raising lets the caller route this to
            # status='failed' with a specific error_message.
            logger.error("Stage 9 failed to parse JSON; aborting before Stage 10")
            raise Stage9ParseError(
                "Stage 9 produced unparseable JSON for cited prose; "
                f"output token count={m9.output_tokens}"
            )
        logger.info("Stage 9: %dms / %d in / %d out / $%.3f / direction=%s conviction=%s",
                    m9.latency_ms, m9.input_tokens, m9.output_tokens, m9.cost_usd,
                    parsed.get("thesis_direction"), parsed.get("conviction_pct"))
        run_ensemble_payload = None

    # ===========================================================================
    # Stage 2 — hypothesis enumeration (post-Stage 1 / Stage 6 winner)
    # Stage 3 — adversarial pre-mortem
    # ===========================================================================
    hypothesis_result: Optional[HypothesisResult] = None
    premortem_result: Optional[PreMortemResult] = None
    if enable_premortem:
        logger.info("=== Stage 2: hypothesis enumeration (%s) ===", model)
        hypothesis_result = run_hypothesis_enumeration(
            a_client,
            cited_prose=cited_prose,
            parsed_json=parsed,
            ctx=ctx,
            model=model,
            system_blocks=build_system_blocks(
                shared_prefix, STAGE_2_SYSTEM, static_prefix=static_prefix,
            ),
        )
        # D-118: post-Stage-2 prior renormalization. Blend model priors toward
        # the empirical base rate from Stage 4, weighted by (1 - evidence_quality).
        try:
            eq_for_anchor = parsed.get("evidence_quality")
            eq_for_anchor = float(eq_for_anchor) if eq_for_anchor is not None else None
        except (TypeError, ValueError):
            eq_for_anchor = None
        _, renorm_debug = renormalize_priors(
            hypothesis_result.hypotheses, anchor, eq_for_anchor,
        )
        run.stage_metrics.append(StageMetric(
            stage_name="stage_2_hypothesis_enumeration",
            model=model,
            input_tokens=hypothesis_result.input_tokens,
            output_tokens=hypothesis_result.output_tokens,
            cache_read_tokens=hypothesis_result.cache_read_tokens,
            cache_creation_tokens=hypothesis_result.cache_creation_tokens,
            cost_usd=hypothesis_result.cost_usd,
            latency_ms=hypothesis_result.latency_ms,
            status="completed" if hypothesis_result.pass_ else "failed",
            notes={
                "pass": hypothesis_result.pass_,
                "n_hypotheses": len(hypothesis_result.hypotheses),
                "labels": [h.label for h in hypothesis_result.hypotheses],
                "n_findings": len(hypothesis_result.findings),
                "renormalize": renorm_debug,
                # D-120: persist a head of the raw model response for audit;
                # full text is lost otherwise (no separate raw_response store).
                "raw_response_head": (hypothesis_result.raw_response or "")[:4000],
                "findings": [
                    {"severity": f.severity, "check": f.check,
                     "detail": f.detail[:200]}
                    for f in hypothesis_result.findings
                ],
                # Wave 6.3 — hypothesis-quality summary so we can detect
                # Stage 2 regressions (kill_conditions thinning, mechanism
                # citation density dropping) without re-querying the model.
                "quality_metrics": hypothesis_result.quality_metrics(),
            },
        ))
        logger.info(
            "Stage 2: pass=%s n_hypotheses=%d labels=%s findings=%d cost=$%.3f "
            "renorm=%s",
            hypothesis_result.pass_, len(hypothesis_result.hypotheses),
            [h.label for h in hypothesis_result.hypotheses],
            len(hypothesis_result.findings), hypothesis_result.cost_usd,
            renorm_debug.get("applied"),
        )

        if hypothesis_result.hypotheses:
            logger.info("=== Stage 3: pre-mortem (%s) ===", model)
            premortem_result = run_premortem(
                a_client,
                hypothesis_result=hypothesis_result,
                ctx=ctx,
                model=model,
                system_blocks=build_system_blocks(
                    shared_prefix, STAGE_3_SYSTEM, static_prefix=static_prefix,
                ),
            )
            run.stage_metrics.append(StageMetric(
                stage_name="stage_3_premortem",
                model=model,
                input_tokens=premortem_result.input_tokens,
                output_tokens=premortem_result.output_tokens,
                cache_read_tokens=premortem_result.cache_read_tokens,
                cache_creation_tokens=premortem_result.cache_creation_tokens,
                cost_usd=premortem_result.cost_usd,
                latency_ms=premortem_result.latency_ms,
                status="completed" if premortem_result.pass_ else "failed",
                notes={
                    "pass": premortem_result.pass_,
                    "overall_verdict": premortem_result.overall_verdict,
                    "surviving": premortem_result.surviving_hypothesis_ids,
                    "n_findings": len(premortem_result.findings),
                    # D-120: persist head of raw model response for audit.
                    "raw_response_head": (premortem_result.raw_response or "")[:4000],
                    "findings": [
                        {"severity": f.severity, "check": f.check,
                         "detail": f.detail[:200]}
                        for f in premortem_result.findings
                    ],
                },
            ))
            logger.info(
                "Stage 3: overall=%s surviving=%s findings=%d cost=$%.3f",
                premortem_result.overall_verdict,
                premortem_result.surviving_hypothesis_ids,
                len(premortem_result.findings), premortem_result.cost_usd,
            )

            # B4 (was D-117): on all_falsified, signal Stage 10 to cap AFTER
            # isotonic calibration — do NOT mutate parsed["conviction_pct"]
            # here. Capping before calibration hid the raw model output from
            # the curve and made conviction_pct_calibrated = isotonic(min(raw,
            # 30)) instead of min(isotonic(raw), 30). Stash the raw value on
            # ctx so stage_10_persist writes raw_conviction_pct correctly
            # and applies the cap to the calibrated output.
            if premortem_result.overall_verdict == "all_falsified":
                try:
                    raw_conv = float(parsed.get("conviction_pct") or 0.0)
                except (TypeError, ValueError):
                    raw_conv = 0.0
                if raw_conv > ALL_FALSIFIED_CONVICTION_CEILING:
                    logger.warning(
                        "Stage 3 all_falsified: conviction %.1f will be capped "
                        "to %.1f AFTER Stage 8 calibration in stage_10_persist",
                        raw_conv, ALL_FALSIFIED_CONVICTION_CEILING,
                    )
                    ctx["conviction_capped_by_premortem"] = True
                # Always set pre_premortem_conviction so raw_conviction_pct
                # writeback is consistent whether or not the cap binds.
                ctx["pre_premortem_conviction"] = raw_conv
        else:
            logger.warning("Stage 2 emitted no hypotheses; skipping Stage 3.")

    # Stage 7 constitutional check
    constitutional_result: Optional[ConstitutionalResult] = None
    if run_constitutional:
        logger.info("=== Stage 7: constitutional check ===")
        try:
            conviction_for_check = float(parsed.get("conviction_pct") or 50.0)
        except (TypeError, ValueError):
            conviction_for_check = 50.0
        constitutional_result = run_constitutional_check(
            a_client,
            cited_prose=cited_prose,
            facts=ctx["facts"],
            document_ids=[d["id"] for d in ctx["documents"]],
            thesis_direction=parsed.get("thesis_direction") or "neutral",
            conviction_pct=conviction_for_check,
            reference_class=asset.get("reference_class_signature"),
            reference_class_base_rate=(
                anchor.base_rate.approval_rate if anchor.base_rate else None),
            model=extractor_model,
            skip_semantic=constitutional_skip_semantic,
            hypothesis_result=hypothesis_result,
            premortem_result=premortem_result,
            semantic_system_blocks=build_system_blocks(
                shared_prefix, SEMANTIC_SYSTEM_PROMPT, static_prefix=static_prefix,
            ),
        )
        run.stage_metrics.append(StageMetric(
            stage_name="stage_7_constitutional",
            model=extractor_model if not constitutional_skip_semantic else "deterministic",
            input_tokens=constitutional_result.semantic_input_tokens,
            output_tokens=constitutional_result.semantic_output_tokens,
            cost_usd=constitutional_result.semantic_cost_usd,
            latency_ms=constitutional_result.semantic_latency_ms,
            status="completed" if constitutional_result.pass_ else "failed",
            notes={
                "pass": constitutional_result.pass_,
                "n_findings": len(constitutional_result.findings),
                "n_citations_checked": constitutional_result.n_citations_checked,
                "n_citations_resolved": constitutional_result.n_citations_resolved,
                "findings": [
                    {"severity": f.severity, "check": f.check,
                     "detail": f.detail[:200]}
                    for f in constitutional_result.findings
                ],
            },
        ))
        logger.info("Stage 7: pass=%s findings=%d (citations: %d/%d resolved) cost=$%.3f",
                    constitutional_result.pass_, len(constitutional_result.findings),
                    constitutional_result.n_citations_resolved,
                    constitutional_result.n_citations_checked,
                    constitutional_result.semantic_cost_usd)

    # Phase 4A (D-127): expose parsed payload to the replay harness before
    # the persistence gate. dict.update() preserves the caller's reference
    # so they can read it after run_one returns.
    if parsed_out is not None:
        # Filled below as well — also fill it here so a constitutional-fail
        # abort still hands the parsed payload back to the replay harness for
        # inspection (the test harness uses dry_run + parsed_out together).
        parsed_out.clear()
        parsed_out.update(parsed)

    # D-117 enforcement: Stage 7 hard fail aborts the pipeline BEFORE Stage 10.
    # Persisting an assessment with unresolved citations or structural Stage 2/3
    # errors would taint convergence_assessments and downstream calibration —
    # the docstring promise that constitutional findings gate the run is now
    # enforced. The catcher in orchestrator_app.drain_queue writes
    # status='failed_constitutional' on this exception.
    if constitutional_result and not constitutional_result.pass_:
        error_findings = [
            f for f in constitutional_result.findings if f.severity == "error"
        ]
        logger.error(
            "Stage 7 constitutional fail (%d error finding(s)); "
            "aborting before Stage 10",
            len(error_findings),
        )
        raise ConstitutionalFailure(error_findings)

    if dry_run:
        logger.info("[dry-run] would persist; assessment summary:")
        logger.info("  thesis_direction: %s", parsed.get("thesis_direction"))
        logger.info("  conviction_pct: %s", parsed.get("conviction_pct"))
        logger.info("  evidence_quality: %s", parsed.get("evidence_quality"))
        logger.info("  thesis_summary: %s", parsed.get("thesis_summary"))
        logger.info("  band: %s", derive_band(float(parsed.get("conviction_pct") or 50.0)))
        if hypothesis_result:
            logger.info("  hypotheses: %d (%s)",
                        len(hypothesis_result.hypotheses),
                        [h.label for h in hypothesis_result.hypotheses])
        if premortem_result:
            logger.info("  pre_mortem_verdict: %s surviving: %s",
                        premortem_result.overall_verdict,
                        premortem_result.surviving_hypothesis_ids)
        if constitutional_result:
            logger.info("  constitutional_pass: %s", constitutional_result.pass_)
        return None

    logger.info("=== Stage 10: persist ===")
    assessment_id = stage_10_persist(
        sb, asset_id, run, cited_prose, parsed, ctx, model, extractor_model,
        ensemble_payload=run_ensemble_payload,
        constitutional_result=constitutional_result,
        hypothesis_result=hypothesis_result,
        premortem_result=premortem_result,
    )
    logger.info("Persisted assessment: %s", assessment_id)
    return assessment_id


def main(argv: List[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="orchestrator_runtime.runtime")
    p.add_argument("--asset-id", required=True)
    p.add_argument("--trigger-type", default="manual",
                   choices=["new_doc", "cross_source", "scheduled",
                            "operator_refresh", "market_move", "tier2_escalation",
                            "backtest", "manual"])
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--extractor-model", default=DEFAULT_EXTRACTOR_MODEL)
    p.add_argument("--ensemble-n", type=int, default=1,
                   help="N parallel synthesis runs (1=single-shot, 3+ enables ensemble)")
    p.add_argument("--ensemble-mode", default="streaming",
                   choices=["streaming", "batch"],
                   help="streaming = N concurrent live calls (rate-limit risk); "
                        "batch = Messages Batches API (50%% cost, ~1h latency)")
    p.add_argument("--no-constitutional", action="store_true",
                   help="Skip Stage 7 constitutional check entirely")
    p.add_argument("--constitutional-deterministic-only", action="store_true",
                   help="Run only the deterministic citation-resolution checks "
                        "(no Sonnet adversarial pass)")
    p.add_argument("--no-premortem", action="store_true",
                   help="Skip Stage 2 (hypothesis enumeration) + Stage 3 "
                        "(pre-mortem). Useful for cost-bounded backtests or "
                        "if a regression is found in either stage.")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        logger.error("ANTHROPIC_API_KEY not set")
        return 2

    sb = SupabaseClient()
    a_client = OrchestratorClient()

    aid = run_one(
        sb, a_client,
        asset_id=args.asset_id,
        trigger_type=args.trigger_type,
        model=args.model,
        extractor_model=args.extractor_model,
        ensemble_n=args.ensemble_n,
        ensemble_mode=args.ensemble_mode,
        run_constitutional=not args.no_constitutional,
        constitutional_skip_semantic=args.constitutional_deterministic_only,
        enable_premortem=not args.no_premortem,
        dry_run=args.dry_run,
    )
    return 0 if (aid is not None or args.dry_run) else 1


if __name__ == "__main__":
    sys.exit(main())
