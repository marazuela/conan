"""asset_linker_gold_set labeling — Phases 1 (bootstrap) and 4 (continuous eval).

Two modes:

  bootstrap (default; label_gold_set)
    Stratified-by-source sampling of recent documents (target ~500 docs),
    Sonnet 4.6 labeled against the active fda_assets universe with prompt
    caching. Idempotent on doc_id — rerunnable to refresh the gold set when
    the universe shape changes. ~$3–15 per run depending on universe size.

  incremental (daily; daily_incremental_label)
    Sample 50 random new docs ingested in the last 24h, skip any already
    in asset_linker_gold_set, label with Sonnet 4.6. Designed for Phase 4
    continuous-eval — catches recall drift when (a) the SOURCE_ALLOWLIST
    becomes wrong as new approved-stage assets enter the universe, or (b)
    pass-1 model behavior changes. ~$1–3 per daily run.

Design rationale:
  - Primary labeler is Sonnet 4.6 (single-pass) with prompt caching on the
    asset universe to keep cost low. Opus 4.7 would give marginally better
    labels but at ~5× cost.
  - Second-pass arbitration (Opus 4.7 on docs Sonnet labeled positive) is
    available via the `arbitrate=True` arg but defaults off to keep cost
    near the daily budget.
  - Prompt caching: the fda_assets universe is placed in the first content
    block with cache_control. Cache reads at 10% of base input cost amortize
    the universe across each batch.

Run (Modal):
  modal deploy modal_workers/label_asset_linker_gold_set_app.py
  # bootstrap (~500 docs, ~$3-15)
  modal run modal_workers/label_asset_linker_gold_set_app.py::main \\
      --target-count 500 [--arbitrate]
  # incremental (~50 docs, ~$1-3, idempotent on doc_id)
  modal run modal_workers/label_asset_linker_gold_set_app.py::main_incremental \\
      --target-count 50

Automation: the @app.function decorator on daily_incremental_label includes
`schedule=modal.Cron("0 3 * * *")` (03:00 UTC). If the Modal free-tier
5-schedules cap blocks deploy, fall back to pg_cron + a web endpoint — see
the GitHub issue spec referenced from plans/asset-linker-yield-optimization.md.

Cost projection (Sonnet 4.6 only, prompt-cached 35-asset universe):
  - Bootstrap (500 docs):     ~$3.50 total
  - Incremental (50 docs):    ~$0.50/day
  - With --arbitrate (Opus on ~10% positives): +$1-2
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
def _incremental_sample(sb, target_count: int = 50, days_back: int = 1) -> List[Dict[str, Any]]:
    """Sample N random new docs ingested in last `days_back` days, skipping
    docs already in asset_linker_gold_set. Used by the daily continuous-eval
    cron. Distribution-agnostic by design — we want drift signal from any
    source, including newly-eligible ones.
    """
    from datetime import timedelta
    since = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
    # Pull more than needed, filter against gold set in Python (PostgREST NOT IN
    # against another table is awkward; over-fetch is fine at this size).
    over_fetch = target_count * 4
    params = {
        "select": "id,source,title,raw_text",
        "fetched_at": f"gte.{since}",
        "order": "fetched_at.desc",
        "limit": str(over_fetch),
    }
    try:
        candidates = sb._rest("GET", "documents", params=params) or []
    except Exception as e:
        print(f"WARN: incremental sample fetch failed: {e}", flush=True)
        return []

    # Skip already-labeled docs
    existing_params = {
        "select": "doc_id",
        "labeled_at": f"gte.{since}",
        "limit": "10000",
    }
    try:
        existing_rows = sb._rest("GET", "asset_linker_gold_set", params=existing_params) or []
    except Exception:
        existing_rows = []
    existing_ids = {r["doc_id"] for r in existing_rows}

    import random
    pool = [c for c in candidates if c["id"] not in existing_ids]
    random.shuffle(pool)
    picked = pool[:target_count]

    rows: List[Dict[str, Any]] = []
    for row in picked:
        text = row.get("raw_text") or ""
        rows.append({
            "id": row["id"],
            "source": row["source"],
            "title": row.get("title") or "",
            "text_window": text[:DOC_TEXT_MAX_CHARS],
            "text_truncated": len(text) > DOC_TEXT_MAX_CHARS,
        })
    by_source: Dict[str, int] = {}
    for r in rows:
        by_source[r["source"]] = by_source.get(r["source"], 0) + 1
    print(f"incremental sample: {len(rows)} docs by source: {by_source}", flush=True)
    return rows


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


@app.function(
    image=image,
    timeout=3_600,
    secrets=[anthropic_secrets, supabase_secrets],
    schedule=modal.Cron("0 3 * * *"),
)
def daily_incremental_label(target_count: int = 50, arbitrate: bool = False) -> Dict[str, Any]:
    """Daily continuous-eval — sample 50 random new docs from last 24h that
    aren't already in the gold set, label with Sonnet 4.6, persist. Designed
    to catch recall drift when the SOURCE_ALLOWLIST or pass-1 model behavior
    becomes wrong as the universe evolves.

    Schedule: 03:00 UTC daily via @modal.function(schedule=...). If the Modal
    free-tier 5-schedules cap blocks this, fall back to pg_cron + an HTTP
    endpoint — see GitHub issue tracked from
    plans/asset-linker-yield-optimization.md.

    Budget: ~$0.50–2/day on 35-asset universe; well under the $5/day cap.
    """
    import anthropic
    from modal_workers.shared.supabase_client import SupabaseClient

    sb = SupabaseClient()
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    docs = _incremental_sample(sb, target_count=target_count, days_back=1)
    if not docs:
        return {"counts": {"labeled": 0, "reason": "no_new_docs"}, "usage": {}}

    universe = _load_asset_universe(sb)
    universe_block = _format_universe_for_prompt(universe)

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

    return {"counts": counts, "usage": total_usage, "universe_size": len(universe)}


@app.local_entrypoint()
def main(target_count: int = 500, arbitrate: bool = False, dry_run: bool = False):
    """Local entrypoint — run via `modal run`."""
    out = label_gold_set.remote(target_count=target_count, arbitrate=arbitrate, dry_run=dry_run)
    print(json.dumps(out, indent=2, default=str))


@app.local_entrypoint()
def main_incremental(target_count: int = 50, arbitrate: bool = False):
    """Local entrypoint for the daily incremental — useful for ad-hoc runs."""
    out = daily_incremental_label.remote(target_count=target_count, arbitrate=arbitrate)
    print(json.dumps(out, indent=2, default=str))
