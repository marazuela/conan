"""Orchestrator runtime — MVP single-stage assessment.

Reads (asset, extracted_facts, key documents, market context, prior memory)
and emits one `convergence_assessments` row.

This is a SIMPLIFIED v0.1 implementation of the plan's 10-stage pipeline. The
fully-built pipeline (Stages 0-10 with ensemble + critique + isotonic
calibration + memory tool + interleaved thinking + Citations API) is the
next-iteration deliverable. v0.1 demonstrates the core synthesis loop
end-to-end on the VRDN MVP.

What v0.1 includes:
  Stage 0  — load asset metadata + extracted_facts (no full memory hierarchy)
  Stage 1  — Sonnet synthesis (cited prose, fact_id-anchored)
  Stage 9  — Sonnet structured-output extraction → schema-validated JSON
  Stage 10 — write convergence_assessments row + post_mortem_queue stub

What v0.1 skips (next iteration):
  Stage 2-3 — hypothesis enumeration + adversarial pre-mortem
  Stage 4   — reference-class anchoring (compute-mcp tools)
  Stage 6   — N=7 Batch ensemble + dispersion
  Stage 7   — Sonnet constitutional pass with citation-resolution check
  Stage 8   — isotonic calibration (no curve fitted yet)

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

from modal_workers.shared.supabase_client import SupabaseClient
from orchestrator_runtime.client import (
    DEFAULT_EXTRACTOR_MODEL,
    DEFAULT_MODEL,
    OrchestratorClient,
    estimate_cost,
    parse_json_or_none,
)

logger = logging.getLogger(__name__)

ORCHESTRATOR_VERSION = "orch-v0.1.0-mvp"

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
                "select": "id,source,doc_type,title,url,published_at,raw_text,extensions",
                "id": f"in.({ids_filter})",
            },
        ) or []
        # Preserve order from asset_docs (newest first)
        by_id = {r["id"]: r for r in rows}
        docs = [by_id[did] for did in doc_ids if did in by_id]

    # Memory file (if exists) — currently optional/empty for MVP
    memory_text: Optional[str] = None
    memory_path = asset.get("memory_path")
    if memory_path:
        try:
            blob = client.read_cache("memory", memory_path.lstrip("/"))
            if blob:
                memory_text = blob.decode("utf-8", errors="replace")
        except Exception as exc:
            logger.debug("memory file %s not found: %s", memory_path, exc)

    return {
        "asset": asset,
        "facts": facts,
        "documents": docs,
        "memory_text": memory_text,
        "asset_doc_links": asset_docs,
    }


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
) -> tuple[str, StageMetric]:
    asset = ctx["asset"]
    facts = ctx["facts"]
    docs = ctx["documents"]
    memory_text = ctx["memory_text"]

    # Format facts compactly
    facts_section = "\n".join(
        f"- F:{f['id'][:8]} ({f['fact_type']}, conf={f.get('confidence')}, "
        f"doc=D:{f['document_id'][:8]}): {f['fact_text']}\n"
        f"  evidence: \"{f['evidence_quote'][:300]}\""
        for f in facts
    )

    # Format document excerpts (head + tail of each, char-budgeted)
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

    user_content = f"""Tracked asset:
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

Document window: last 180 days (most recent {len(docs)} material documents \
shown below; full set has more)

## Structured fact layer ({len(facts)} facts, ranked by confidence then \
recency)

{facts_section}

## Document excerpts ({len(docs)} documents, head-only excerpts)

{docs_section}{memory_section}

Produce the cited prose synthesis per the system prompt. End with the \
Conclusion section in the exact format specified."""

    result = a_client.call(
        system=STAGE_1_SYSTEM,
        messages=[{"role": "user", "content": user_content}],
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


def stage_9_extract(
    a_client: OrchestratorClient,
    cited_prose: str,
    model: str,
) -> tuple[Optional[Dict[str, Any]], StageMetric]:
    user_content = f"Cited prose to extract:\n\n{cited_prose}"
    result = a_client.call(
        system=STAGE_9_SYSTEM,
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


def stage_10_persist(
    sb: SupabaseClient,
    asset_id: str,
    run: AssessmentRun,
    cited_prose: str,
    parsed: Dict[str, Any],
    ctx: Dict[str, Any],
    model: str,
    extractor_model: str,
) -> str:
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

    band = derive_band(conviction)

    total_input = sum(m.input_tokens for m in run.stage_metrics)
    total_output = sum(m.output_tokens for m in run.stage_metrics)
    total_thinking = sum(m.thinking_tokens for m in run.stage_metrics)
    total_cache_read = sum(m.cache_read_tokens for m in run.stage_metrics)
    total_cache_create = sum(m.cache_creation_tokens for m in run.stage_metrics)
    total_cost = sum(m.cost_usd for m in run.stage_metrics)
    total_latency = sum(m.latency_ms for m in run.stage_metrics)

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
        },
        "reasoning_trace": cited_prose,
        "cited_prose_blocks": hydrated_blocks,
        "key_facts": hydrated_key_facts,
        "uncertainties": parsed.get("uncertainties") or [],
        "raw_conviction_pct": conviction,
        "thesis_direction": direction,
        "thesis_summary": parsed.get("thesis_summary") or "",
        "ensemble_n": 1,            # MVP single-shot
        "ensemble_mean": conviction,
        "ensemble_dispersion": 0.0,
        "shrinkage_factor": 0.0,
        "constitutional_pass": None,    # MVP skips Stage 7
        "conviction_pct_calibrated": conviction,  # MVP skips Stage 8 isotonic
        "conviction_pct": conviction,
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

    rows = sb._rest(
        "POST", "convergence_assessments",
        json_body=row,
        prefer="return=representation",
    )
    if not rows:
        raise RuntimeError("Failed to insert convergence_assessments row")
    assessment_id = rows[0]["id"]

    # Per-stage metrics rows
    for m in run.stage_metrics:
        sb._rest(
            "POST", "assessment_stage_metrics",
            json_body={
                "assessment_id": assessment_id,
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
            },
            prefer="return=minimal",
        )

    # Post-mortem queue stub — outcome resolves at PDUFA date.
    # For VRDN MVP, PDUFA is 2026-06-30. We pull from the asset's pending FDA
    # event row if available; otherwise default to +60d.
    pdufa_rows = sb._rest(
        "GET", "fda_regulatory_events",
        params={
            "select": "event_date",
            "asset_id": f"eq.{asset_id}",
            "event_type": "eq.pdufa",
            "event_status": "eq.pending",
            "order": "event_date.asc.nullslast",
            "limit": "1",
        },
    ) or []
    if pdufa_rows and pdufa_rows[0].get("event_date"):
        outcome_window_end = (
            datetime.fromisoformat(pdufa_rows[0]["event_date"]).replace(tzinfo=timezone.utc)
            + timedelta(days=2)
        )
    else:
        outcome_window_end = datetime.now(timezone.utc) + timedelta(days=60)

    sb._rest(
        "POST", "post_mortem_queue",
        json_body={
            "assessment_id": assessment_id,
            "asset_id": asset_id,
            "predicted_outcome": _direction_to_outcome(direction),
            "predicted_conviction_pct": conviction,
            "predicted_direction": direction,
            "outcome_window_end": outcome_window_end.isoformat(),
        },
        prefer="return=minimal",
    )

    return assessment_id


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

def run_one(sb: SupabaseClient, a_client: OrchestratorClient,
            asset_id: str, trigger_type: str = "manual",
            model: str = DEFAULT_MODEL,
            extractor_model: str = DEFAULT_EXTRACTOR_MODEL,
            dry_run: bool = False) -> Optional[str]:
    run = AssessmentRun(asset_id=asset_id, trigger_type=trigger_type)

    logger.info("=== Stage 0: load context ===")
    ctx = stage_0_load(sb, asset_id)
    asset = ctx["asset"]
    logger.info("Asset: %s / %s (%s, %s); facts=%d, docs=%d",
                asset.get("ticker"), asset.get("drug_name"),
                asset.get("indication"), asset.get("application_number") or "no_app#",
                len(ctx["facts"]), len(ctx["documents"]))

    logger.info("=== Stage 1: Sonnet synthesis (%s) ===", model)
    cited_prose, m1 = stage_1_synthesize(a_client, ctx, model)
    run.stage_metrics.append(m1)
    logger.info("Stage 1: %dms / %d in / %d out / $%.3f",
                m1.latency_ms, m1.input_tokens, m1.output_tokens, m1.cost_usd)

    logger.info("=== Stage 9: structured extraction (%s) ===", extractor_model)
    parsed, m9 = stage_9_extract(a_client, cited_prose, extractor_model)
    run.stage_metrics.append(m9)
    if not parsed:
        logger.error("Stage 9 failed to parse JSON; aborting")
        return None
    logger.info("Stage 9: %dms / %d in / %d out / $%.3f / direction=%s conviction=%s",
                m9.latency_ms, m9.input_tokens, m9.output_tokens, m9.cost_usd,
                parsed.get("thesis_direction"), parsed.get("conviction_pct"))

    if dry_run:
        logger.info("[dry-run] would persist; printing assessment summary:")
        logger.info("  thesis_direction: %s", parsed.get("thesis_direction"))
        logger.info("  conviction_pct: %s", parsed.get("conviction_pct"))
        logger.info("  evidence_quality: %s", parsed.get("evidence_quality"))
        logger.info("  thesis_summary: %s", parsed.get("thesis_summary"))
        logger.info("  band: %s", derive_band(float(parsed.get("conviction_pct") or 50.0)))
        return None

    logger.info("=== Stage 10: persist ===")
    assessment_id = stage_10_persist(
        sb, asset_id, run, cited_prose, parsed, ctx, model, extractor_model)
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
