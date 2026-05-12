"""Per-document Sonnet fact extractor.

Reads `documents` linked to `fda_assets` (via `asset_documents` with
`is_material=true`) and extracts structured FDA-relevant facts. One row per
fact in `extracted_facts` with `evidence_quote` (verbatim) + `citation_span`.

Why a separate per-doc pass instead of the orchestrator reading raw docs:
plan §"Two-tier cross-doc reasoning at scale" — extracting once and indexing
the structured layer beats single-shot 1M-context dumps. The orchestrator
Stage 1 reads from `extracted_facts` (cheap) and only falls back to raw
documents via the `fetch_full_document` tool when needed.

Schema (matches plan + Pydantic on output):
  fact_type    text        e.g. 'pdufa_date','adcom_vote','phase3_endpoint',
                                'safety_signal','insider_buy',
                                'pipeline_disclosure','fda_correspondence',
                                'efficacy_result','enrollment','sponsor_change',
                                'competitive_mention','regulatory_pathway'
  fact_text    text        structured representation (1 sentence)
  evidence_quote text      verbatim quote from doc (≤500 chars)
  citation_span jsonb      {start, end, page?}
  confidence   numeric(3,2)

Run:
  ANTHROPIC_API_KEY=... SUPABASE_URL=... \\
    python3 -m modal_workers.extractor.sonnet_fact_extractor \\
        [--asset-id <uuid>] [--max N] [--budget-usd 30] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import anthropic

from modal_workers.shared.supabase_client import SupabaseClient

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-5-20250929"

# Per-doc context cap. For huge 10-Ks we chunk the linked spans + their
# surrounding context to stay under this.
MAX_DOC_TOKENS = 100_000
SPAN_CONTEXT_CHARS = 6_000

# Fact type whitelist — unknown fact_types from the model are dropped.
VALID_FACT_TYPES = {
    "pdufa_date",
    "adcom_date",
    "adcom_vote",
    "phase3_endpoint",
    "phase2_endpoint",
    "phase1_safety",
    "primary_endpoint_result",
    "secondary_endpoint_result",
    "safety_signal",
    "adverse_event",
    "fda_correspondence",
    "label_change",
    "approval_event",
    "crl_event",
    "withdrawal_event",
    "regulatory_pathway",
    "designation",
    "insider_buy",
    "insider_sell",
    "ownership_change",
    "pipeline_disclosure",
    "sponsor_change",
    "partnership_event",
    "competitive_mention",
    "enrollment_status",
    "enrollment_change",
    "efficacy_result",
    "subgroup_result",
    "biomarker_result",
    "trial_design_change",
    "trial_milestone",
    "press_release_topic",
}


# Sonnet 4.5 pricing
COST_INPUT_PER_M = 3.0
COST_OUTPUT_PER_M = 15.0
# Prompt caching (5-min ephemeral): write = 1.25× base, read = 0.1× base.
COST_CACHE_WRITE_PER_M = COST_INPUT_PER_M * 1.25  # 3.75
COST_CACHE_READ_PER_M = COST_INPUT_PER_M * 0.10   # 0.30


def _estimate_cost(input_tokens: int, output_tokens: int,
                   cache_read_tokens: int = 0,
                   cache_creation_tokens: int = 0) -> float:
    return (
        input_tokens * COST_INPUT_PER_M
        + cache_creation_tokens * COST_CACHE_WRITE_PER_M
        + cache_read_tokens * COST_CACHE_READ_PER_M
        + output_tokens * COST_OUTPUT_PER_M
    ) / 1_000_000


@dataclass
class ExtractStats:
    docs_seen: int = 0
    docs_extracted: int = 0
    facts_inserted: int = 0
    api_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float = 0.0
    errors: int = 0


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def load_unextracted_links(client: SupabaseClient,
                           asset_id: Optional[str] = None,
                           max_links: int = 200) -> List[Dict[str, Any]]:
    """Pull asset_documents rows whose document doesn't yet have any extracted_facts."""
    # Get all distinct (asset_id, document_id) from asset_documents that are material
    params: Dict[str, str] = {
        "select": "asset_id,document_id,link_type,extraction_confidence,extracted_spans",
        "is_material": "is.true",
        "order": "created_at.desc",
        "limit": str(max_links * 2),
    }
    if asset_id:
        params["asset_id"] = f"eq.{asset_id}"
    links = client._rest("GET", "asset_documents", params=params) or []

    # Filter out docs that already have extracted_facts rows
    existing = client._rest(
        "GET", "extracted_facts",
        params={"select": "document_id"},
    ) or []
    existing_doc_ids = {r["document_id"] for r in existing}

    return [l for l in links if l["document_id"] not in existing_doc_ids][:max_links]


def load_doc_with_text(client: SupabaseClient, document_id: str) -> Optional[Dict[str, Any]]:
    rows = client._rest(
        "GET", "documents",
        params={
            "select": ",".join([
                "id", "source", "doc_type", "title", "url",
                "raw_text", "raw_text_tokens", "storage_path",
                "published_at", "extensions",
            ]),
            "id": f"eq.{document_id}",
            "limit": "1",
        },
    ) or []
    if not rows:
        return None
    doc = rows[0]
    if not doc.get("raw_text") and doc.get("storage_path"):
        try:
            blob = client.read_cache("documents", doc["storage_path"])
            if blob:
                doc["raw_text"] = blob.decode("utf-8", errors="replace")
        except Exception as exc:
            logger.warning("Storage read failed for doc %s: %s", document_id, exc)
    return doc


def load_asset(client: SupabaseClient, asset_id: str) -> Optional[Dict[str, Any]]:
    rows = client._rest(
        "GET", "fda_assets",
        params={
            "select": ("id,ticker,drug_name,generic_name,sponsor_name,indication,"
                       "indication_normalized,reference_class_signature,application_number"),
            "id": f"eq.{asset_id}",
            "limit": "1",
        },
    ) or []
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Doc trimming around linked spans
# ---------------------------------------------------------------------------

def trim_to_relevant(text: str, link_spans: List[Dict[str, Any]],
                     drug_name: str, sponsor_first_token: Optional[str],
                     max_chars: int = MAX_DOC_TOKENS * 4) -> str:
    """Build context window from linked-span anchors + keyword neighborhoods.
    For docs under max_chars, return as-is."""
    if len(text) <= max_chars:
        return text

    anchors_text: List[str] = []
    text_lower = text.lower()

    # 1) Each link span's verbatim text → look it up in raw text → window around
    for s in link_spans or []:
        quote = (s.get("text") or "").strip()
        if len(quote) < 20:
            continue
        idx = text.find(quote)
        if idx == -1:
            # Try lowercase fallback
            idx = text_lower.find(quote.lower())
            if idx == -1:
                continue
        w_start = max(0, idx - SPAN_CONTEXT_CHARS // 2)
        w_end = min(len(text), idx + len(quote) + SPAN_CONTEXT_CHARS // 2)
        anchors_text.append((w_start, w_end))

    # 2) drug_name + sponsor occurrences
    for kw in (drug_name, sponsor_first_token):
        if not kw:
            continue
        kw_lower = kw.lower()
        start = 0
        while True:
            idx = text_lower.find(kw_lower, start)
            if idx == -1 or len(anchors_text) > 25:
                break
            w_start = max(0, idx - SPAN_CONTEXT_CHARS // 2)
            w_end = min(len(text), idx + len(kw) + SPAN_CONTEXT_CHARS // 2)
            anchors_text.append((w_start, w_end))
            start = idx + len(kw)

    if not anchors_text:
        # Fallback: head 40k chars
        return text[:max_chars // 2] + "\n\n[…trim…]\n\n"

    # Merge overlapping windows
    anchors_text.sort()
    merged: List[tuple] = []
    for s, e in anchors_text:
        if merged and s <= merged[-1][1] + 100:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))

    pieces: List[str] = []
    used = 0
    for s, e in merged:
        seg = text[s:e]
        if used + len(seg) > max_chars:
            pieces.append(seg[: max_chars - used])
            break
        pieces.append(seg)
        pieces.append("\n\n[…trim…]\n\n")
        used += len(seg) + 16

    return "".join(pieces)


# ---------------------------------------------------------------------------
# Sonnet extraction
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You extract structured FDA-relevant facts from financial \
filings, clinical trial registry entries, FDA notices, and press releases.

For one input document linked to one tracked drug asset, identify discrete \
facts and return ONLY a JSON object matching this schema:

{
  "facts": [
    {
      "fact_type": "<one of the allowed types>",
      "fact_text": "<1-sentence structured statement>",
      "evidence_quote": "<verbatim quote from document, ≤500 chars>",
      "citation_span": {"start": <int char offset>, "end": <int char offset>, "page": <int|null>},
      "confidence": <float 0..1>
    }
  ]
}

Allowed fact_type values:
  pdufa_date, adcom_date, adcom_vote, phase3_endpoint, phase2_endpoint,
  phase1_safety, primary_endpoint_result, secondary_endpoint_result,
  safety_signal, adverse_event, fda_correspondence, label_change,
  approval_event, crl_event, withdrawal_event, regulatory_pathway,
  designation, insider_buy, insider_sell, ownership_change,
  pipeline_disclosure, sponsor_change, partnership_event,
  competitive_mention, enrollment_status, enrollment_change,
  efficacy_result, subgroup_result, biomarker_result, trial_design_change,
  trial_milestone, press_release_topic

Rules:
- Each fact MUST be specific to the tracked asset (drug name + indication + \
sponsor). Skip facts about other drugs unless they're a direct competitive \
comparison.
- evidence_quote must be VERBATIM (character-for-character) from the document. \
Do not paraphrase. ≤500 chars.
- citation_span.start / .end are character offsets in the document text I \
provide. They must point to the evidence_quote. (For trimmed documents, use \
offsets within the text I show you.)
- confidence: 0.95+ when the document explicitly states the fact (e.g. an 8-K \
announcing the PDUFA date); 0.70-0.85 for inferred facts (sponsor's pipeline \
table mentioning approved indication); below 0.70 only when language is hedged \
("may", "expects").
- fact_text: structured 1-sentence statement, e.g. "PDUFA date set for \
2025-12-30 (BLA 761441)" or "Primary endpoint: proptosis responder rate 70% \
vs 5% placebo, p<0.001 at week 15".
- Skip boilerplate (forward-looking statement disclaimers, generic risk \
factor language).
- Maximum 20 facts per document. Prioritize the most specific + most recent.

If the document has no specific facts about the tracked asset, return \
{"facts": []}.

Output JSON only — no commentary, no markdown fences."""


def extract_facts(
    a_client: anthropic.Anthropic,
    doc: Dict[str, Any],
    asset: Dict[str, Any],
    link: Dict[str, Any],
) -> tuple[List[Dict[str, Any]], int, int, int, int]:
    """Send a doc + asset context to Sonnet for fact extraction. Returns
    (facts, input_tokens, output_tokens, cache_read_tokens,
     cache_creation_tokens)."""
    raw_text = doc.get("raw_text") or ""
    if not raw_text:
        return [], 0, 0, 0, 0

    sponsor_first = None
    sponsor = asset.get("sponsor_name") or ""
    sponsor_tokens = re.findall(r"\b[A-Z][\w-]{3,}\b", sponsor)
    if sponsor_tokens:
        sponsor_first = sponsor_tokens[0]

    trimmed = trim_to_relevant(
        raw_text,
        link.get("extracted_spans") or [],
        drug_name=asset.get("drug_name") or "",
        sponsor_first_token=sponsor_first,
    )

    user_content = f"""Tracked asset:
  drug_name: {asset.get('drug_name')}
  generic_name: {asset.get('generic_name') or '(unknown)'}
  sponsor_name: {asset.get('sponsor_name')}
  indication: {asset.get('indication')}
  application_number: {asset.get('application_number') or '(unknown)'}

Document metadata:
  source: {doc.get('source')}
  doc_type: {doc.get('doc_type')}
  title: {doc.get('title') or '(none)'}
  url: {doc.get('url') or '(none)'}
  published_at: {doc.get('published_at')}
  link_type: {link.get('link_type')}
  link_confidence: {link.get('extraction_confidence')}

Document text (offsets are within this text; possibly trimmed — sections \
separated by […trim…]):

{trimmed}
"""

    # cache_control on the SYSTEM_PROMPT prefix. At current SYSTEM_PROMPT
    # tokenization (~500 tokens per Anthropic's tokenizer) this is BELOW the
    # 1024-token minimum for Sonnet caching and the API silently returns
    # cache_*_input_tokens=0 — same situation as the asset_linker prefix.
    # Kept as a forward-looking no-op: engages automatically once the prompt
    # grows past the minimum (e.g. if VALID_FACT_TYPES guidance is expanded).
    system_blocks = [
        {"type": "text", "text": SYSTEM_PROMPT,
         "cache_control": {"type": "ephemeral"}},
    ]

    resp = a_client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=system_blocks,
        messages=[{"role": "user", "content": user_content}],
    )
    in_tokens = resp.usage.input_tokens
    out_tokens = resp.usage.output_tokens
    cache_read = getattr(resp.usage, "cache_read_input_tokens", 0) or 0
    cache_create = getattr(resp.usage, "cache_creation_input_tokens", 0) or 0

    text_out = "".join(b.text for b in resp.content if b.type == "text")
    text_out = text_out.strip()
    if text_out.startswith("```"):
        text_out = re.sub(r"^```(?:json)?\s*\n?", "", text_out)
        text_out = re.sub(r"\n?```\s*$", "", text_out)

    try:
        parsed = json.loads(text_out)
    except json.JSONDecodeError as exc:
        logger.warning("JSON parse failed for doc %s: %s — output[:200]: %s",
                       doc.get("id"), exc, text_out[:200])
        return [], in_tokens, out_tokens, cache_read, cache_create

    facts = parsed.get("facts", [])
    out: List[Dict[str, Any]] = []
    for f in facts[:25]:  # hard cap at 25 even if model emitted more
        if not isinstance(f, dict):
            continue
        fact_type = f.get("fact_type")
        if fact_type not in VALID_FACT_TYPES:
            continue
        fact_text = (f.get("fact_text") or "").strip()
        evidence_quote = (f.get("evidence_quote") or "").strip()[:500]
        if not fact_text or not evidence_quote:
            continue
        citation_span = f.get("citation_span") or {}
        if not isinstance(citation_span, dict):
            citation_span = {}
        confidence = f.get("confidence")
        try:
            confidence = float(confidence) if confidence is not None else None
        except (TypeError, ValueError):
            confidence = None
        if confidence is not None:
            confidence = max(0.0, min(1.0, confidence))
        out.append({
            "fact_type": fact_type,
            "fact_text": fact_text[:2000],
            "evidence_quote": evidence_quote,
            "citation_span": citation_span,
            "confidence": confidence,
        })
    return out, in_tokens, out_tokens, cache_read, cache_create


# ---------------------------------------------------------------------------
# Insert
# ---------------------------------------------------------------------------

def insert_facts(client: SupabaseClient, asset_id: str, document_id: str,
                 facts: List[Dict[str, Any]]) -> int:
    if not facts:
        return 0
    rows = []
    for f in facts:
        rows.append({
            "document_id": document_id,
            "asset_id": asset_id,
            "fact_type": f["fact_type"],
            "fact_text": f["fact_text"],
            "evidence_quote": f["evidence_quote"],
            "citation_span": f["citation_span"],
            "confidence": round(f["confidence"], 2) if f["confidence"] is not None else None,
            "extraction_model": MODEL,
        })
    try:
        result = client._rest(
            "POST", "extracted_facts",
            json_body=rows,
            prefer="return=minimal",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("extracted_facts insert failed for doc %s: %s",
                         document_id, exc)
        return 0
    return len(rows)


# ---------------------------------------------------------------------------
# Run-row lifecycle — fact_extractor_runs (added 2026-05-12 as the storage
# half of the 24h hard halt). Mirror of asset_linker_runs lifecycle.
# ---------------------------------------------------------------------------

STALE_RUNNING_AFTER_MINUTES = 30
LOCK_CONFLICT = "fact_extractor_runs_one_running"


def _start_fact_extractor_run_row(
    client: SupabaseClient, model: str
) -> tuple[Optional[str], bool]:
    """Acquire the per-pass concurrency lock by INSERTing a row with
    status='running'. Returns (run_id, lock_held). lock_held=False means
    another instance is actively running and this caller should exit."""
    from datetime import datetime, timedelta, timezone
    cutoff_iso = (datetime.now(timezone.utc)
                  - timedelta(minutes=STALE_RUNNING_AFTER_MINUTES)).isoformat()
    # Reclaim any zombie 'running' row older than the cutoff.
    try:
        client._rest(
            "PATCH", "fact_extractor_runs",
            params={
                "status": "eq.running",
                "started_at": f"lt.{cutoff_iso}",
            },
            json_body={
                "status": "failed",
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "notes": "reclaimed: stale running row exceeded "
                         f"{STALE_RUNNING_AFTER_MINUTES}min threshold",
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("stale-running reclaim PATCH failed: %s", exc)

    try:
        res = client._rest(
            "POST", "fact_extractor_runs",
            json_body={"model": model, "status": "running"},
            prefer="return=representation",
        )
    except Exception as exc:  # noqa: BLE001
        # 409 from the partial unique index means another runner already
        # holds the lock — exit cleanly.
        if LOCK_CONFLICT in str(exc) or "409" in str(exc):
            return None, False
        logger.warning("start_run_row INSERT failed: %s", exc)
        return None, True
    if isinstance(res, list) and res:
        return res[0].get("id"), True
    return None, True


def _finish_fact_extractor_run_row(
    client: SupabaseClient, run_id: Optional[str],
    status: str, stats: "ExtractStats",
) -> None:
    """PATCH the run row with terminal stats. Best-effort — silent on failure."""
    if not run_id:
        return
    from datetime import datetime, timezone
    patch: Dict[str, Any] = {
        "status": status,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "docs_seen": stats.docs_seen,
        "docs_extracted": stats.docs_extracted,
        "facts_inserted": stats.facts_inserted,
        "api_calls": stats.api_calls,
        "errors": stats.errors,
        "input_tokens": stats.input_tokens,
        "output_tokens": stats.output_tokens,
        "cache_read_tokens": stats.cache_read_tokens,
        "cache_creation_tokens": stats.cache_creation_tokens,
        "cost_usd": round(float(stats.cost_usd), 4),
    }
    try:
        client._rest(
            "PATCH", "fact_extractor_runs",
            params={"id": f"eq.{run_id}"},
            json_body=patch,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("finish_run_row PATCH failed: %s", exc)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: List[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sonnet_fact_extractor")
    p.add_argument("--asset-id", default=None,
                   help="Restrict to one fda_asset (default: all material links)")
    p.add_argument("--max", type=int, default=50,
                   help="Max linked documents to extract this run (reduced "
                        "from 100 on 2026-05-11 in the asset_linker incident "
                        "hardening sweep — keep tight until per-doc cost is "
                        "verified)")
    p.add_argument("--budget-usd", type=float, default=5.0,
                   help="Stop early if cumulative cost exceeds this (reduced "
                        "from $30 on 2026-05-11; per-doc Sonnet 4.5 fact "
                        "extraction is ~$0.05-0.15, so $5 gives 30-100 docs "
                        "of headroom)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--ignore-24h-halt", action="store_true",
                   help="Bypass the 24h hard-halt check. Operator override "
                        "for one-off backfills; cron never sets this.")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        logger.error("ANTHROPIC_API_KEY not set")
        return 2

    sb = SupabaseClient()

    # 24h hard halt — check BEFORE acquiring the lock or fetching docs.
    # Skipped in dry-run (offline operator path) and behind --ignore-24h-halt
    # for explicit backfills.
    if not args.dry_run and not args.ignore_24h_halt:
        from modal_workers.shared.cost_budget import (
            check_fact_extractor_hard_halt,
        )
        halt_status = check_fact_extractor_hard_halt(sb)
        if halt_status["halt"]:
            logger.error(
                "fact_extractor 24h spend $%.2f has reached the hard halt — "
                "exiting without work. (See operator_flags for the breach.)",
                halt_status["total_24h_usd"],
            )
            return 0
    elif args.ignore_24h_halt:
        logger.warning("--ignore-24h-halt active: 24h hard halt bypassed.")

    a_client = anthropic.Anthropic()
    stats = ExtractStats()
    run_id: Optional[str] = None
    lock_held = True
    if not args.dry_run:
        run_id, lock_held = _start_fact_extractor_run_row(sb, MODEL)
        if not lock_held:
            logger.info("Another fact_extractor run is active — exiting.")
            return 0
    crashed = False
    budget_exceeded = False

    try:
        links = load_unextracted_links(
            sb, asset_id=args.asset_id, max_links=args.max,
        )
        logger.info("Loaded %d unextracted asset_documents link(s)", len(links))
        stats.docs_seen = len(links)

        # Cache asset rows since many links share the same asset
        asset_cache: Dict[str, Dict[str, Any]] = {}

        for link in links:
            if stats.cost_usd > args.budget_usd:
                logger.warning("Budget exceeded ($%.2f > $%.2f); stopping",
                               stats.cost_usd, args.budget_usd)
                budget_exceeded = True
                break

            asset_id = link["asset_id"]
            document_id = link["document_id"]

            if asset_id not in asset_cache:
                a = load_asset(sb, asset_id)
                if not a:
                    logger.warning("Asset %s not found; skipping", asset_id)
                    stats.errors += 1
                    continue
                asset_cache[asset_id] = a
            asset = asset_cache[asset_id]

            doc = load_doc_with_text(sb, document_id)
            if not doc or not doc.get("raw_text"):
                logger.warning("Doc %s has no text; skipping", document_id)
                stats.errors += 1
                continue

            try:
                (facts, in_tok, out_tok, cache_read_tok,
                 cache_create_tok) = extract_facts(a_client, doc, asset, link)
            except anthropic.APIError as exc:
                logger.warning("API error for doc %s: %s", document_id, exc)
                stats.errors += 1
                time.sleep(2.0)
                continue

            stats.api_calls += 1
            stats.input_tokens += in_tok
            stats.output_tokens += out_tok
            stats.cache_read_tokens += cache_read_tok
            stats.cache_creation_tokens += cache_create_tok
            stats.cost_usd += _estimate_cost(
                in_tok, out_tok,
                cache_read_tokens=cache_read_tok,
                cache_creation_tokens=cache_create_tok,
            )
            stats.docs_extracted += 1

            if not facts:
                logger.info("doc %s [%s] %s -> no facts (cost so far $%.3f)",
                            document_id, doc.get("source"), doc.get("doc_type"),
                            stats.cost_usd)
                continue

            if args.dry_run:
                logger.info("[dry-run] doc %s -> %d facts: %s",
                            document_id, len(facts),
                            [f["fact_type"] for f in facts])
                continue

            n = insert_facts(sb, asset_id, document_id, facts)
            stats.facts_inserted += n
            logger.info("doc %s [%s] %s -> %d facts inserted (cost $%.3f)",
                        document_id, doc.get("source"), doc.get("doc_type"),
                        n, stats.cost_usd)
    except Exception as exc:  # noqa: BLE001
        # Uncaught exception escaped the per-doc loop — finalize the run row
        # with status='failed' so observability + the watchdog can see it,
        # and we don't leave a zombie status='running' row blocking the next
        # cron tick (via the partial unique index).
        logger.exception("Unexpected exception in fact_extractor main: %s", exc)
        crashed = True
    finally:
        logger.info("=" * 60)
        logger.info("Extractor summary:")
        logger.info("  docs_seen=%d  docs_extracted=%d  errors=%d",
                    stats.docs_seen, stats.docs_extracted, stats.errors)
        logger.info("  facts_inserted=%d  api_calls=%d",
                    stats.facts_inserted, stats.api_calls)
        logger.info("  tokens: in=%d  out=%d  cache_read=%d  cache_create=%d  "
                    "cost_usd=$%.3f",
                    stats.input_tokens, stats.output_tokens,
                    stats.cache_read_tokens, stats.cache_creation_tokens,
                    stats.cost_usd)
        if crashed:
            final_status = "failed"
        elif budget_exceeded:
            final_status = "budget_exceeded"
        else:
            final_status = "completed"
        _finish_fact_extractor_run_row(sb, run_id, final_status, stats)
    return 1 if crashed else 0


if __name__ == "__main__":
    sys.exit(main())
