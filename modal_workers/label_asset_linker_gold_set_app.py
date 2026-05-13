"""asset_linker_gold_set labeling — Phase 1 of yield-optimization plan.

Stratified-by-source sampling of recent documents, scored by Sonnet 4.6 against
the active fda_assets universe. Output rows land in asset_linker_gold_set.

Design rationale:
  - Primary labeler is Sonnet 4.6 (single-pass) with prompt caching on the
    asset universe to keep cost low. Opus 4.7 would give marginally better
    labels but at ~5× cost — for a 500-doc gold set used to differentiate
    a 5× yield gap between Haiku and Sonnet, Sonnet 4.6's resolution is more
    than enough.
  - Second-pass arbitration (Opus 4.7 on docs Sonnet labeled positive) is
    available via the `arbitrate=True` arg but defaults off to keep the
    initial run at the ~$15 budget Pedro approved.
  - Prompt caching: the fda_assets universe (~2000 rows ≈ 25k tokens) is
    placed in the first content block with cache_control. Cache reads at
    10% of base input cost amortize the universe across 500 calls.

Run (Modal):
  modal deploy modal_workers/label_asset_linker_gold_set_app.py
  modal run modal_workers/label_asset_linker_gold_set_app.py::label_gold_set \\
      --target-count 500 [--arbitrate]

Cost projection (Sonnet 4.6 only, prompt-cached universe):
  - 1× cache write   ~25k tokens × $3.75/M  = $0.09
  - 500× cache reads ~25k tokens × $0.30/M  = $3.75
  - 500× per-doc    ~5k tokens × $3.00/M    = $7.50
  - 500× outputs    ~250 tokens × $15.00/M  = $1.88
  - Total                                   ≈ $13.22

With --arbitrate (Opus 4.7 second pass on ~30% positive subset, ~150 docs):
  - 150× Opus 4.7 calls ≈ +$15
  - Total                                   ≈ $28
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import modal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Modal app + image
# ---------------------------------------------------------------------------
app = modal.App("conan-asset-linker-gold-set-labeler")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "anthropic>=0.43.0",
        "httpx>=0.27",
        "pydantic>=2.6",
        "requests>=2.31",
    )
    .add_local_python_source("modal_workers")
)

anthropic_secrets = modal.Secret.from_name("anthropic-orchestrator")
supabase_secrets = modal.Secret.from_name("supabase-secrets")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
PRIMARY_MODEL = "claude-sonnet-4-6"
ARBITRATOR_MODEL = "claude-opus-4-7-20260101"

# Doc text we send the model. Most docs are short; for huge docs (EDGAR
# 10-K, PDFs) we send a window. The model is told this is a window so it
# doesn't over-call "no asset mentioned" on truncated text.
DOC_TEXT_MAX_CHARS = 12_000

# Stratification: which sources, how many each. Total target = sum.
# Source names match documents.source as live in Supabase (verified 2026-05-13).
# Weights reflect the actual 7d corpus distribution: dailymed dominates (6170/wk)
# and is the prefilter leak that burned $40 on 2026-05-11 — over-sampled here so
# we can characterize the noise floor precisely.
STRATIFICATION = {
    "dailymed":          150,
    "edgar":             100,
    "openfda":           100,
    "federal_register":   75,
    "clinicaltrials":     75,
}

LABELING_SYSTEM_PROMPT = """You are labeling documents for a high-precision ground-truth set used to evaluate an automated asset-linker pipeline.

Your job: given a document (title + text) and a universe of tracked FDA-regulated drug-development assets, return a JSON list of every asset the document *materially* discusses.

CRITICAL DEFINITIONS:
- "Materially discusses" means the document substantively names the asset's drug, ticker, sponsor, or trial in a way relevant to its FDA / regulatory / clinical trajectory. It does NOT include:
  - Generic mentions in lists (e.g. "various biotech companies")
  - Boilerplate disclosure of holdings without analysis
  - Competitor mentions only in passing comparison
- A "primary" link means the document is principally about that asset.
- A "mentions" link means the asset is named substantively but is not the main subject.
- A "pipeline_context" link means the asset is named as part of the sponsor's broader pipeline.
- A "safety_signal" link means the document describes adverse events / clinical safety related to the asset.
- A "literature" link means the document is a paper / abstract / press release primarily reporting on the asset.

OUTPUT FORMAT (strict JSON):
{
  "links": [
    {"asset_id": "<uuid>", "link_type": "primary|mentions|pipeline_context|safety_signal|literature", "span": "<short excerpt from the doc justifying this link>"}
  ],
  "reasoning": "<one-sentence rationale>"
}

If the document mentions NO tracked asset, return {"links": [], "reasoning": "..."}.
Be conservative — false positives are more harmful than false negatives at this stage."""


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------
def _stratified_sample(sb, days_back: int = 7) -> List[Dict[str, Any]]:
    """Pull ~500 docs stratified by source from last N days.

    Returns rows shaped {id, source, title, text_window, text_truncated}.

    Uses PostgREST `params=` dict (not path query-string) so the `+` in
    ISO-8601 timezone offsets doesn't get URL-encoded as a space, which
    is what swallowed the first dry-run.
    """
    from datetime import timedelta
    since = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
    rows: List[Dict[str, Any]] = []
    for source, target in STRATIFICATION.items():
        params = {
            "select": "id,source,title,raw_text,fetched_at",
            "source": f"eq.{source}",
            "fetched_at": f"gte.{since}",
            "order": "fetched_at.desc",
            "limit": str(target * 3),  # over-sample, we'll trim
        }
        try:
            r = sb._rest("GET", "documents", params=params) or []
        except Exception as e:
            print(f"WARN: source={source} sample failed: {e}", flush=True)
            r = []
        for row in r[:target]:
            text = row.get("raw_text") or ""
            rows.append({
                "id": row["id"],
                "source": row["source"],
                "title": row.get("title") or "",
                "text_window": text[:DOC_TEXT_MAX_CHARS],
                "text_truncated": len(text) > DOC_TEXT_MAX_CHARS,
            })
        print(f"sampled {len(r[:target])}/{target} for source={source}", flush=True)
    return rows


def _load_asset_universe(sb) -> List[Dict[str, Any]]:
    """All active fda_assets as compact dicts for the prompt."""
    params = {
        "select": "id,ticker,drug_name,generic_name,sponsor_name,indication",
        "is_active": "eq.true",
        "limit": "5000",
    }
    rows = sb._rest("GET", "fda_assets", params=params) or []
    return rows


def _format_universe_for_prompt(assets: List[Dict[str, Any]]) -> str:
    """Compact one-line-per-asset format. Token-efficient."""
    lines = []
    for a in assets:
        bits = [
            f"id={a['id']}",
            f"tk={a.get('ticker') or '-'}",
            f"drug={a.get('drug_name') or '-'}",
            f"gen={a.get('generic_name') or '-'}",
            f"sponsor={a.get('sponsor_name') or '-'}",
            f"ind={a.get('indication') or '-'}",
        ]
        lines.append(" | ".join(bits))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Labeling
# ---------------------------------------------------------------------------
def _label_one_doc(
    client,
    doc: Dict[str, Any],
    universe_block: str,
    model: str,
) -> Dict[str, Any]:
    """Single Anthropic call with prompt-cached universe + per-doc question."""
    truncation_note = (
        f"\n[Note: document text truncated to first {DOC_TEXT_MAX_CHARS} characters.]"
        if doc["text_truncated"] else ""
    )
    user_text = (
        f"DOCUMENT (source={doc['source']}, id={doc['id']}):\n"
        f"TITLE: {doc['title']}\n\n"
        f"TEXT:\n{doc['text_window']}{truncation_note}\n\n"
        f"Return the JSON object specified in the system prompt."
    )
    resp = client.messages.create(
        model=model,
        max_tokens=1024,
        system=[
            {
                "type": "text",
                "text": LABELING_SYSTEM_PROMPT,
            },
            {
                "type": "text",
                "text": f"TRACKED ASSET UNIVERSE (one per line):\n{universe_block}",
                "cache_control": {"type": "ephemeral"},
            },
        ],
        messages=[{"role": "user", "content": user_text}],
    )
    text = "".join(b.text for b in resp.content if hasattr(b, "text"))
    # Parse JSON tolerantly — model sometimes wraps in ```json
    text = text.strip()
    if text.startswith("```"):
        # strip code fence
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        parsed = json.loads(text)
    except Exception as e:
        logger.warning("doc=%s parse failed: %s; raw=%r", doc["id"], e, text[:200])
        parsed = {"links": [], "reasoning": f"parse_error: {e}"}
    return {
        "doc": doc,
        "parsed": parsed,
        "usage": {
            "input_tokens": getattr(resp.usage, "input_tokens", 0),
            "output_tokens": getattr(resp.usage, "output_tokens", 0),
            "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", 0),
            "cache_creation_input_tokens": getattr(resp.usage, "cache_creation_input_tokens", 0),
        },
    }


# ---------------------------------------------------------------------------
# Persist
# ---------------------------------------------------------------------------
def _persist(sb, doc: Dict[str, Any], parsed: Dict[str, Any], labeler_models: List[str], confidence: str) -> None:
    links = parsed.get("links") or []
    true_asset_ids = [link["asset_id"] for link in links if link.get("asset_id")]
    spans = [
        {"asset_id": link["asset_id"], "link_type": link.get("link_type"), "span": link.get("span")}
        for link in links if link.get("asset_id")
    ]
    row = {
        "doc_id": doc["id"],
        "true_asset_ids": true_asset_ids,
        "confidence": confidence,
        "spans": spans,
        "labeler_models": labeler_models,
        "source_at_sample": doc["source"],
        "notes": parsed.get("reasoning"),
    }
    # Upsert in case we re-run (PRIMARY KEY on doc_id)
    sb._rest(
        "POST",
        "asset_linker_gold_set?on_conflict=doc_id",
        json_body=[row],
        prefer="resolution=merge-duplicates,return=minimal",
    )


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------
@app.function(
    image=image,
    timeout=14_400,  # 4h ceiling — labeling is API-bound, not compute
    secrets=[anthropic_secrets, supabase_secrets],
)
def label_gold_set(target_count: int = 500, arbitrate: bool = False, dry_run: bool = False) -> Dict[str, Any]:
    """Sample + label + persist.

    Args:
      target_count: approx total docs to label. Stratification adds up to ~500;
                    if smaller, proportionally scaled.
      arbitrate:    if True, run Opus 4.7 second-pass on any doc Sonnet labeled
                    with non-empty `links`. Disagreements → confidence='low'.
      dry_run:      sample + load universe but don't call the model or persist.
    """
    import anthropic
    from modal_workers.shared.supabase_client import SupabaseClient

    sb = SupabaseClient()
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    docs = _stratified_sample(sb)
    if target_count and target_count < len(docs):
        docs = docs[:target_count]
    universe = _load_asset_universe(sb)
    universe_block = _format_universe_for_prompt(universe)

    logger.info(
        "labeling: docs=%d, universe=%d assets, universe_chars=%d, arbitrate=%s, dry_run=%s",
        len(docs), len(universe), len(universe_block), arbitrate, dry_run,
    )

    if dry_run:
        return {
            "docs_sampled": len(docs),
            "universe_size": len(universe),
            "universe_chars": len(universe_block),
            "by_source": {s: sum(1 for d in docs if d["source"] == s) for s in STRATIFICATION},
            "dry_run": True,
        }

    counts = {"labeled": 0, "positive": 0, "negative": 0, "parse_error": 0, "arbitrated": 0, "disagreement": 0}
    total_usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}

    for doc in docs:
        primary = _label_one_doc(client, doc, universe_block, PRIMARY_MODEL)
        for k, v in primary["usage"].items():
            total_usage[k] += v
        parsed = primary["parsed"]
        labeler_models = [PRIMARY_MODEL]
        confidence = "high"

        if parsed.get("reasoning", "").startswith("parse_error"):
            counts["parse_error"] += 1
            confidence = "low"
        elif parsed.get("links"):
            counts["positive"] += 1
            if arbitrate:
                second = _label_one_doc(client, doc, universe_block, ARBITRATOR_MODEL)
                for k, v in second["usage"].items():
                    total_usage[k] += v
                counts["arbitrated"] += 1
                primary_ids = sorted(l["asset_id"] for l in parsed["links"])
                second_ids = sorted(l["asset_id"] for l in second["parsed"].get("links", []))
                if primary_ids != second_ids:
                    counts["disagreement"] += 1
                    confidence = "low"
                labeler_models.append(ARBITRATOR_MODEL)
        else:
            counts["negative"] += 1

        _persist(sb, doc, parsed, labeler_models, confidence)
        counts["labeled"] += 1
        if counts["labeled"] % 25 == 0:
            logger.info("progress: %s, usage=%s", counts, total_usage)

    return {"counts": counts, "usage": total_usage, "universe_size": len(universe)}


@app.local_entrypoint()
def main(target_count: int = 500, arbitrate: bool = False, dry_run: bool = False):
    """Local entrypoint — run via `modal run`."""
    out = label_gold_set.remote(target_count=target_count, arbitrate=arbitrate, dry_run=dry_run)
    print(json.dumps(out, indent=2, default=str))
