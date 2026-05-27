"""Orchestrator runtime — v4 AI-first assessment.

Reads (asset, extracted_facts, key documents, market context, prior memory)
and emits one `convergence_assessments` row.

The v4 runtime keeps judgment in one cited AI synthesis pass and keeps code on
evidence integrity, calibration, market gates, cost, persistence, and memory.

Pipeline:
  Stage 0  — load asset metadata + extracted_facts + memory hierarchy
  Stage 1  — FDA + commercial Sonnet synthesis (cited prose)
  Stage 4  — reference-class anchoring (base rate + similar resolved cases)
  Stage 7  — deterministic citation-resolution check
  Stage 9  — Sonnet structured-output extraction → schema-validated JSON
  Stage 10 — write convergence_assessments row + post_mortem_queue stub

Run:
  ANTHROPIC_API_KEY=... SUPABASE_URL=... \\
    python3 -m orchestrator_runtime.runtime --asset-id <uuid> [--dry-run]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

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
    parse_json_or_none,
)
from orchestrator_runtime.memory import MemoryStore, MemoryBlobs
from orchestrator_runtime.sub_agent_dispatcher import (
    DISPATCH_TOOL_DEF,
    dispatch_sub_agent_tool,
    reset_budget as reset_sub_agent_budget,
)
from orchestrator_runtime.evidence_packet import validate_evidence_packet

logger = logging.getLogger(__name__)

ORCHESTRATOR_VERSION = "orch-v4.0"

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
STAGE_1_RAG_CORPORA_OVERRIDE = os.environ.get("ORCH_STAGE_1_RAG_CORPORA")
# Raised 4 → 8 (2026-05-23, audit/sub_agent_schema_drift_2026-05-23.md §S-1):
# 4 turns proved too tight on VRDN dry-run — Stage 1 used 3 turns dispatching 3
# of 4 sub-agents and ran out on the 4th turn before producing final JSON. 8
# leaves headroom for parallel dispatch + per-role retry + final synthesis.
# Per-turn cost is bounded by the $15/run BudgetExceededError ceiling.
SUB_AGENT_LOOP_MAX_TURNS = int(os.environ.get("ORCH_SUB_AGENT_LOOP_MAX_TURNS", "8"))

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

# Per-asset doc-buffer construction caps (keep below Tier-1 rate limit
# of 30k input tokens/min on the new API key)
MAX_FACTS_IN_PROMPT = 80
MAX_DOC_EXCERPTS = 8
DOC_EXCERPT_CHARS = 4000

# Band thresholds (derived from conviction_pct). Plan §"probabilistic +
# calibrated, not categorical" — these are configurable, not hardcoded
# inputs to the model.
BAND_THRESHOLDS = [
    (80.0, "immediate"),
    (60.0, "watchlist"),
    (40.0, "archive"),
    (0.0, "discard"),
]

DEFAULT_PREDICTION_TARGET = {
    "target_type": "price_move",
    "horizon_days": 30,
    "event_anchor": None,
    "label_rule": "forward_return_t30_calendar",
}
PREDICTION_TARGET_TYPES = {
    "price_move",
    "regulatory_outcome",
    "event_outcome",
}
PREDICTION_LABEL_RULES = {
    "forward_return_t30_calendar",
    "forward_return",
    "approval_decision",
    "adcom_recommendation",
}

MARKET_SIDE_GATE_ENABLED = os.environ.get("ORCH_ENABLE_MARKET_SIDE_GATE") == "1"
MARKET_SIDE_GATE_EV_THRESHOLD_BPS = float(
    os.environ.get("ORCH_MARKET_GATE_EV_THRESHOLD_BPS", "0.0")
)
ROLE_DIVERSE_ENSEMBLE_ENABLED = (
    os.environ.get("ORCH_ENABLE_ROLE_DIVERSE_ENSEMBLE") == "1"
)
DEFAULT_POST_MORTEM_WINDOW_DAYS = int(
    os.environ.get("ORCH_POST_MORTEM_WINDOW_DAYS", "60")
)
CATALYST_EVENT_TYPES = (
    "pdufa",
    "advisory_committee",
    "eop2",
    "readout",
)


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
    orchestrator_run_id: Optional[str] = None
    document_window_start: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc) - timedelta(days=180))
    document_window_end: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc))
    stage_metrics: List[StageMetric] = field(default_factory=list)


class EvidencePacketError(RuntimeError):
    """Raised when Tier-1 would synthesize without enough grounded evidence."""


@dataclass
class ConstitutionalFinding:
    severity: str
    check: str
    detail: str
    affected_id: Optional[str] = None


@dataclass
class ConstitutionalResult:
    pass_: bool
    findings: List[ConstitutionalFinding] = field(default_factory=list)
    n_citations_checked: int = 0
    n_citations_resolved: int = 0
    semantic_check_used: bool = False
    semantic_input_tokens: int = 0
    semantic_output_tokens: int = 0
    semantic_cost_usd: float = 0.0
    semantic_latency_ms: int = 0


class ConstitutionalFailure(RuntimeError):
    """Raised when Stage 7 blocks persistence of an assessment."""

    def __init__(
        self,
        findings: Optional[List[ConstitutionalFinding]] = None,
        message: Optional[str] = None,
    ):
        self.findings = findings or []
        if message:
            super().__init__(message)
            return
        n_errors = sum(1 for f in self.findings if f.severity == "error")
        noun = "finding" if n_errors == 1 else "findings"
        super().__init__(
            f"constitutional check failed with {n_errors} error {noun}"
        )


class Stage9ParseError(RuntimeError):
    """Raised when Stage 9 fails to emit valid structured JSON."""


CITE_FACT_RE = re.compile(r"\[F:([0-9a-f]{6,12})\]", re.IGNORECASE)
CITE_DOC_RE = re.compile(r"\[D:([0-9a-f]{6,12})\]", re.IGNORECASE)


def _validate_citations(
    *,
    cited_prose: str,
    facts: List[Dict[str, Any]],
    document_ids: List[str],
) -> ConstitutionalResult:
    """Deterministically verify Stage 1 cites resolve to visible evidence.

    Phase 6c removes the semantic constitutional reviewer but keeps this
    invariant: every [F:short] and [D:short] in the persisted thesis must map
    to an extracted fact or source document shown to the model.

    Prefix-tolerant lookup: the regex accepts 6–12 char shorts, but historically
    the lookup set was built with [:8] which only matched exact 8-char shorts.
    The Stage 1 prompt example uses 6-char shorts (`[F:abc123]`) while the
    in-context facts table shows 8-char shorts, so when the model anchors on
    the example it emits shorts the validator can't resolve and Stage 7 blocks
    every run. We compare each cited short against the FULL fact_id / document_id
    as a prefix. UUID collision risk on a 6-char hex prefix for ≤80 facts is
    ~1 in 16M — negligible.
    """
    fact_ids_lower = [str(f["id"]).lower() for f in facts if f.get("id")]
    doc_ids_lower = [str(d).lower() for d in document_ids if d]
    cited_facts = {
        m.group(1).lower() for m in CITE_FACT_RE.finditer(cited_prose or "")
    }
    cited_docs = {
        m.group(1).lower() for m in CITE_DOC_RE.finditer(cited_prose or "")
    }

    findings: List[ConstitutionalFinding] = []
    n_total = len(cited_facts) + len(cited_docs)
    n_resolved = 0

    for short in cited_facts:
        if any(fid.startswith(short) for fid in fact_ids_lower):
            n_resolved += 1
        else:
            findings.append(ConstitutionalFinding(
                severity="error",
                check="unresolved_fact_id",
                detail=(
                    f"Cited fact_id [F:{short}] does not resolve to any fact "
                    "in the assessment's fact_ids list"
                ),
                affected_id=short,
            ))

    for short in cited_docs:
        if any(did.startswith(short) for did in doc_ids_lower):
            n_resolved += 1
        else:
            findings.append(ConstitutionalFinding(
                severity="error",
                check="unresolved_doc_id",
                detail=(
                    f"Cited doc_id [D:{short}] does not resolve to any "
                    "document in the assessment's document_ids list"
                ),
                affected_id=short,
            ))

    return ConstitutionalResult(
        pass_=all(f.severity != "error" for f in findings),
        findings=findings,
        n_citations_checked=n_total,
        n_citations_resolved=n_resolved,
    )


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

    # Pull material documents linked to this asset, newest-first
    asset_docs = client._rest(
        "GET", "asset_documents",
        params={
            "select": "document_id,link_type,extraction_confidence,extracted_spans",
            "asset_id": f"eq.{asset_id}",
            "is_material": "is.true",
            "order": "created_at.desc",
        },
    ) or []
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
            try:
                blob = client.read_cache("memory", legacy_path.lstrip("/"))
                if blob:
                    memory_text = blob.decode("utf-8", errors="replace")
            except Exception as exc:
                logger.debug("legacy memory %s not found: %s", legacy_path, exc)

    return {
        "asset": asset,
        "facts": facts,
        "documents": docs,
        "memory_text": memory_text,
        "memory_blobs": memory_blobs,
        "asset_doc_links": asset_docs,
        "reference_class_anchor": None,  # populated by stage_4_anchor
    }


def require_tier1_evidence_packet(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Fail closed before Tier-1 synthesis when source grounding is incomplete."""
    packet = validate_evidence_packet(
        asset=ctx.get("asset") or {},
        extracted_facts=ctx.get("facts") or [],
        asset_documents=ctx.get("asset_doc_links") or [],
        tier=1,
    )
    ctx["evidence_packet"] = packet
    if not packet.get("ok"):
        raise EvidencePacketError(
            "Tier-1 evidence packet incomplete: "
            + ", ".join(packet.get("errors") or ["unknown_error"])
        )
    return packet


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
    metric = StageMetric(
        stage_name="stage_4_reference_class_anchor",
        model="deterministic",
        latency_ms=int((time.monotonic() - t0) * 1000),
        notes={
            "reference_class": reference_class,
            "has_base_rate": anchor.base_rate is not None,
            "n_similar_cases": len(anchor.similar_cases),
            "n_cases_in_class": (anchor.base_rate.n_cases
                                 if anchor.base_rate else None),
            "approval_rate_pct": (round(anchor.base_rate.as_pct(), 2)
                                  if anchor.base_rate else None),
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
    corpora = recommend_stage_1_rag_corpora(ctx)
    try:
        from orchestrator_runtime import rag_handle
        if corpora == ["all"]:
            chunks = rag_handle.hybrid_search(
                sb, query,
                corpus="all",
                k=k,
                asset_id=asset.get("id") if asset_scoped else None,
            )
        else:
            per_corpus_k = max(2, (k + len(corpora) - 1) // len(corpora))
            all_hits: List[Any] = []
            for corpus in corpora:
                all_hits.extend(rag_handle.hybrid_search(
                    sb, query,
                    corpus=corpus,
                    k=per_corpus_k,
                    asset_id=asset.get("id") if asset_scoped else None,
                ))
            chunks = _merge_rag_hits(all_hits, k=k)
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
            "corpora": corpora,
        },
    )


# ===========================================================================
# Stage 1 — Sonnet synthesis
# ===========================================================================

# Canonical v4 Stage 1 prompt: FDA + commercial dual-mandate synthesis. This
# prompt absorbs the old separate hypothesis enumeration, adversarial pre-mortem,
# ensemble, and semantic constitutional-review concerns into one cited AI pass.
STAGE_1_SYSTEM = """You are a biotech investment analyst producing a complete \
thesis on one tracked drug asset. You evaluate two parallel dimensions:

1. REGULATORY — approval probability anchored in trial-data forensics, AdCom \
risk, label risk, sponsor track record, class precedents.
2. COMMERCIAL — total addressable market (TAM), market-cap vs opportunity \
ratio, standard of care for the indication, severity of unmet medical need, \
side-effect profile of current therapies, regulatory incentives derived from \
therapeutic gaps, competitive landscape.

You synthesize from a structured fact layer + raw document excerpts + (when \
available) prior assessment memory.

Your output is CITED PROSE — every material claim about the drug, the trial, \
or the sponsor MUST reference a fact_id from the structured layer (in \
[F:<fact_id_short>] notation, e.g. [F:abc12345]) or a document_id (in \
[D:<doc_id_short>]). Use the exact 8-character short shown in the fact table \
below — do not abbreviate further.

Commercial-dimension claims (TAM, standard of care, unmet need severity, \
current-therapy side effects) may not have direct fact_id support in the \
document set. For those, you MAY draw on widely-known clinical and market \
baselines, but you MUST mark such claims with [INF] (inferred) and keep them \
conservative. Speculative commercial estimates without grounding will reduce \
evidence_quality.

Uncited claims about regulatory facts will be rejected by the constitutional \
check.

Required output structure (verbatim section headers, in this order):

## Asset summary
2-3 sentences identifying the asset, indication, and current regulatory state.

## Catalyst landscape
The pending catalyst (PDUFA date, AdComm, readout, etc.) and what's known \
about it. Cite specific facts.

## Regulatory evidence for approval / positive direction
Bullet list. Each bullet cites the specific fact(s) that support it.

## Regulatory evidence for CRL / negative direction
Bullet list. Each bullet cites contradicting facts. If you cannot find \
contrary evidence, say "no contrary evidence found in the document set" \
explicitly.

## Commercial opportunity
A required section with the following sub-bullets (address each, even if briefly):
- **TAM**: estimated total addressable market in USD; provide low/high range; \
cite facts where available, otherwise mark [INF].
- **Market-cap vs opportunity**: ratio of current company market cap to \
estimated peak revenue; brief assessment of whether the stock looks mispriced.
- **Standard of care**: what's currently used for this indication.
- **SoC limitations & side effects**: where current therapies fall short — \
efficacy ceiling, durability, treatment-emergent adverse events, \
contraindications.
- **Unmet need severity (1-5)**: 5 = no adequate therapy exists / \
mortality-driving unmet need; 4 = poor existing options with major \
side-effect burden; 3 = adequate options exist but improvement clearly \
valuable; 2 = mild incremental need; 1 = crowded category with strong \
existing options.
- **Regulatory incentives**: Breakthrough / Fast Track / Orphan Drug / \
Priority Review designations and how they relate to therapeutic gap.
- **Competitive landscape**: programs in the same indication at comparable \
phases; who else is approaching the same patient population.

## Key uncertainties
Bullet list of open questions where the evidence is ambiguous. Each \
uncertainty: what's unknown, why it matters, what would resolve it.

## Reasoning trace
3-5 sentences walking through how you weighted regulatory + commercial \
evidence to reach your direction + conviction. Be explicit about how the \
commercial picture modulated (amplified, dampened, or did not affect) the \
pure regulatory thesis.

## Conclusion
- thesis_direction: long | short | neutral | straddle
- conviction_pct: 0-100 (probability your direction is correct)
- evidence_quality: 0.0-1.0 (how confident are you in the underlying \
evidence base — separate from direction)

Direction calibration:
  long: expect approval and/or positive market move
  short: expect CRL and/or negative market move
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
    system_prompt: str = STAGE_1_SYSTEM,
) -> tuple[str, StageMetric]:
    """Stage 1 synthesis. Single-shot by default.

    Stream 3.6: when `enable_sub_agents=True`, Claude is given the
    `dispatch_sub_agent` tool and the call enters a tool-use loop. Claude can
    issue parallel tool calls for literature / competitive / regulatory_history /
    options_microstructure within one assistant turn; results land back as
    tool_result blocks and Claude continues until it produces final cited
    prose.

    The canonical v4 prompt includes the required Commercial opportunity
    section and the inline reasoning discipline that replaced the old Stage
    2/3/6/semantic-7 chain.
    """
    user_content = _build_stage_1_user_content(ctx)
    facts = ctx["facts"]
    docs = ctx["documents"]
    system_blocks = build_system_blocks(
        build_shared_system_prefix(ctx), system_prompt,
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

    for turn in range(SUB_AGENT_LOOP_MAX_TURNS):
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

# Canonical v4 Stage 9 schema. The commercial_dimensions block is part of the
# production output contract and persists to convergence_assessments.
STAGE_9_SYSTEM = """You convert a cited-prose investment thesis into a strict \
JSON object matching the schema below. Do not add commentary; emit JSON only.

Schema:
{
  "thesis_direction": "long" | "short" | "neutral" | "straddle",
  "conviction_pct": <number 0-100>,
  "prediction_target": {
    "target_type": "price_move" | "regulatory_outcome" | "event_outcome",
    "horizon_days": <integer or null>,
    "event_anchor": "<event id/name or null>",
    "label_rule": "forward_return_t30_calendar" | "approval_decision" | "adcom_recommendation"
  },
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
  "reasoning_summary": "<2-3 sentence reasoning trace>",
  "commercial_dimensions": {
    "tam_estimate": {
      "low_usd": <number or null>,
      "high_usd": <number or null>,
      "is_inferred": <bool>
    },
    "mcap_to_peak_revenue_ratio": <number or null>,
    "standard_of_care": "<short>",
    "soc_limitations": ["<short>", "..."],
    "soc_side_effects": ["<short>", "..."],
    "unmet_need_severity_1_5": <integer 1-5>,
    "regulatory_incentives": ["breakthrough" | "fast_track" | "orphan_drug" | "priority_review" | "none", "..."],
    "competitive_landscape_summary": "<short>"
  }
}

Rules:
- thesis_direction MUST be one of the four values
- conviction_pct + evidence_quality MUST be numeric (no strings)
- prediction_target is required. Use price_move + horizon_days=30 + \
label_rule=forward_return_t30_calendar unless the prose explicitly predicts a \
regulatory or event outcome.
- key_facts: 5-15 items, each grounded in a [F:...] cite from the prose
- uncertainties: 2-5 items
- cited_prose_blocks: one per section header in the prose; preserve all \
[F:...] / [D:...] cites you find
- commercial_dimensions: REQUIRED. Pull values from the "Commercial \
opportunity" section of the prose. tam_estimate.is_inferred must be true \
when any TAM number is marked [INF] in the prose. \
unmet_need_severity_1_5 MUST be an integer 1-5. regulatory_incentives is \
an array — use ["none"] if no designations apply.
- Output ONLY the JSON object — no markdown fences, no commentary"""


def stage_9_extract(
    a_client: OrchestratorClient,
    cited_prose: str,
    model: str,
    *,
    system_prompt: str = STAGE_9_SYSTEM,
) -> tuple[Optional[Dict[str, Any]], StageMetric]:
    """Stage 9 structured extraction.

    The canonical v4 schema includes `commercial_dimensions`.
    """
    user_content = f"Cited prose to extract:\n\n{cited_prose}"
    result = a_client.call(
        system=system_prompt,
        messages=[{"role": "user", "content": user_content}],
        model=model,
        max_tokens=8192,
    )
    parsed = parse_json_or_none(result.text)
    metric = StageMetric(
        stage_name="stage_9_extraction",
        model=result.model,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        thinking_tokens=result.thinking_tokens,
        cache_read_tokens=result.cache_read_tokens,
        cache_creation_tokens=result.cache_creation_tokens,
        cost_usd=result.cost_usd,
        latency_ms=result.latency_ms,
        status="completed" if parsed else "failed",
        notes={"parsed": bool(parsed)},
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


def compute_document_set_hash(
    sb: SupabaseClient, asset_id: str
) -> Optional[str]:
    # Parity contract with reactor `computeDocSetHash` (supabase/functions/
    # reactor/index.ts): MD5 over the sorted document_id list of the asset's
    # current material-primary asset_documents rows. Both sides MUST produce
    # the same hash for the dedup index in
    # 20260527000010_v3_content_dedup_document_set_hash.sql to suppress
    # re-enqueues whose evidence set is unchanged.
    rows = sb._rest(
        "GET", "asset_documents",
        params={
            "select": "document_id",
            "asset_id": f"eq.{asset_id}",
            "link_type": "eq.primary",
            "is_material": "is.true",
        },
    ) or []
    if not rows:
        return None
    sorted_ids = sorted(r["document_id"] for r in rows)
    payload = ",".join(sorted_ids).encode("utf-8")
    return hashlib.md5(payload).hexdigest()


def _coerce_prediction_target(value: Any) -> Dict[str, Any]:
    """Normalize Stage 9's prediction_target object to DB columns."""
    out = dict(DEFAULT_PREDICTION_TARGET)
    if isinstance(value, dict):
        target_type = value.get("target_type")
        if target_type in PREDICTION_TARGET_TYPES:
            out["target_type"] = target_type

        label_rule = value.get("label_rule")
        if label_rule in PREDICTION_LABEL_RULES:
            out["label_rule"] = label_rule

        event_anchor = value.get("event_anchor")
        out["event_anchor"] = str(event_anchor) if event_anchor else None

        horizon = value.get("horizon_days")
        if horizon is None and out["target_type"] != "price_move":
            out["horizon_days"] = None
        else:
            try:
                out["horizon_days"] = int(horizon)
            except (TypeError, ValueError):
                pass

    if out["target_type"] == "price_move" and out.get("horizon_days") is None:
        out["horizon_days"] = DEFAULT_PREDICTION_TARGET["horizon_days"]
    return out


def _normalize_json_for_hash(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def compute_convergence_signature(
    *,
    direction: str,
    calibrated_conviction_pct: float,
    cited_prose_blocks: List[Dict[str, Any]],
    key_facts: List[Dict[str, Any]],
    fact_ids: List[str],
    document_ids: List[str],
) -> str:
    """Stable signature for duplicate Stage 9 outputs over the same asset."""
    bucketed_conviction = int(round(float(calibrated_conviction_pct) / 5.0) * 5)
    payload = {
        "direction": direction,
        "bucketed_conviction": max(0, min(100, bucketed_conviction)),
        "cited_prose_blocks": cited_prose_blocks,
        "key_facts": key_facts,
        "fact_ids": sorted(fact_ids),
        "document_ids": sorted(document_ids),
    }
    return hashlib.md5(_normalize_json_for_hash(payload).encode("utf-8")).hexdigest()


def build_claim_ledger(
    *,
    cited_prose_blocks: List[Dict[str, Any]],
    key_facts: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Normalize alertable model claims into a compact evidence ledger.

    This is intentionally derived from already-hydrated Stage 9 structures so
    it cannot introduce new claims. Downstream alert gates can require at
    least one supported claim without reparsing free-form prose.
    """
    claims: List[Dict[str, Any]] = []
    for idx, block in enumerate(cited_prose_blocks or []):
        text = str(block.get("text") or "").strip()
        fact_ids = [f for f in (block.get("fact_citations") or []) if f]
        doc_ids = [d for d in (block.get("doc_citations") or []) if d]
        if not text:
            continue
        claims.append({
            "claim_id": f"C{len(claims) + 1}",
            "claim_type": "cited_prose_block",
            "section": block.get("section") or f"block_{idx + 1}",
            "claim_text": text[:1200],
            "supporting_fact_ids": fact_ids,
            "contradicting_fact_ids": [],
            "document_ids": doc_ids,
            "verifier_status": (
                "supported" if fact_ids or doc_ids else "unsupported"
            ),
            "model_origin": "stage_9_extraction",
        })

    for idx, fact in enumerate(key_facts or []):
        text = str(fact.get("text") or "").strip()
        fact_id = fact.get("fact_id")
        if not text:
            continue
        claims.append({
            "claim_id": f"C{len(claims) + 1}",
            "claim_type": "key_fact",
            "section": "key_facts",
            "claim_text": text[:600],
            "supporting_fact_ids": [fact_id] if fact_id else [],
            "contradicting_fact_ids": [],
            "document_ids": [],
            "verifier_status": "supported" if fact_id else "unsupported",
            "model_origin": "stage_9_extraction",
            "source_index": idx,
        })

    return claims


def _find_existing_convergence_signature(
    sb: SupabaseClient,
    *,
    asset_id: str,
    convergence_signature: Optional[str],
) -> Optional[str]:
    if not convergence_signature:
        return None
    try:
        rows = sb._rest(
            "GET", "convergence_assessments",
            params={
                "select": "id",
                "asset_id": f"eq.{asset_id}",
                "convergence_signature": f"eq.{convergence_signature}",
                "superseded_by": "is.null",
                "order": "created_at.desc",
                "limit": "1",
            },
        ) or []
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "convergence_signature precheck unavailable; continuing insert: %s",
            exc,
        )
        return None
    return rows[0].get("id") if rows else None


def recommend_stage_1_rag_corpora(ctx: Dict[str, Any]) -> List[str]:
    """Choose the minimum useful RAG corpus set for Stage 1."""
    if STAGE_1_RAG_CORPORA_OVERRIDE:
        requested = [
            c.strip() for c in STAGE_1_RAG_CORPORA_OVERRIDE.split(",")
            if c.strip()
        ]
        return requested or ["all"]

    asset = ctx.get("asset") or {}
    haystack = " ".join(
        str(asset.get(k) or "")
        for k in (
            "reference_class_signature",
            "program_status",
            "indication",
            "indication_normalized",
            "application_type",
        )
    ).lower()

    if any(tok in haystack for tok in ("phase3", "phase 3", "trial", "readout", "endpoint")):
        return ["literature", "filings"]
    if any(tok in haystack for tok in ("pdufa", "adcom", "advisory", "crl", "nda", "bla")):
        return ["filings", "news", "labels_aes"]
    if any(tok in haystack for tok in ("safety", "cmc", "manufacturing", "warning", "483")):
        return ["filings", "labels_aes", "news"]
    return ["all"]


def _chunk_identifier(hit: Any) -> Optional[str]:
    if isinstance(hit, dict):
        return hit.get("chunk_id") or hit.get("id")
    return getattr(hit, "chunk_id", None) or getattr(hit, "id", None)


def _chunk_score(hit: Any) -> float:
    if isinstance(hit, dict):
        value = hit.get("rerank_score", hit.get("score", 0.0))
    else:
        value = getattr(hit, "rerank_score", getattr(hit, "score", 0.0))
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _merge_rag_hits(hits: List[Any], *, k: int) -> List[Any]:
    seen: Dict[str, Any] = {}
    anonymous: List[Any] = []
    for hit in hits:
        key = _chunk_identifier(hit)
        if not key:
            anonymous.append(hit)
            continue
        if key not in seen or _chunk_score(hit) > _chunk_score(seen[key]):
            seen[key] = hit
    merged = list(seen.values()) + anonymous
    return sorted(merged, key=_chunk_score, reverse=True)[:k]


def _resolve_market_event_date(
    sb: SupabaseClient,
    asset_id: str,
    fallback: datetime,
) -> Tuple[date, Optional[str]]:
    rows = sb._rest(
        "GET", "fda_regulatory_events",
        params={
            "select": "id,event_date,event_type,event_status",
            "asset_id": f"eq.{asset_id}",
            "event_type": "in.(pdufa,advisory_committee,eop2,readout)",
            "event_status": "in.(pending,resolved)",
            "order": "event_date.asc.nullslast",
            "limit": "1",
        },
    ) or []
    if rows and rows[0].get("event_date"):
        try:
            event_day = date.fromisoformat(str(rows[0]["event_date"])[:10])
            marker = f"{rows[0].get('event_type')}:{rows[0].get('id')}"
            return event_day, marker
        except ValueError:
            pass
    return (fallback + timedelta(days=30)).date(), None


def compute_market_side_context(
    sb: SupabaseClient,
    *,
    asset_id: str,
    asset: Dict[str, Any],
    calibrated_conviction_pct: float,
    direction: str,
    current_band: str,
    run: AssessmentRun,
) -> Tuple[Dict[str, Any], str, Optional[str]]:
    """Fetch options context and optionally downgrade low-EV immediate alerts."""
    if not MARKET_SIDE_GATE_ENABLED:
        return {}, current_band, None
    ticker = asset.get("ticker")
    if not ticker or direction not in {"long", "short"}:
        return {}, current_band, None
    try:
        from modal_workers.providers.polygon.base import PolygonClient
        from modal_workers.providers.polygon.options_data import PolygonOptionsData
        provider = PolygonOptionsData(client=PolygonClient())
        event_date, marker = _resolve_market_event_date(
            sb, asset_id, run.document_window_end,
        )
        straddle = provider.get_straddle_implied_move(str(ticker), event_date)
    except Exception as exc:  # noqa: BLE001
        logger.info("market-side gate skipped for %s: %s", ticker, exc)
        return {
            "market_gate_status": "skipped",
            "market_gate_reason": str(exc)[:160],
        }, current_band, None

    if not straddle:
        return {"market_gate_status": "no_options_context"}, current_band, None

    implied_move = (
        straddle.get("implied_move_pct")
        or straddle.get("straddle_implied_move_pct")
    )
    try:
        implied_move_f = float(implied_move)
    except (TypeError, ValueError):
        return {"market_gate_status": "bad_options_context"}, current_band, None

    iv_values = [
        float(v) for v in (straddle.get("call_iv"), straddle.get("put_iv"))
        if isinstance(v, (int, float))
    ]
    options_iv = (sum(iv_values) / len(iv_values)) if iv_values else None
    if options_iv is not None and options_iv <= 5:
        options_iv *= 100.0

    directional_edge = max(0.0, calibrated_conviction_pct / 100.0 - 0.5)
    expected_value_bps = directional_edge * implied_move_f * 100.0
    context = {
        "market_gate_status": "computed",
        "market_event_marker": marker,
        "market_implied_move": round(implied_move_f, 2),
        "expected_value_bps": round(expected_value_bps, 2),
        "options_iv": round(options_iv, 2) if options_iv is not None else None,
    }

    reason = None
    band = current_band
    if (
        current_band == "immediate"
        and expected_value_bps < MARKET_SIDE_GATE_EV_THRESHOLD_BPS
    ):
        band = "watchlist"
        reason = (
            f"low_ev_vs_market(expected_value_bps={expected_value_bps:.1f} "
            f"< {MARKET_SIDE_GATE_EV_THRESHOLD_BPS:.1f})"
        )
        context["market_gate_reason"] = reason
    return context, band, reason


def _resolve_catalyst_window(
    sb: SupabaseClient,
    asset_id: str,
) -> Tuple[datetime, str]:
    rows = sb._rest(
        "GET", "fda_regulatory_events",
        params={
            "select": "id,event_date,event_type,event_status",
            "asset_id": f"eq.{asset_id}",
            "event_type": f"in.({','.join(CATALYST_EVENT_TYPES)})",
            "event_status": "in.(pending,resolved)",
            "order": "event_date.asc.nullslast",
            "limit": "1",
        },
    ) or []
    if rows and rows[0].get("event_date"):
        try:
            event_date = datetime.fromisoformat(
                str(rows[0]["event_date"])[:10],
            ).replace(tzinfo=timezone.utc)
            marker = f"{rows[0].get('event_type')}:{rows[0].get('id')}"
            return event_date + timedelta(days=2), marker
        except ValueError:
            pass
    return (
        datetime.now(timezone.utc) + timedelta(days=DEFAULT_POST_MORTEM_WINDOW_DAYS),
        f"default_{DEFAULT_POST_MORTEM_WINDOW_DAYS}d_fallback",
    )


def _build_stage_10_secondaries(
    run: AssessmentRun,
    ctx: Dict[str, Any],
    parsed: Dict[str, Any],
) -> Dict[str, Any]:
    stage_metrics = [
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

    direction = parsed.get("thesis_direction") or "neutral"
    try:
        predicted_pct = float(parsed.get("conviction_pct") or 50.0)
    except (TypeError, ValueError):
        predicted_pct = 50.0

    return {
        "stage_metrics": stage_metrics,
        "hypotheses": [],
        "premortem_verdicts": [],
        "post_mortem_stub": {
            "asset_id": run.asset_id,
            "predicted_outcome": _direction_to_outcome(direction),
            "predicted_conviction_pct": max(0.0, min(100.0, predicted_pct)),
            "predicted_direction": direction,
        },
    }


def _unwrap_persist_assessment_response(response: Any) -> str:
    if isinstance(response, str):
        return response
    if isinstance(response, list) and response:
        return _unwrap_persist_assessment_response(response[0])
    if isinstance(response, dict):
        for key in ("persist_assessment_v3", "id"):
            if response.get(key):
                return str(response[key])
    raise RuntimeError(f"persist_assessment_v3 returned unexpected shape: {response!r}")


def _supersede_prior_ic_memo_best_effort(*_args: Any, **_kwargs: Any) -> None:
    """Compatibility hook for tests/older IC memo orchestration paths."""
    return None


def _maybe_trigger_ic_memo_best_effort(*_args: Any, **_kwargs: Any) -> None:
    """Compatibility hook for tests/older IC memo orchestration paths."""
    return None


def stage_10_persist(
    sb: SupabaseClient,
    asset_id: str,
    run: AssessmentRun,
    cited_prose: str,
    parsed: Dict[str, Any],
    ctx: Dict[str, Any],
    model: str,
    extractor_model: str,
    constitutional_result: Optional[ConstitutionalResult] = None,
    *,
    signal_category: Optional[str] = None,
    commercial_dimensions: Optional[Dict[str, Any]] = None,
) -> str:
    """Persist one convergence_assessment row.

    v4 stamps `orchestrator_version_v4=true` unconditionally. The legacy
    v3 hypothesis, premortem, and ensemble side payloads are no longer produced;
    their historical columns remain nullable for old rows.
    """
    fact_ids = [f["id"] for f in ctx["facts"]]
    document_ids = [d["id"] for d in ctx["documents"]]

    # Resolve short fact_ids back to full UUIDs
    short_to_full = {f["id"][:8]: f["id"] for f in ctx["facts"]}
    short_to_full_doc = {d["id"][:8]: d["id"] for d in ctx["documents"]}

    # Hydrate citations in cited_prose_blocks
    hydrated_blocks = []
    for blk in (parsed.get("cited_prose_blocks") or []):
        hydrated_blocks.append({
            "section": blk.get("section"),
            "text": blk.get("text"),
            "fact_citations": [
                short_to_full.get(s, s) for s in (blk.get("fact_citations") or [])
            ],
            "doc_citations": [
                short_to_full_doc.get(s, s) for s in (blk.get("doc_citations") or [])
            ],
        })

    hydrated_key_facts = []
    for kf in (parsed.get("key_facts") or []):
        short = kf.get("fact_id_short", "")
        hydrated_key_facts.append({
            "text": kf.get("text"),
            "fact_id": short_to_full.get(short, short),
        })

    conviction = float(parsed.get("conviction_pct") or 50.0)
    conviction = max(0.0, min(100.0, conviction))
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

    # Stage 8 — isotonic calibration if a curve is active
    active_curve = get_active_calibration_curve(sb)
    if active_curve and active_curve.get("curve_data"):
        calibrated = apply_isotonic_calibration(
            raw_conviction / 100.0, active_curve["curve_data"]) * 100.0
        calibrated = max(0.0, min(100.0, calibrated))
        calibration_curve_version: Optional[str] = active_curve.get("version")
    else:
        calibrated = raw_conviction
        calibration_curve_version = None
    band = derive_band(calibrated)
    raw_prediction_target = parsed.get("prediction_target")
    prediction_target_explicit = isinstance(raw_prediction_target, dict)
    prediction_target = _coerce_prediction_target(
        raw_prediction_target
    )
    if not prediction_target_explicit:
        ctx["prediction_target_defaulted"] = True

    market_context, band, market_gate_reason = compute_market_side_context(
        sb,
        asset_id=asset_id,
        asset=ctx.get("asset") or {},
        calibrated_conviction_pct=calibrated,
        direction=direction,
        current_band=band,
        run=run,
    )
    if market_gate_reason:
        logger.info("Market-side gate: band downgraded to %s — %s",
                    band, market_gate_reason)

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

    convergence_signature = compute_convergence_signature(
        direction=direction,
        calibrated_conviction_pct=calibrated,
        cited_prose_blocks=hydrated_blocks,
        key_facts=hydrated_key_facts,
        fact_ids=fact_ids,
        document_ids=document_ids,
    )
    duplicate_assessment_id = _find_existing_convergence_signature(
        sb,
        asset_id=asset_id,
        convergence_signature=convergence_signature,
    )
    if duplicate_assessment_id:
        logger.info(
            "Skipping duplicate convergence_signature for asset=%s existing=%s",
            asset_id, duplicate_assessment_id,
        )
        return duplicate_assessment_id

    evidence_ledger = {
        "n_facts": len(fact_ids),
        "n_documents": len(document_ids),
        "fact_types_covered": sorted(set(f["fact_type"] for f in ctx["facts"])),
        "evidence_packet": ctx.get("evidence_packet"),
        "prediction_target_explicit": prediction_target_explicit,
    }
    if market_context:
        evidence_ledger["market_side_gate"] = market_context

    claim_ledger = build_claim_ledger(
        cited_prose_blocks=hydrated_blocks,
        key_facts=hydrated_key_facts,
    )
    unsupported_claim_count = sum(
        1 for claim in claim_ledger
        if claim.get("verifier_status") != "supported"
    )
    alert_gate_reasons: List[str] = []
    if constitutional_result is not None and not constitutional_result.pass_:
        alert_gate_reasons.append("constitutional_failed")
    if evidence_quality is None or evidence_quality < 0.45:
        alert_gate_reasons.append("low_evidence_quality")
    if not prediction_target_explicit:
        alert_gate_reasons.append("prediction_target_defaulted")
    if unsupported_claim_count > 0:
        alert_gate_reasons.append("unsupported_claims_present")
    if (market_context.get("expected_value_bps") is None or
            float(market_context.get("expected_value_bps") or 0.0) <= 0.0):
        alert_gate_reasons.append("non_positive_expected_value")
    top_model_review = {
        "eligible": (
            band == "immediate"
            and not alert_gate_reasons
            and float(market_context.get("expected_value_bps") or 0.0) > 0.0
        ),
        "status": (
            "not_requested_eval_gate_required"
            if os.environ.get("ORCH_ENABLE_TOP_MODEL_FINAL_REVIEW") != "1"
            else "enabled_but_not_implemented"
        ),
        "policy": (
            "Top-model review is reserved for high-EV, alert-eligible Tier-1 "
            "cases and remains off until calibration/eval gates are live."
        ),
    }

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
        "evidence_ledger": evidence_ledger,
        "claim_ledger": claim_ledger,
        "alert_gate_status": "pass" if not alert_gate_reasons else "suppress",
        "alert_gate_reasons": alert_gate_reasons,
        "top_model_review": top_model_review,
        "reasoning_trace": cited_prose,
        "cited_prose_blocks": hydrated_blocks,
        "key_facts": hydrated_key_facts,
        "uncertainties": parsed.get("uncertainties") or [],
        "raw_conviction_pct": raw_conviction,
        "thesis_direction": direction,
        "thesis_summary": parsed.get("thesis_summary") or "",
        "target_type": prediction_target["target_type"],
        "horizon_days": prediction_target["horizon_days"],
        "event_anchor": prediction_target["event_anchor"],
        "label_rule": prediction_target["label_rule"],
        "ensemble_n": 1,
        "ensemble_runs": None,
        "ensemble_mean": conviction,
        "ensemble_dispersion": 0.0,
        "shrinkage_factor": 0.0,
        "constitutional_pass": (
            constitutional_result.pass_ if constitutional_result else None),
        "constitutional_findings": (
            [{"severity": f.severity, "check": f.check, "detail": f.detail,
              "affected_id": f.affected_id}
             for f in constitutional_result.findings]
            if constitutional_result else None),
        # PR-5: explicit gate outcome. Tier-1 rows always carry a non-NULL
        # gate_status; bulk_v0 rows set 'tier2_skipped' in tier2.py. The
        # ConstitutionalFailure aborts before persistence when deterministic
        # citation validation fails, so persisted live rows should be 'pass'.
        "gate_status": (
            ("pass" if constitutional_result.pass_ else "fail")
            if constitutional_result else "not_evaluated"
        ),
        "hypotheses": None,
        "pre_mortem": None,
        "adversarial_challenges": None,
        "pre_mortem_verdict": None,
        "surviving_hypothesis_ids": [],
        "reference_class": reference_class_value,
        "reference_class_base_rate": (
            round(base_rate_value, 3) if base_rate_value is not None else None),
        "similar_resolved_case_ids": similar_case_ids or None,
        "conviction_pct_calibrated": round(calibrated, 2),
        "calibration_curve_version": calibration_curve_version,
        "calibration_status": (
            "applied" if calibration_curve_version else "no_active_curve"
        ),
        "conviction_pct": round(calibrated, 2),
        "evidence_quality": evidence_quality,
        "band": band,
        "market_implied_move": market_context.get("market_implied_move"),
        "expected_value_bps": market_context.get("expected_value_bps"),
        "options_iv": market_context.get("options_iv"),
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_thinking_tokens": total_thinking,
        "total_cache_read_tokens": total_cache_read,
        "total_cache_creation_tokens": total_cache_create,
        "cost_usd": round(total_cost, 4),
        "latency_ms": total_latency,
        # PR-2 content-aware dedup: stamp the asset's current material-primary
        # document set hash so the reactor can suppress re-enqueues whose evidence
        # set is unchanged from the last completed synthesis.
        "document_set_hash": compute_document_set_hash(sb, asset_id),
        "convergence_signature": convergence_signature,
        "orchestrator_version_v4": True,
        "signal_category": signal_category,
        "commercial_dimensions": commercial_dimensions,
    }

    secondaries = _build_stage_10_secondaries(
        run,
        ctx,
        {**parsed, "conviction_pct": calibrated, "thesis_direction": direction},
    )
    outcome_window_end, catalyst_marker = _resolve_catalyst_window(sb, asset_id)
    secondaries["post_mortem_stub"]["predicted_conviction_pct"] = calibrated
    secondaries["post_mortem_stub"]["outcome_window_end"] = outcome_window_end.isoformat()
    secondaries["post_mortem_stub"]["catalyst_resolution_marker"] = catalyst_marker

    rpc_payload = {
        "orchestrator_run_id": run.orchestrator_run_id,
        "assessment": row,
        **secondaries,
    }
    assessment_id = _unwrap_persist_assessment_response(
        sb._rest(
            "POST", "rpc/persist_assessment_v3",
            json_body={"payload": rpc_payload},
            prefer="return=representation",
        )
    )

    # Stream 3.4: write a distilled asset-scope memory blob so the next
    # assessment of this asset starts with the prior thesis summary in
    # context. We use the Stage 9 reasoning_summary plus headline metadata —
    # not the full prose — so the memory file stays compact (<2KB) and the
    # 1h-TTL system block A doesn't bloat. Best-effort: a write failure does
    # not block the assessment from returning.
    try:
        # D-123 C5: read prior memory, then append-merge a new entry into
        # ## Recent assessments (idempotent on assessment_id, capped at 5).
        store = MemoryStore(sb)
        prior_blobs = store.load_all(asset_id=asset_id)
        prior_text = (prior_blobs.asset or "") if prior_blobs else ""
        memory_summary = _build_asset_memory_summary(
            asset=ctx["asset"],
            parsed=parsed,
            cited_prose=cited_prose,
            conviction_calibrated=calibrated,
            band=band,
            direction=direction,
            assessment_id=assessment_id,
            prior_text=prior_text,
        )
        store.write(
            scope="asset",
            scope_id=asset_id,
            content=memory_summary,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("memory writeback failed for asset=%s: %s", asset_id, exc)

    return assessment_id


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
{anchor_section}
## Structured fact layer ({len(facts)} facts, ranked by confidence then \
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
            dry_run: bool = False,
            run_id: Optional[str] = None,
            hard_kill_usd: Optional[float] = 15.0,
            parsed_out: Optional[Dict[str, Any]] = None) -> Optional[str]:
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
            dry_run, parsed_out,
        )
    finally:
        if hard_kill_usd is not None:
            a_client.detach_budget()


def _run_one_inner(sb: SupabaseClient, a_client: OrchestratorClient,
                   asset_id: str, trigger_type: str,
                   model: str, extractor_model: str,
                   dry_run: bool,
                   parsed_out: Optional[Dict[str, Any]] = None) -> Optional[str]:
    logger.info(
        "v4 path active: commercial dual-mandate prompts; retired "
        "stage 2/3/6/semantic-7 code paths removed",
    )

    run = AssessmentRun(asset_id=asset_id, trigger_type=trigger_type)

    logger.info("=== Stage 0: load context ===")
    ctx = stage_0_load(sb, asset_id)
    evidence_packet = require_tier1_evidence_packet(ctx)
    asset = ctx["asset"]
    logger.info("Asset: %s / %s (%s, %s); facts=%d, docs=%d",
                asset.get("ticker"), asset.get("drug_name"),
                asset.get("indication"), asset.get("application_number") or "no_app#",
                len(ctx["facts"]), len(ctx["documents"]))
    logger.info("Evidence packet ok: %s", evidence_packet.get("counts"))

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

    logger.info("=== Stage 1: synthesis (%s) ===", model)
    cited_prose, m1 = stage_1_synthesize(
        a_client, ctx, model, system_prompt=STAGE_1_SYSTEM,
    )
    run.stage_metrics.append(m1)
    logger.info("Stage 1: %dms / %d in / %d out / $%.3f",
                m1.latency_ms, m1.input_tokens, m1.output_tokens, m1.cost_usd)

    logger.info("=== Stage 9: structured extraction (%s) ===", extractor_model)
    parsed, m9 = stage_9_extract(
        a_client, cited_prose, extractor_model,
        system_prompt=STAGE_9_SYSTEM,
    )
    run.stage_metrics.append(m9)
    if not parsed:
        logger.error("Stage 9 failed to parse JSON; aborting")
        raise Stage9ParseError("Stage 9 failed to parse JSON")
    logger.info("Stage 9: %dms / %d in / %d out / $%.3f / direction=%s conviction=%s",
                m9.latency_ms, m9.input_tokens, m9.output_tokens, m9.cost_usd,
                parsed.get("thesis_direction"), parsed.get("conviction_pct"))

    logger.info("=== Stage 7: deterministic citation validation ===")
    constitutional_result = _validate_citations(
        cited_prose=cited_prose,
        facts=ctx["facts"],
        document_ids=[d["id"] for d in ctx["documents"]],
    )
    run.stage_metrics.append(StageMetric(
        stage_name="stage_7_citation_validation",
        model="deterministic",
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
    logger.info(
        "Stage 7: pass=%s findings=%d (citations: %d/%d resolved)",
        constitutional_result.pass_, len(constitutional_result.findings),
        constitutional_result.n_citations_resolved,
        constitutional_result.n_citations_checked,
    )
    if not constitutional_result.pass_:
        raise ConstitutionalFailure(constitutional_result.findings)

    # Phase 4A (D-127): expose parsed payload to the replay harness before
    # the persistence gate. dict.update() preserves the caller's reference
    # so they can read it after run_one returns.
    if parsed_out is not None:
        parsed_out.clear()
        parsed_out.update(parsed)

    if dry_run:
        logger.info("[dry-run] would persist; assessment summary:")
        logger.info("  thesis_direction: %s", parsed.get("thesis_direction"))
        logger.info("  conviction_pct: %s", parsed.get("conviction_pct"))
        logger.info("  evidence_quality: %s", parsed.get("evidence_quality"))
        logger.info("  thesis_summary: %s", parsed.get("thesis_summary"))
        logger.info("  band: %s", derive_band(float(parsed.get("conviction_pct") or 50.0)))
        logger.info("  citation_validation_pass: %s", constitutional_result.pass_)
        return None

    logger.info("=== Stage 10: persist ===")
    # Phase 4 will refine signal_category to use the actual scanner emitter
    # category; until then trigger_type is a coarse fallback.
    commercial = parsed.get("commercial_dimensions")
    signal_category = run.trigger_type
    assessment_id = stage_10_persist(
        sb, asset_id, run, cited_prose, parsed, ctx, model, extractor_model,
        constitutional_result=constitutional_result,
        signal_category=signal_category,
        commercial_dimensions=commercial,
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
        dry_run=args.dry_run,
    )
    return 0 if (aid is not None or args.dry_run) else 1


if __name__ == "__main__":
    sys.exit(main())
