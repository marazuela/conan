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
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float = 0.0
    errors: int = 0
    marker_failures: int = 0   # _mark_classified PATCH failures (silent regression vector)


# Sonnet 4.5 pricing (USD per 1M tokens, as of plan-time):
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


def build_keyword_index(assets: List[Dict[str, Any]]
                        ) -> Dict[str, List[Dict[str, Any]]]:
    """Map keyword (drug_name / generic_name / sponsor_name tokens) →
    list of {"asset": a, "field": fld} entries. Used by the regex pre-filter.

    `indication` was previously indexed but caused 61% of docs to pass the
    prefilter against a <3% true match rate — common-condition strings like
    "type 2 diabetes" or "thyroid eye disease" leak into unrelated drug
    labels. Sonnet correctly rejects them but the prefilter pays the input
    bill. Dropped in response to the dailymed cost incident.

    The {"field": fld} tag is consumed by prefilter_doc to enforce
    source-aware gates: drug-label / trial sources require a drug_name or
    generic_name hit (sponsor-only hits are the 2026-05-11 leak vector that
    burned ~$40 on ~2400 false-positive dailymed labels).
    """
    idx: Dict[str, List[Dict[str, Any]]] = {}
    for a in assets:
        for fld in ("drug_name", "generic_name", "sponsor_name"):
            val = (a.get(fld) or "").strip()
            if not val:
                continue
            for kw in _keywords_from(val, fld):
                idx.setdefault(kw.lower(), []).append({"asset": a, "field": fld})
    return idx


# Pharma-industry boilerplate words that appear in many sponsor_name strings
# AND inside unrelated dailymed drug labels — indexing them as keywords
# leaks ~30% of dailymed docs through the prefilter to Sonnet for $0 of true
# matches. Stripped 2026-05-20 after the indication-removal pass-rate stayed
# at ~50% instead of dropping to ~15%.
SPONSOR_STOPWORDS = frozenset({
    # Corporate-form suffixes (case-sensitive, matched as title-case tokens)
    "Corporation", "Corporate", "Limited", "Holdings", "Holding",
    "Group", "Industries", "Company", "Companies",
    # Pharma industry boilerplate — these are the worst offenders
    "Therapeutics", "Therapeutic", "Therapy",
    "Pharmaceuticals", "Pharmaceutical", "Pharma", "Pharmacy",
    "Sciences", "Science", "Bioscience", "Biosciences",
    "Medicines", "Medicine", "Medical", "Health", "Healthcare",
    "Biotech", "BioPharma", "Biopharmaceuticals",
    # Generic-sounding modifiers commonly used in pharma brand names
    "Precision", "Advanced", "Innovative", "Life", "Lab", "Labs",
    "Global", "International", "Worldwide",
})


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
        # Find all 4+-char Title-case tokens, drop pharma boilerplate, take
        # the first 2 specific tokens. If filtering empties the list, prefer
        # NO keyword over a stopword keyword — drug_name + generic_name
        # already cover the asset.
        tokens = re.findall(r"\b[A-Z][\w-]{3,}\b", value)
        specific = [t for t in tokens if t not in SPONSOR_STOPWORDS]
        return specific[:2]
    return []


# ---------------------------------------------------------------------------
# Document loading
# ---------------------------------------------------------------------------

def load_documents_to_link(client: SupabaseClient, max_docs: int = 200,
                           doc_ids: Optional[List[str]] = None
                           ) -> List[Dict[str, Any]]:
    """Pull documents that have not yet been classified by pass-1. Newest-first
    so the most recent material lands quickly during a backfill.

    `doc_ids` is an operator-only override that bypasses the unclassified
    filter and pulls the specified rows directly — used by the cache-validation
    test path. The cron path never sets it.

    Uses documents.linker_classified_at IS NULL (backed by the partial index
    documents_linker_unclassified_idx). A terminal outcome — linked, no_match,
    or parse_error — sets linker_classified_at; transient API errors leave it
    NULL so the next run retries.
    """
    select = ",".join([
        "id", "source", "doc_type", "title", "url",
        "raw_text", "raw_text_tokens", "storage_path",
        "published_at", "extensions",
    ])
    if doc_ids:
        rows = client._rest(
            "GET", "documents",
            params={
                "select": select,
                "id": f"in.({','.join(doc_ids)})",
            },
        ) or []
        return rows
    rows = client._rest(
        "GET", "documents",
        params={
            "select": select,
            "linker_classified_at": "is.null",
            "order": "published_at.desc",
            "limit": str(max_docs),
        },
    ) or []
    return rows


def _mark_classified(client: SupabaseClient, doc_id: str, result: str) -> bool:
    """Stamp documents.linker_classified_at + result for a terminal pass-1
    outcome. `result` is one of: linked, no_match, parse_error.

    Returns True on success, False on PATCH failure. Caller MUST increment
    `LinkerStats.marker_failures` on False — a silent failure here would
    leave the doc unmarked and the next cron run re-Sonnets it, regressing
    the very bug the marker mechanism was added to prevent.
    """
    from datetime import datetime, timezone
    try:
        client._rest(
            "PATCH", "documents",
            params={"id": f"eq.{doc_id}"},
            json_body={
                "linker_classified_at": datetime.now(timezone.utc).isoformat(),
                "linker_classified_result": result,
            },
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("mark_classified PATCH failed for doc %s (%s): %s",
                       doc_id, result, exc)
        return False


STALE_RUNNING_AFTER_MINUTES = 30
LOCK_CONFLICT = "asset_linker_runs_one_running_per_pass"


def _start_run_row(client: SupabaseClient, pass_name: str, model: str) -> tuple[Optional[str], bool]:
    """Acquire the per-pass concurrency lock by inserting a row with
    status='running'. Returns (run_id, lock_held).

    Steps:
      1. Reclaim any 'running' row that started >30min ago (zombie from a
         crashed previous run) by PATCHing its status to 'failed'.
      2. INSERT a new 'running' row. The partial unique index
         asset_linker_runs_one_running_per_pass enforces at most one running
         row per pass — a 409 here means another instance is actively running
         and this caller should skip cleanly.

    lock_held=False means we did NOT acquire the lock — the caller should
    return early without doing any work. lock_held=True with run_id=None
    means the INSERT succeeded but the response didn't return a row (rare
    PostgREST quirk); caller should proceed but observability is degraded.
    """
    from datetime import datetime, timedelta, timezone
    cutoff_iso = (datetime.now(timezone.utc)
                  - timedelta(minutes=STALE_RUNNING_AFTER_MINUTES)).isoformat()
    try:
        client._rest(
            "PATCH", "asset_linker_runs",
            params={
                "pass": f"eq.{pass_name}",
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
        # Reclaim failure is non-fatal — INSERT will still fail correctly if
        # an active run holds the lock.
        logger.warning("stale-row reclaim failed (continuing): %s", exc)

    try:
        res = client._rest(
            "POST", "asset_linker_runs",
            json_body={"pass": pass_name, "model": model, "status": "running"},
            prefer="return=representation",
        )
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if LOCK_CONFLICT in msg or "23505" in msg or "duplicate key" in msg.lower():
            logger.info("asset_linker %s lock held by concurrent run — skipping",
                        pass_name)
            return None, False
        logger.warning("asset_linker_runs INSERT failed (proceeding without "
                       "observability row): %s", exc)
        return None, True
    if res and isinstance(res, list) and res:
        return res[0].get("id"), True
    return None, True


def _finish_run_row(
    client: SupabaseClient,
    run_id: Optional[str],
    status: str,
    stats: "LinkerStats | Pass2Stats",
) -> None:
    """PATCH the run row with terminal stats. Best-effort — silent on failure.

    marker_failures is surfaced in the notes column when non-zero so an
    operator scanning asset_linker_runs can spot silent regression of the
    documents.linker_classified_at mechanism (the original incident vector).
    """
    if not run_id:
        return
    from datetime import datetime, timezone
    marker_failures = getattr(stats, "marker_failures", 0)
    patch: Dict[str, Any] = {
        "status": status,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "api_calls": getattr(stats, "api_calls", 0),
        "errors": getattr(stats, "errors", 0),
        "input_tokens": getattr(stats, "input_tokens", 0),
        "output_tokens": getattr(stats, "output_tokens", 0),
        "cache_read_tokens": getattr(stats, "cache_read_tokens", 0),
        "cache_creation_tokens": getattr(stats, "cache_creation_tokens", 0),
        "cost_usd": round(float(getattr(stats, "cost_usd", 0.0)), 4),
    }
    if marker_failures > 0:
        patch["notes"] = (f"marker_failures={marker_failures} — these docs "
                          "were classified but NOT stamped; next run will "
                          "re-Sonnet them")
    # Pass-1 only fields
    if isinstance(stats, LinkerStats):
        patch["docs_seen"] = stats.docs_seen
        patch["prefilter_passed"] = stats.docs_prefilter_passed
        patch["prefilter_skipped"] = stats.docs_prefilter_skipped
        patch["links_inserted"] = stats.links_inserted
        patch["links_dedup_skipped"] = stats.links_dedup_skipped
    # Pass-2 only fields
    else:
        patch["docs_seen"] = stats.rows_seen
        patch["links_inserted"] = stats.kept + stats.demoted + stats.rejected
    try:
        client._rest(
            "PATCH", "asset_linker_runs",
            params={"id": f"eq.{run_id}"},
            json_body=patch,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("asset_linker_runs finish PATCH failed for %s: %s",
                       run_id, exc)


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

# Sources where sponsor-name matches alone are NOT sufficient to fire Sonnet —
# drug labels, FDA-application records, and trial listings rarely mention a
# sponsor without also naming the specific drug. Sponsor-only hits are the
# dominant false-positive vector here (big-pharma sponsors like Pfizer/BMS/
# AstraZeneca appear on hundreds of unrelated dailymed labels). For these
# sources we require a drug_name or generic_name hit. SEC sources (10-K/Q/8-K)
# keep the full prefilter — sponsor-only matches in competitor filings are
# valuable signal there.
SPONSOR_ONLY_INSUFFICIENT_SOURCES = frozenset({
    "dailymed", "openfda", "clinicaltrials",
})

# Doc types that historically yield ~0 links AND have a high parse-error rate
# (Sonnet returns malformed JSON on these huge structured legalistic filings).
# Observed 2026-05-11: 50 edgar 424B2 docs classified → 0 links, 25
# parse_errors. These are SEC registration/prospectus filings — pipeline
# disclosures are summarized but rarely material per-asset. Skip without
# Sonnet; the existing 'no_match' marker stamps them so they don't re-fire.
PREFILTER_EXCLUDED_DOC_TYPES = frozenset({
    "424B2", "424B3", "424B4", "424B5",
    "S-1", "S-1/A", "S-3", "S-3/A",
})


def _compile_keyword_patterns(keyword_index: Dict[str, Any]
                              ) -> Dict[str, "re.Pattern[str]"]:
    """Pre-compile word-boundary regex per keyword. Word boundaries prevent
    short tokens like 'Vanda' from matching inside unrelated identifiers
    (e.g. 'Vandalism'), which was a precision leak under the older substring
    check."""
    return {kw: re.compile(r"\b" + re.escape(kw) + r"\b", re.IGNORECASE)
            for kw in keyword_index}


def prefilter_doc(text: str, keyword_index: Dict[str, List[Dict[str, Any]]],
                  source: Optional[str] = None,
                  doc_type: Optional[str] = None,
                  keyword_patterns: Optional[Dict[str, "re.Pattern[str]"]] = None
                  ) -> List[Dict[str, Any]]:
    """Returns the assets whose keywords appear in `text`. Empty list = skip.

    Three gates, cheapest first:
      1. doc_type ∈ PREFILTER_EXCLUDED_DOC_TYPES → empty (no regex scan).
      2. Keyword match via word-boundary regex on `text`.
      3. For drug-label / FDA-app / trial sources, drop assets whose only
         matched field was sponsor_name (sponsor-only insufficient).

    keyword_patterns is an optional pre-compiled regex cache; if None we
    compile on the fly (fine for tests, wasteful for production loops).
    """
    if doc_type and doc_type in PREFILTER_EXCLUDED_DOC_TYPES:
        return []

    if keyword_patterns is None:
        keyword_patterns = _compile_keyword_patterns(keyword_index)

    matched_fields: Dict[str, set[str]] = {}
    asset_by_id: Dict[str, Dict[str, Any]] = {}
    for kw, entries in keyword_index.items():
        pat = keyword_patterns.get(kw)
        if pat is None or not pat.search(text):
            continue
        for entry in entries:
            a = entry["asset"]
            asset_by_id[a["id"]] = a
            matched_fields.setdefault(a["id"], set()).add(entry["field"])

    sponsor_only_ok = source not in SPONSOR_ONLY_INSUFFICIENT_SOURCES
    out: List[Dict[str, Any]] = []
    for asset_id, fields in matched_fields.items():
        if not sponsor_only_ok and fields == {"sponsor_name"}:
            continue
        out.append(asset_by_id[asset_id])
    return out


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


def _build_asset_card(candidate_assets: List[Dict[str, Any]]) -> str:
    return "\n".join(
        f"- asset_id={a['id']} | drug_name={a.get('drug_name', '?')} | "
        f"generic={a.get('generic_name', '?')} | sponsor={a.get('sponsor_name', '?')} | "
        f"indication={a.get('indication', '?')}"
        for a in candidate_assets
    )


def classify_document(
    client: anthropic.Anthropic,
    doc: Dict[str, Any],
    candidate_assets: List[Dict[str, Any]],
    text: str,
    matched_keywords: List[str],
) -> tuple[List[LinkResult], int, int, int, int, bool]:
    """Send a single document to Sonnet for classification. Returns
    (links, input_tokens, output_tokens, cache_read_tokens,
     cache_creation_tokens, parse_ok). parse_ok=False means Sonnet returned
    malformed JSON — the call was paid for but no links can be extracted;
    the caller should mark the doc as parse_error so it isn't retried on
    the next cron run.

    Uses prompt caching on the two stable prefix blocks (SYSTEM_PROMPT and
    the asset_card) so cost amortizes across the ~100-200 docs in a single
    cron tick. Doc text remains the un-cached tail.
    """
    trimmed = trim_around_matches(text, matched_keywords)
    asset_card = _build_asset_card(candidate_assets)

    user_content = f"""Document metadata:
- source: {doc.get('source')}
- doc_type: {doc.get('doc_type')}
- title: {doc.get('title') or '(none)'}
- url: {doc.get('url') or '(none)'}
- published_at: {doc.get('published_at')}

Document text (possibly trimmed for length — sections separated by […trim…]):

{trimmed}
"""

    # Single cache breakpoint at the end of [SYSTEM_PROMPT + asset_card].
    # Anthropic enforces a 1024-token minimum cached prefix for Sonnet. At the
    # current ~34-asset watchlist the tokenized prefix is ~620 tokens, BELOW
    # the threshold — the API silently returns cache_*_input_tokens=0 and we
    # pay the full input rate. The cache_control is kept as a forward-looking
    # no-op: once the watchlist grows past ~50 assets (≈1024 tokens of
    # asset_card), caching engages automatically without code changes.
    system_blocks = [
        {"type": "text", "text": SYSTEM_PROMPT},
        {"type": "text",
         "text": "Tracked assets (identify which, if any, the document "
                 f"references):\n\n{asset_card}",
         "cache_control": {"type": "ephemeral"}},
    ]

    resp = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        system=system_blocks,
        messages=[{"role": "user", "content": user_content}],
    )
    in_tokens = resp.usage.input_tokens
    out_tokens = resp.usage.output_tokens
    cache_read = getattr(resp.usage, "cache_read_input_tokens", 0) or 0
    cache_create = getattr(resp.usage, "cache_creation_input_tokens", 0) or 0

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
        return [], in_tokens, out_tokens, cache_read, cache_create, False

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
    return out, in_tokens, out_tokens, cache_read, cache_create, True


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
    p.add_argument("--max", type=int, default=100,
                   help="Max documents to process this run (reduced from 200 "
                        "on 2026-05-11 to bound Anthropic rate-limit exposure; "
                        "the */15 cron tick can still drain a backlog at "
                        "400 docs/hour)")
    p.add_argument("--dry-run", action="store_true",
                   help="Classify but do not insert asset_documents rows")
    p.add_argument("--budget-usd", type=float, default=5.0,
                   help="Stop early if cumulative cost exceeds this (reduced "
                        "from $15 on 2026-05-11; with cache_control + max=100 "
                        "a typical tick should cost <$1)")
    p.add_argument("--ignore-24h-halt", action="store_true",
                   help="Bypass the 24h global hard-halt check. Operator "
                        "override for one-off test invocations; the cron "
                        "should never set this.")
    p.add_argument("--doc-ids", default=None,
                   help="Comma-separated document IDs. Bypasses the "
                        "unclassified-newest-first queue and runs against "
                        "the specified docs. Operator override for the "
                        "cache-validation test path.")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        logger.error("ANTHROPIC_API_KEY not set")
        return 2

    sb = SupabaseClient()

    # 24h hard halt — check BEFORE acquiring lock or fetching docs. Skipped
    # in dry-run because the user is explicitly invoking the tool offline,
    # and skipped behind --ignore-24h-halt for operator-driven test runs.
    if not args.dry_run and not args.ignore_24h_halt:
        from modal_workers.shared.cost_budget import (
            check_asset_linker_hard_halt,
        )
        halt_status = check_asset_linker_hard_halt(sb)
        if halt_status["halt"]:
            logger.error(
                "asset_linker 24h spend $%.2f has reached the hard halt — "
                "exiting without work. (See operator_flags for the breach.)",
                halt_status["total_24h_usd"],
            )
            return 0
    elif args.ignore_24h_halt:
        logger.warning("--ignore-24h-halt active: 24h hard halt bypassed.")

    a_client = anthropic.Anthropic()
    stats = LinkerStats()
    run_id: Optional[str] = None
    lock_held = True  # dry-run path doesn't take the lock
    if not args.dry_run:
        run_id, lock_held = _start_run_row(sb, "pass1", MODEL)
        if not lock_held:
            logger.info("Another pass1 run is active — exiting without work.")
            return 0
    budget_exceeded = False
    crashed = False

    try:
        assets = load_active_assets(sb, only_asset_id=args.asset_id)
        if not assets:
            logger.error("No active fda_assets found")
            return 1
        logger.info("Loaded %d active asset(s)", len(assets))

        keyword_index = build_keyword_index(assets)
        keyword_patterns = _compile_keyword_patterns(keyword_index)
        logger.info("Built keyword index with %d unique keywords (compiled "
                    "to word-boundary regex)", len(keyword_index))

        doc_ids = ([d.strip() for d in args.doc_ids.split(",") if d.strip()]
                   if args.doc_ids else None)
        docs = load_documents_to_link(sb, max_docs=args.max, doc_ids=doc_ids)
        logger.info("Loaded %d unlinked document(s)", len(docs))
        stats.docs_seen = len(docs)

        for doc in docs:
            if stats.cost_usd > args.budget_usd:
                logger.warning("Budget exceeded ($%.2f > $%.2f); stopping early",
                               stats.cost_usd, args.budget_usd)
                budget_exceeded = True
                break

            text = _load_doc_text(doc, sb)
            if not text:
                logger.warning("doc %s has no text; skipping", doc["id"])
                stats.errors += 1
                continue

            candidates = prefilter_doc(text, keyword_index,
                                       source=doc.get("source"),
                                       doc_type=doc.get("doc_type"),
                                       keyword_patterns=keyword_patterns)
            if not candidates:
                stats.docs_prefilter_skipped += 1
                # Prefilter is deterministic given the current active asset set —
                # mark as no_match so the doc doesn't reappear next cron run. If
                # new assets are added later, a separate trigger should reset
                # linker_classified_at = NULL for docs needing re-evaluation.
                if not args.dry_run and not _mark_classified(sb, doc["id"], "no_match"):
                    stats.marker_failures += 1
                continue
            stats.docs_prefilter_passed += 1

            # Collect distinct keywords that triggered the match (for trimming)
            matched_kws: List[str] = []
            text_lower = text.lower()
            for kw in keyword_index:
                if kw in text_lower:
                    matched_kws.append(kw)

            try:
                (links, in_tok, out_tok, cache_read_tok,
                 cache_create_tok, parse_ok) = classify_document(
                    a_client, doc, candidates, text, matched_kws,
                )
            except anthropic.APIError as exc:
                # Transient — don't mark, let the next cron run retry.
                logger.warning("API error for doc %s: %s", doc["id"], exc)
                stats.errors += 1
                time.sleep(2.0)
                continue
            except Exception as exc:  # noqa: BLE001
                # Catch-all for non-Anthropic surprises (httpx errors, malformed
                # response shapes, etc.). Treat like a transient APIError: don't
                # mark, don't let one bad doc kill the whole run. Without this
                # broader catch, an uncaught exception leaves the run row in
                # status='running' forever and breaks the next 15-min cron tick.
                logger.exception("Unexpected error classifying doc %s: %s",
                                 doc["id"], exc)
                stats.errors += 1
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
            stats.docs_classified += 1

            if not parse_ok:
                # Sonnet returned malformed JSON. The call was paid for; retrying
                # almost certainly produces the same garbage. Mark to stop the loop.
                stats.errors += 1
                if not args.dry_run and not _mark_classified(sb, doc["id"], "parse_error"):
                    stats.marker_failures += 1
                continue

            if not links:
                logger.info("doc %s [%s] %s — no links emitted (cost so far $%.3f)",
                            doc["id"], doc.get("source"), doc.get("doc_type"),
                            stats.cost_usd)
                if not args.dry_run and not _mark_classified(sb, doc["id"], "no_match"):
                    stats.marker_failures += 1
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
            if not _mark_classified(sb, doc["id"], "linked"):
                stats.marker_failures += 1
            logger.info(
                "doc %s [%s] %s -> %d link(s) inserted, %d skipped (cost $%.3f)",
                doc["id"], doc.get("source"), doc.get("doc_type"),
                inserted, skipped, stats.cost_usd,
            )
    except Exception as exc:  # noqa: BLE001
        # Uncaught exception escaped the per-doc loop — finalize the run row
        # with status='failed' so observability + the watchdog can see it,
        # and we don't leave a zombie status='running' row blocking the next
        # cron tick (via the partial unique index).
        logger.exception("Unexpected exception in asset_linker main: %s", exc)
        crashed = True
    finally:
        logger.info("=" * 60)
        logger.info("Linker summary:")
        logger.info("  docs_seen=%d  prefilter_passed=%d  prefilter_skipped=%d",
                    stats.docs_seen, stats.docs_prefilter_passed, stats.docs_prefilter_skipped)
        logger.info("  docs_classified=%d  api_calls=%d  errors=%d  marker_failures=%d",
                    stats.docs_classified, stats.api_calls, stats.errors,
                    stats.marker_failures)
        logger.info("  links_inserted=%d  links_dedup_skipped=%d",
                    stats.links_inserted, stats.links_dedup_skipped)
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
        _finish_run_row(sb, run_id, final_status, stats)
    return 1 if crashed else 0


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
    marker_failures: int = 0   # PATCH failures on pass2_verdict writes


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
    run_id: Optional[str] = None
    lock_held = True
    if not args.dry_run:
        run_id, lock_held = _start_run_row(sb, "pass2", PASS2_MODEL)
        if not lock_held:
            logger.info("Another pass2 run is active — exiting without work.")
            return 0
    budget_exceeded = False
    crashed = False

    try:
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
                budget_exceeded = True
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
            except Exception as exc:  # noqa: BLE001
                logger.exception("Pass-2 unexpected error on batch starting %s: %s",
                                 batch[0]["id"], exc)
                stats.errors += 1
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
                    if not _apply_pass2_verdict(sb, v):
                        stats.marker_failures += 1

            logger.info(
                "batch %d (size=%d): %d verdicts (kept=%d demoted=%d rejected=%d) "
                "tokens in=%d out=%d cum_cost=$%.4f",
                stats.batches_called, len(batch), len(verdicts),
                sum(1 for x in verdicts if x.verdict == "kept"),
                sum(1 for x in verdicts if x.verdict == "demoted"),
                sum(1 for x in verdicts if x.verdict == "rejected"),
                in_tok, out_tok, stats.cost_usd,
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected exception in asset_linker pass2_main: %s", exc)
        crashed = True
    finally:
        logger.info("=" * 60)
        logger.info("Pass-2 summary:")
        logger.info("  rows_seen=%d  batches_called=%d  api_calls=%d  errors=%d  marker_failures=%d",
                    stats.rows_seen, stats.batches_called, stats.api_calls,
                    stats.errors, stats.marker_failures)
        logger.info("  verdicts: kept=%d demoted=%d rejected=%d",
                    stats.kept, stats.demoted, stats.rejected)
        logger.info("  tokens: in=%d out=%d cost_usd=$%.4f",
                    stats.input_tokens, stats.output_tokens, stats.cost_usd)
        if crashed:
            final_status = "failed"
        elif budget_exceeded:
            final_status = "budget_exceeded"
        else:
            final_status = "completed"
        _finish_run_row(sb, run_id, final_status, stats)
    return 1 if crashed else 0


if __name__ == "__main__":
    sys.exit(main())
