"""Asset linker — classify documents into asset_documents.

Single-pass Sonnet classifier for the v3 MVP (one tracked asset = VRDN/Veligrotug).
Generalizes to multi-asset two-pass when more assets enter the watchlist.

Flow per document:
  1. Pre-filter raw_text via regex on each watched asset's drug_name,
     sponsor_name, and indication. Skip if no match (cheap, no API call).
  2. For matches, send the document (or pre-trimmed window around matches for
     huge docs >80k tokens) to Sonnet with the full set of currently-active
     fda_assets as context.
  3. Sonnet returns JSON: list of links, each with
     {asset_id, link_type, extraction_confidence, extracted_spans, is_material,
      reasoning}.
  4. For each link: insert asset_documents row (idempotent on
     (asset_id, document_id, link_type)).

link_type values (CHECK constraint): primary, mentions, pipeline_context,
safety_signal, literature.

Cost: ~$3-15 for typical 100-document run depending on doc size + match rate.

Run:
  ANTHROPIC_API_KEY=... SUPABASE_URL=... \\
    python3 -m modal_workers.extractor.asset_linker \\
        --asset-id <uuid> [--max N] [--dry-run]
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
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import anthropic

from modal_workers.shared.supabase_client import SupabaseClient

logger = logging.getLogger(__name__)

# Sonnet 4.6 — structured-output extraction. Cheap relative to Opus,
# accurate for classification tasks.
MODEL = "claude-sonnet-4-5-20250929"  # widely-available v4.5; v4.6 alias when GA

# For huge docs, we trim around matches to keep cost bounded. 80k context is
# plenty for asset linking (which doesn't need every paragraph — just the
# parts that mention the asset).
MAX_DOC_TOKENS_FOR_LINKER = 80_000
TRIM_WINDOW_CHARS = 4_000  # window around each regex hit when trimming

# Pydantic-ish output schema for the model
LINK_TYPES = {"primary", "mentions", "pipeline_context", "safety_signal", "literature"}


@dataclass
class LinkResult:
    asset_id: str
    link_type: str
    extraction_confidence: float
    extracted_spans: List[Dict[str, Any]]
    is_material: bool
    reasoning: str


@dataclass
class LinkerStats:
    docs_seen: int = 0
    docs_prefilter_passed: int = 0
    docs_prefilter_skipped: int = 0
    docs_classified: int = 0
    links_inserted: int = 0
    links_dedup_skipped: int = 0
    api_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    errors: int = 0


def record_linker_run_summary(
    client: SupabaseClient,
    stats: LinkerStats,
    started_at: datetime,
    *,
    status: str,
    notes: Optional[str] = None,
) -> None:
    """Persist the operational rollup row consumed by dashboard/audit queries."""
    row = {
        "pass": "pass1",
        "model": MODEL,
        "started_at": started_at.isoformat(),
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "docs_seen": stats.docs_seen,
        "prefilter_passed": stats.docs_prefilter_passed,
        "prefilter_skipped": stats.docs_prefilter_skipped,
        "api_calls": stats.api_calls,
        "errors": stats.errors,
        "links_inserted": stats.links_inserted,
        "links_dedup_skipped": stats.links_dedup_skipped,
        "input_tokens": stats.input_tokens,
        "output_tokens": stats.output_tokens,
        "cost_usd": round(stats.cost_usd, 4),
        "notes": notes,
    }
    client._rest_with_retry(
        "POST",
        "asset_linker_runs",
        json_body=row,
        prefer="return=minimal",
    )


# Sonnet 4.5 pricing (USD per 1M tokens, as of plan-time):
COST_INPUT_PER_M = 3.0
COST_OUTPUT_PER_M = 15.0


def _estimate_cost(input_tokens: int, output_tokens: int) -> float:
    return (input_tokens * COST_INPUT_PER_M + output_tokens * COST_OUTPUT_PER_M) / 1_000_000


# ---------------------------------------------------------------------------
# Active assets — load from fda_assets where is_active = true
# ---------------------------------------------------------------------------

def load_active_assets(client: SupabaseClient,
                       only_asset_id: Optional[str] = None) -> List[Dict[str, Any]]:
    params = {
        "select": "id,ticker,drug_name,generic_name,sponsor_name,indication,indication_normalized",
        "is_active": "eq.true",
    }
    if only_asset_id:
        params["id"] = f"eq.{only_asset_id}"
    return client._rest("GET", "fda_assets", params=params) or []


def build_keyword_index(assets: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Map keyword (drug_name / generic / sponsor / indication tokens) →
    list of assets that match. Used by the regex pre-filter."""
    idx: Dict[str, List[Dict[str, Any]]] = {}
    for a in assets:
        for fld in ("drug_name", "generic_name", "sponsor_name", "indication"):
            val = (a.get(fld) or "").strip()
            if not val:
                continue
            # Drug name as-is; sponsor split into informative tokens; indication
            # take key noun phrases. Keep it simple — primary kw is drug_name.
            for kw in _keywords_from(val, fld):
                idx.setdefault(kw.lower(), []).append(a)
    return idx


def _keywords_from(value: str, fld: str) -> List[str]:
    """Pick keywords worth regex-matching."""
    if fld == "drug_name":
        # Drug brand may have parenthetical generic, e.g. "FILSPARI (sparsentan)".
        parts = [value]
        m = re.match(r"^([A-Z][\w-]+)\s*\(([^)]+)\)\s*$", value)
        if m:
            parts = [m.group(1), m.group(2)]
        return [p.strip() for p in parts if len(p.strip()) >= 4]
    if fld == "generic_name":
        return [value] if len(value) >= 4 else []
    if fld == "sponsor_name":
        # First word of company name (skip "the/inc/corp/llc")
        tokens = re.findall(r"\b[A-Z][\w-]{3,}\b", value)
        return tokens[:2]
    if fld == "indication":
        # Take key noun phrases — first 3 words
        words = value.split()
        if len(words) <= 3:
            return [value]
        return [" ".join(words[:3])]
    return []


# ---------------------------------------------------------------------------
# Document loading
# ---------------------------------------------------------------------------

def load_documents_to_link(client: SupabaseClient, max_docs: int = 200) -> List[Dict[str, Any]]:
    """Pull documents that have no asset_documents row yet. Newest-first so
    the most recent material lands quickly during a backfill."""
    # PostgREST doesn't support NOT EXISTS subquery directly. Easiest: fetch
    # all linked document_ids first, then exclude.
    linked = client._rest(
        "GET", "asset_documents",
        params={"select": "document_id"},
    ) or []
    linked_ids = {r["document_id"] for r in linked}

    rows = client._rest(
        "GET", "documents",
        params={
            "select": ",".join([
                "id", "source", "doc_type", "title", "url",
                "raw_text", "raw_text_tokens", "storage_path",
                "published_at", "extensions",
            ]),
            "order": "published_at.desc",
            "limit": str(max_docs * 2),  # fetch extra to allow filtering
        },
    ) or []

    return [r for r in rows if r["id"] not in linked_ids][:max_docs]


def _load_doc_text(doc: Dict[str, Any], client: SupabaseClient) -> Optional[str]:
    """Get raw text from inline column or Storage."""
    if doc.get("raw_text"):
        return doc["raw_text"]
    if doc.get("storage_path"):
        try:
            blob = client.read_cache("documents", doc["storage_path"])
        except Exception as exc:
            logger.warning("Storage read failed for doc %s: %s", doc["id"], exc)
            return None
        if blob:
            return blob.decode("utf-8", errors="replace")
    return None


# ---------------------------------------------------------------------------
# Pre-filter
# ---------------------------------------------------------------------------

def prefilter_doc(text: str, keyword_index: Dict[str, List[Dict[str, Any]]]
                  ) -> List[Dict[str, Any]]:
    """Returns the assets whose keywords appear in `text`. Empty list = skip."""
    text_lower = text.lower()
    seen_asset_ids: set[str] = set()
    matched: List[Dict[str, Any]] = []
    for kw, assets in keyword_index.items():
        if kw in text_lower:
            for a in assets:
                if a["id"] not in seen_asset_ids:
                    seen_asset_ids.add(a["id"])
                    matched.append(a)
    return matched


def trim_around_matches(text: str, keywords: List[str],
                        max_chars: int = MAX_DOC_TOKENS_FOR_LINKER * 4) -> str:
    """For huge docs, return the first 30% of the doc + windows around each
    keyword hit, capped at `max_chars`. Keeps the head (where 10-K Item 1 is)
    and the relevant context."""
    if len(text) <= max_chars:
        return text

    # 30% from head — covers Item 1 Business + leading sections
    head_size = int(max_chars * 0.3)
    head = text[:head_size]

    # Windows around matches
    windows: List[tuple] = []
    text_lower = text.lower()
    for kw in keywords:
        kw_lower = kw.lower()
        start = 0
        while True:
            idx = text_lower.find(kw_lower, start)
            if idx == -1 or len(windows) > 10:
                break
            w_start = max(head_size, idx - TRIM_WINDOW_CHARS // 2)
            w_end = min(len(text), idx + TRIM_WINDOW_CHARS // 2)
            windows.append((w_start, w_end))
            start = idx + len(kw)

    # Merge overlapping windows
    windows.sort()
    merged: List[tuple] = []
    for s, e in windows:
        if merged and s <= merged[-1][1] + 100:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))

    body_pieces = [head]
    body_pieces.append("\n\n[…trimmed…]\n\n")
    remaining_budget = max_chars - head_size - 200
    for s, e in merged:
        seg = text[s:e]
        if remaining_budget < len(seg):
            body_pieces.append(seg[:remaining_budget])
            break
        body_pieces.append(seg)
        body_pieces.append("\n\n[…trim…]\n\n")
        remaining_budget -= len(seg) + 16

    return "".join(body_pieces)


# ---------------------------------------------------------------------------
# Sonnet classifier
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You classify SEC filings, ClinicalTrials.gov entries, FDA notices, and \
press releases against a list of FDA-tracked drug assets. For each input document, \
identify which assets (if any) the document references, and how.

You emit ONLY a JSON object matching this schema:

{
  "links": [
    {
      "asset_id": "<uuid from the input>",
      "link_type": "primary | mentions | pipeline_context | safety_signal | literature",
      "extraction_confidence": <float 0..1>,
      "extracted_spans": [
        {"text": "<verbatim quote from doc>", "context": "<1-line description>"}
      ],
      "is_material": <true|false>,
      "reasoning": "<1-3 sentences>"
    }
  ]
}

Definitions:
- primary: the document is *about* this asset (e.g. PDUFA notice for this NDA, \
sponsor's own 8-K announcing FDA correspondence on this drug, sponsor's 10-K \
section discussing this drug's program). High confidence (≥0.85).
- mentions: the asset is named in passing (e.g. competitor 10-K listing this drug \
in a market-overview table; 13F filing listing the sponsor as a holding). \
Lower confidence and is_material is usually false for incidental mentions.
- pipeline_context: the document discusses the sponsor's pipeline broadly and \
this asset is one of several covered (e.g. sponsor 10-K Item 1 Business that \
describes the entire portfolio). is_material may be true if the section gives \
substantive detail on this asset.
- safety_signal: the document raises a safety concern relevant to this asset \
(FAERS adverse event, FDA warning letter, 483 inspection, peer-reviewed AE \
publication).
- literature: peer-reviewed paper or preprint discussing the asset's mechanism, \
trial data, or comparative evidence.

is_material: true if a competent investor would consider this content meaningful \
input to a thesis on the asset. False for boilerplate / 13F-style holdings / \
generic mentions in unrelated contexts.

extracted_spans: 1-3 short verbatim quotes (≤300 chars each) that demonstrate \
the link. Required for every link.

If the document does not reference any of the listed assets in any meaningful way, \
return {"links": []}.

Output JSON only — no commentary, no markdown fences."""


def classify_document(
    client: anthropic.Anthropic,
    doc: Dict[str, Any],
    candidate_assets: List[Dict[str, Any]],
    text: str,
    matched_keywords: List[str],
) -> tuple[List[LinkResult], int, int]:
    """Send a single document to Sonnet for classification. Returns
    (links, input_tokens, output_tokens)."""
    trimmed = trim_around_matches(text, matched_keywords)

    asset_card = "\n".join(
        f"- asset_id={a['id']} | drug_name={a.get('drug_name', '?')} | "
        f"generic={a.get('generic_name', '?')} | sponsor={a.get('sponsor_name', '?')} | "
        f"indication={a.get('indication', '?')}"
        for a in candidate_assets
    )

    user_content = f"""Tracked assets (identify which, if any, the document references):

{asset_card}

Document metadata:
- source: {doc.get('source')}
- doc_type: {doc.get('doc_type')}
- title: {doc.get('title') or '(none)'}
- url: {doc.get('url') or '(none)'}
- published_at: {doc.get('published_at')}

Document text (possibly trimmed for length — sections separated by […trim…]):

{trimmed}
"""

    resp = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )
    in_tokens = resp.usage.input_tokens
    out_tokens = resp.usage.output_tokens

    # Parse JSON
    text_out = "".join(b.text for b in resp.content if b.type == "text")
    text_out = text_out.strip()
    # Strip markdown fences if Sonnet adds them despite instruction
    if text_out.startswith("```"):
        text_out = re.sub(r"^```(?:json)?\s*\n?", "", text_out)
        text_out = re.sub(r"\n?```\s*$", "", text_out)

    try:
        parsed = json.loads(text_out)
    except json.JSONDecodeError as exc:
        logger.warning("JSON parse failed for doc %s: %s — output was: %s",
                       doc["id"], exc, text_out[:200])
        return [], in_tokens, out_tokens

    raw_links = parsed.get("links", [])
    out: List[LinkResult] = []
    valid_asset_ids = {a["id"] for a in candidate_assets}
    for r in raw_links:
        if not isinstance(r, dict):
            continue
        asset_id = r.get("asset_id")
        link_type = r.get("link_type")
        if asset_id not in valid_asset_ids or link_type not in LINK_TYPES:
            continue
        confidence = float(r.get("extraction_confidence") or 0.0)
        confidence = max(0.0, min(1.0, confidence))
        spans = r.get("extracted_spans") or []
        if not isinstance(spans, list):
            spans = []
        out.append(LinkResult(
            asset_id=asset_id,
            link_type=link_type,
            extraction_confidence=confidence,
            extracted_spans=spans,
            is_material=bool(r.get("is_material", True)),
            reasoning=str(r.get("reasoning", ""))[:1000],
        ))
    return out, in_tokens, out_tokens


# ---------------------------------------------------------------------------
# Insert
# ---------------------------------------------------------------------------

def insert_links(client: SupabaseClient, document_id: str,
                 links: List[LinkResult]) -> tuple[int, int]:
    """Insert asset_documents rows. Idempotent on (asset_id, document_id, link_type)."""
    inserted = 0
    skipped = 0
    for link in links:
        row = {
            "asset_id": link.asset_id,
            "document_id": document_id,
            "link_type": link.link_type,
            "extraction_method": "agent_pass1",
            "extraction_confidence": round(link.extraction_confidence, 2),
            "extracted_spans": link.extracted_spans,
            "is_material": link.is_material,
            "verified_by_pass2": False,
        }
        try:
            res = client._rest(
                "POST", "asset_documents",
                params={"on_conflict": "asset_id,document_id,link_type"},
                json_body=row,
                prefer="return=representation,resolution=ignore-duplicates",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("asset_documents insert failed for doc %s asset %s: %s",
                           document_id, link.asset_id, exc)
            skipped += 1
            continue
        if res:
            inserted += 1
        else:
            skipped += 1
    return inserted, skipped


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: List[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="asset_linker")
    p.add_argument("--asset-id", default=None,
                   help="Restrict to one fda_asset (default: all is_active=true)")
    p.add_argument("--max", type=int, default=200,
                   help="Max documents to process this run")
    p.add_argument("--dry-run", action="store_true",
                   help="Classify but do not insert asset_documents rows")
    p.add_argument("--budget-usd", type=float, default=15.0,
                   help="Stop early if cumulative cost exceeds this")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        logger.error("ANTHROPIC_API_KEY not set")
        return 2

    sb = SupabaseClient()
    a_client = anthropic.Anthropic()
    stats = LinkerStats()
    started_at = datetime.now(timezone.utc)

    assets = load_active_assets(sb, only_asset_id=args.asset_id)
    if not assets:
        logger.error("No active fda_assets found")
        return 1
    logger.info("Loaded %d active asset(s)", len(assets))

    keyword_index = build_keyword_index(assets)
    logger.info("Built keyword index with %d unique keywords", len(keyword_index))

    docs = load_documents_to_link(sb, max_docs=args.max)
    logger.info("Loaded %d unlinked document(s)", len(docs))
    stats.docs_seen = len(docs)

    for doc in docs:
        if stats.cost_usd > args.budget_usd:
            logger.warning("Budget exceeded ($%.2f > $%.2f); stopping early",
                           stats.cost_usd, args.budget_usd)
            break

        text = _load_doc_text(doc, sb)
        if not text:
            logger.warning("doc %s has no text; skipping", doc["id"])
            stats.errors += 1
            continue

        candidates = prefilter_doc(text, keyword_index)
        if not candidates:
            stats.docs_prefilter_skipped += 1
            continue
        stats.docs_prefilter_passed += 1

        # Collect distinct keywords that triggered the match (for trimming)
        matched_kws: List[str] = []
        text_lower = text.lower()
        for kw in keyword_index:
            if kw in text_lower:
                matched_kws.append(kw)

        try:
            links, in_tok, out_tok = classify_document(
                a_client, doc, candidates, text, matched_kws,
            )
        except anthropic.APIError as exc:
            logger.warning("API error for doc %s: %s", doc["id"], exc)
            stats.errors += 1
            time.sleep(2.0)
            continue

        stats.api_calls += 1
        stats.input_tokens += in_tok
        stats.output_tokens += out_tok
        stats.cost_usd += _estimate_cost(in_tok, out_tok)
        stats.docs_classified += 1

        if not links:
            logger.info("doc %s [%s] %s — no links emitted (cost so far $%.3f)",
                        doc["id"], doc.get("source"), doc.get("doc_type"),
                        stats.cost_usd)
            continue

        if args.dry_run:
            for l in links:
                logger.info(
                    "[dry-run] doc %s -> asset %s link_type=%s conf=%.2f material=%s",
                    doc["id"], l.asset_id, l.link_type,
                    l.extraction_confidence, l.is_material,
                )
            continue

        inserted, skipped = insert_links(sb, doc["id"], links)
        stats.links_inserted += inserted
        stats.links_dedup_skipped += skipped
        logger.info(
            "doc %s [%s] %s -> %d link(s) inserted, %d skipped (cost $%.3f)",
            doc["id"], doc.get("source"), doc.get("doc_type"),
            inserted, skipped, stats.cost_usd,
        )

    logger.info("=" * 60)
    logger.info("Linker summary:")
    logger.info("  docs_seen=%d  prefilter_passed=%d  prefilter_skipped=%d",
                stats.docs_seen, stats.docs_prefilter_passed, stats.docs_prefilter_skipped)
    logger.info("  docs_classified=%d  api_calls=%d  errors=%d",
                stats.docs_classified, stats.api_calls, stats.errors)
    logger.info("  links_inserted=%d  links_dedup_skipped=%d",
                stats.links_inserted, stats.links_dedup_skipped)
    logger.info("  tokens: in=%d  out=%d  cost_usd=$%.3f",
                stats.input_tokens, stats.output_tokens, stats.cost_usd)
    status = "budget_exceeded" if stats.cost_usd > args.budget_usd else "completed"
    notes = "dry_run=true" if args.dry_run else None
    try:
        record_linker_run_summary(sb, stats, started_at, status=status, notes=notes)
    except Exception as exc:  # noqa: BLE001
        logger.warning("asset_linker_runs summary insert failed: %s", exc)
    return 0


# ---------------------------------------------------------------------------
# Pass-2 verifier — Haiku 4.5 verifies low-confidence agent_pass1 links.
# Triggered on rows where extraction_method='agent_pass1' AND
# extraction_confidence < 0.80 AND verified_by_pass2 = false. Verdicts:
#   'kept'     — link is correct
#   'demoted'  — real link but is_material should be false
#   'rejected' — spans don't substantiate the claim
# Rejected sets is_material=false but never DELETEs (audit-trail survival).
# Batched 5 links per Haiku call for ~$0.002/link.
# ---------------------------------------------------------------------------

PASS2_MODEL = "claude-haiku-4-5-20251001"
PASS2_BATCH_SIZE = 5
PASS2_DEFAULT_THRESHOLD = 0.80


@dataclass
class Pass2Verdict:
    asset_documents_id: str
    verdict: str   # 'kept' | 'demoted' | 'rejected'
    confidence: float
    reasoning: str


@dataclass
class Pass2Stats:
    rows_seen: int = 0
    batches_called: int = 0
    api_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    kept: int = 0
    demoted: int = 0
    rejected: int = 0
    errors: int = 0


PASS2_SYSTEM_PROMPT = """You are a verifier for a document → asset linker.

For each pass-1 link, decide whether the extracted spans actually substantiate \
the claimed link_type to the named asset:
- "kept": the spans clearly support the link_type and the asset.
- "demoted": the spans support a real mention of the asset, but the content is \
boilerplate / not substantively material to a thesis (e.g. a generic pipeline \
list mention, a 13F-style holdings line, a corporate-history sentence).
- "rejected": the spans do NOT actually substantiate this asset+link_type \
(wrong asset, wrong link_type, or spans don't say what pass-1 claimed).

Output JSON only:
{"verdicts": [
  {"asset_documents_id": "<uuid>", "verdict": "kept|demoted|rejected", \
"confidence": 0.0-1.0, "reasoning": "<≤200 chars>"},
  ...
]}

No commentary. No markdown fences."""


def _fetch_pass2_pending(
    client: SupabaseClient,
    asset_id: Optional[str] = None,
    max_links: int = 200,
    threshold: float = PASS2_DEFAULT_THRESHOLD,
) -> List[Dict[str, Any]]:
    """Fetch asset_documents rows needing pass-2, joined with asset + document
    context for the verifier prompt."""
    params: Dict[str, str] = {
        "select": (
            "id,asset_id,document_id,link_type,extraction_confidence,"
            "extracted_spans,is_material,"
            "asset:fda_assets(id,drug_name,generic_name,sponsor_name,indication),"
            "document:documents(id,source,doc_type,title,published_at)"
        ),
        "extraction_method": "eq.agent_pass1",
        "verified_by_pass2": "is.false",
        "extraction_confidence": f"lt.{threshold}",
        "order": "asset_id.asc,document_id.asc,extraction_confidence.asc",
        "limit": str(max_links),
    }
    if asset_id:
        params["asset_id"] = f"eq.{asset_id}"
    return client._rest("GET", "asset_documents", params=params) or []


def _build_pass2_user_content(rows: List[Dict[str, Any]]) -> str:
    """Render a batch of up to PASS2_BATCH_SIZE asset_documents rows as the
    verifier user prompt. Includes asset card + spans only (not full doc)."""
    parts: List[str] = []
    for r in rows:
        asset = r.get("asset") or {}
        doc = r.get("document") or {}
        spans = r.get("extracted_spans") or []
        spans_rendered = "\n".join(
            f"  - {(s or {}).get('text', '') if isinstance(s, dict) else str(s)}"
            for s in spans[:6]
        )
        parts.append(
            f"asset_documents_id: {r['id']}\n"
            f"link_type: {r['link_type']}\n"
            f"is_material (pass-1): {r.get('is_material')}\n"
            f"pass-1 confidence: {r.get('extraction_confidence')}\n"
            f"asset:\n"
            f"  drug_name: {asset.get('drug_name', '?')}\n"
            f"  generic_name: {asset.get('generic_name', '?')}\n"
            f"  sponsor_name: {asset.get('sponsor_name', '?')}\n"
            f"  indication: {asset.get('indication', '?')}\n"
            f"document: source={doc.get('source')}, doc_type={doc.get('doc_type')}, "
            f"title={doc.get('title') or '(none)'}\n"
            f"extracted_spans:\n{spans_rendered or '  (none)'}\n"
        )
    return (
        "Verify each pass-1 link below. Output one verdict per "
        "asset_documents_id, in the same order.\n\n"
        + "\n---\n".join(parts)
    )


def verify_link_pass2_batch(
    client: anthropic.Anthropic,
    rows: List[Dict[str, Any]],
) -> tuple[List[Pass2Verdict], int, int]:
    """Send up to PASS2_BATCH_SIZE rows to Haiku 4.5. Returns
    (verdicts, in_tokens, out_tokens). Verdicts are returned in the same order
    as the input rows; missing/invalid verdicts are dropped."""
    user_content = _build_pass2_user_content(rows)
    resp = client.messages.create(
        model=PASS2_MODEL,
        max_tokens=1024,
        system=PASS2_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )
    in_tokens = resp.usage.input_tokens
    out_tokens = resp.usage.output_tokens
    text_out = "".join(b.text for b in resp.content if b.type == "text").strip()
    if text_out.startswith("```"):
        text_out = re.sub(r"^```(?:json)?\s*\n?", "", text_out)
        text_out = re.sub(r"\n?```\s*$", "", text_out)
    try:
        parsed = json.loads(text_out)
    except json.JSONDecodeError as exc:
        logger.warning("Pass-2 JSON parse failed: %s — head=%r",
                       exc, text_out[:200])
        return [], in_tokens, out_tokens

    valid_verdicts = {"kept", "demoted", "rejected"}
    valid_ids = {r["id"] for r in rows}
    out: List[Pass2Verdict] = []
    for v in parsed.get("verdicts", []) or []:
        if not isinstance(v, dict):
            continue
        ad_id = v.get("asset_documents_id")
        verdict = v.get("verdict")
        if ad_id not in valid_ids or verdict not in valid_verdicts:
            continue
        confidence = float(v.get("confidence") or 0.0)
        confidence = max(0.0, min(1.0, confidence))
        out.append(Pass2Verdict(
            asset_documents_id=ad_id,
            verdict=verdict,
            confidence=confidence,
            reasoning=str(v.get("reasoning", ""))[:200],
        ))
    return out, in_tokens, out_tokens


def _apply_pass2_verdict(
    sb: SupabaseClient,
    verdict: Pass2Verdict,
) -> bool:
    """PATCH asset_documents with pass-2 results. 'rejected' also flips
    is_material=false. Never DELETEs."""
    from datetime import datetime, timezone
    patch: Dict[str, Any] = {
        "verified_by_pass2": True,
        "pass2_verdict": verdict.verdict,
        "pass2_confidence": round(verdict.confidence, 2),
        "pass2_at": datetime.now(timezone.utc).isoformat(),
    }
    if verdict.verdict in ("demoted", "rejected"):
        patch["is_material"] = False
    try:
        sb._rest(
            "PATCH", "asset_documents",
            params={"id": f"eq.{verdict.asset_documents_id}"},
            json_body=patch,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Pass-2 PATCH failed for %s: %s",
                       verdict.asset_documents_id, exc)
        return False


def pass2_main(argv: List[str] | None = None) -> int:
    """CLI entry. Walks pass-2 backlog in batches of PASS2_BATCH_SIZE,
    grouped by document for prompt-cache friendliness."""
    p = argparse.ArgumentParser(prog="asset_linker_pass2")
    p.add_argument("--asset-id", default=None,
                   help="Restrict to one fda_asset (default: all)")
    p.add_argument("--max-links", type=int, default=200,
                   help="Max links to verify this run")
    p.add_argument("--threshold", type=float, default=PASS2_DEFAULT_THRESHOLD,
                   help="Verify links with extraction_confidence below this")
    p.add_argument("--budget-usd", type=float, default=2.0,
                   help="Stop early if cumulative pass-2 cost exceeds this")
    p.add_argument("--dry-run", action="store_true",
                   help="Classify but do not write pass-2 fields")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        logger.error("ANTHROPIC_API_KEY not set")
        return 2

    sb = SupabaseClient()
    a_client = anthropic.Anthropic()
    stats = Pass2Stats()

    pending = _fetch_pass2_pending(
        sb,
        asset_id=args.asset_id,
        max_links=args.max_links,
        threshold=args.threshold,
    )
    stats.rows_seen = len(pending)
    if not pending:
        logger.info("No pass-2 backlog (asset_id=%s threshold=%s)",
                    args.asset_id, args.threshold)
        return 0
    logger.info("Pass-2 backlog: %d row(s); batching %d/call",
                len(pending), PASS2_BATCH_SIZE)

    # Lazy import to avoid pulling pricing into the asset_linker import path
    # for callers that only need pass-1.
    from orchestrator_runtime.pricing import estimate_cost as _pricing

    for i in range(0, len(pending), PASS2_BATCH_SIZE):
        if stats.cost_usd > args.budget_usd:
            logger.warning("Budget exceeded ($%.4f > $%.2f); stopping",
                           stats.cost_usd, args.budget_usd)
            break
        batch = pending[i:i + PASS2_BATCH_SIZE]
        try:
            verdicts, in_tok, out_tok = verify_link_pass2_batch(a_client, batch)
        except anthropic.APIError as exc:
            logger.warning("Pass-2 API error on batch starting %s: %s",
                           batch[0]["id"], exc)
            stats.errors += 1
            time.sleep(2.0)
            continue

        stats.batches_called += 1
        stats.api_calls += 1
        stats.input_tokens += in_tok
        stats.output_tokens += out_tok
        stats.cost_usd += _pricing(
            PASS2_MODEL, input_tokens=in_tok, output_tokens=out_tok,
        )

        for v in verdicts:
            if v.verdict == "kept":
                stats.kept += 1
            elif v.verdict == "demoted":
                stats.demoted += 1
            else:
                stats.rejected += 1
            if not args.dry_run:
                _apply_pass2_verdict(sb, v)

        logger.info(
            "batch %d (size=%d): %d verdicts (kept=%d demoted=%d rejected=%d) "
            "tokens in=%d out=%d cum_cost=$%.4f",
            stats.batches_called, len(batch), len(verdicts),
            sum(1 for x in verdicts if x.verdict == "kept"),
            sum(1 for x in verdicts if x.verdict == "demoted"),
            sum(1 for x in verdicts if x.verdict == "rejected"),
            in_tok, out_tok, stats.cost_usd,
        )

    logger.info("=" * 60)
    logger.info("Pass-2 summary:")
    logger.info("  rows_seen=%d  batches_called=%d  api_calls=%d  errors=%d",
                stats.rows_seen, stats.batches_called, stats.api_calls,
                stats.errors)
    logger.info("  verdicts: kept=%d demoted=%d rejected=%d",
                stats.kept, stats.demoted, stats.rejected)
    logger.info("  tokens: in=%d out=%d cost_usd=$%.4f",
                stats.input_tokens, stats.output_tokens, stats.cost_usd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
