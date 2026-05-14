"""One-shot backfill: trigger the v3 orchestrator for every active fda_asset
that has no convergence_assessment yet.

The orchestrator fires on `asset_documents` INSERT when
(link_type='primary' AND is_material=true). The simplest way to enqueue an
orchestrator_run for an asset is to write such a row. The Supabase trigger
`asset_documents_primary_reactor_wh` then dispatches the row to the reactor
edge function, which inserts into `orchestrator_runs` with trigger_type
'new_doc' (or 'cross_source' if another primary doc landed in the prior 24h).

For each active asset that lacks a convergence_assessment AND lacks a
pre-existing primary+material asset_documents link, this script:

  1. Searches `public.documents` for a representative match on the asset's
     drug_name (or generic_name) using a token + ILIKE strategy.
     Source priority: openfda > clinicaltrials > federal_register > edgar
                    > press_release > dailymed.
     Window: documents.published_at within the last 365 days.
  2. If a match is found: INSERT into `asset_documents`
     (link_type='primary', is_material=true, extraction_method='manual',
      extraction_confidence=1.0). The webhook trigger does the rest.
  3. If no match is found: write an `operator_flags` row
     (severity='warn', source='backfill_v3_assessment',
      kind='no_document_found') so a human can attach a document by hand.
  4. Successful enqueues are also logged to operator_flags
     (severity='info', kind='enqueued') for audit.

Idempotency:
  - asset_documents UNIQUE(asset_id, document_id, link_type) catches reruns.
  - operator_flags partial UNIQUE (source, kind, scanner_id, entity_id,
    signal_id, candidate_id) WHERE resolved_at IS NULL — we set entity_id
    so each asset's flag is unique. Reruns either find the prior open flag
    (skip) or the flag has been resolved (new row).
  - Drug names known to be garbage (e.g. 'EX-99' literal scraped from an
    EDGAR exhibit name) are skipped entirely with a corrupted_drug_name
    operator_flag — they need data-quality remediation upstream, not a
    fake primary doc link.

Out of scope:
  - Bridging signals → fda_assets for the 79% of orphaned FDA signals that
    have no asset row at all. This script only handles assets that already
    exist.

Run:
  python3 -m modal_workers.scripts.backfill_v3_assessments [--dry-run] [--limit N]
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from modal_workers.shared.supabase_client import SupabaseClient, SupabaseError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

# Search window for the representative document. Long lookback intentionally —
# many trial registrations were filed years before the current PDUFA date,
# and a 2022/2023 Phase-3 trial registration is still the canonical primary
# doc for an asset filing a 2026 NDA. Set to 1095d (3y) to capture them.
DOC_LOOKBACK_DAYS = 1095

# Documents.source preference for primary linking. The orchestrator weights
# regulatory + clinical primary sources above company news.
SOURCE_PRIORITY: List[str] = [
    "openfda",
    "clinicaltrials",
    "federal_register",
    "edgar",
    "press_release",
    "dailymed",
]

# Drug names where the scraped string is junk and using it for ILIKE search
# would either match nothing (and force every asset to operator_flag) or
# match an enormous set of unrelated docs (e.g. 'EX-99' is an EDGAR exhibit
# file naming convention, not a drug name). Treat them as data-quality bugs
# and surface via operator_flag for upstream remediation.
KNOWN_GARBAGE_DRUG_NAMES = {"ex-99", "exhibit 99", "exhibit-99"}

# Minimum token length we'll ILIKE against. Below this the match is too noisy.
MIN_TOKEN_LEN = 5


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------

@dataclass
class Stats:
    assets_seen: int = 0
    already_have_assessment: int = 0
    already_have_primary_doc: int = 0
    enqueued_via_new_doc: int = 0
    skipped_corrupted_drug: int = 0
    skipped_no_doc_found: int = 0
    errors: int = 0
    operator_flags_written: int = 0


@dataclass
class AssetCase:
    id: str
    ticker: str
    drug_name: str
    generic_name: Optional[str]
    entity_id: Optional[str]
    application_number: Optional[str]
    tokens: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# token extraction
# ---------------------------------------------------------------------------

_TOKEN_RX = re.compile(r"[A-Za-z][A-Za-z0-9-]+")


def extract_tokens(*candidates: Optional[str]) -> List[str]:
    """Pull plausible search tokens from drug_name / generic_name.

    Drops generic English words / qualifiers (cream, tablet, solution, etc.)
    and anything below MIN_TOKEN_LEN. Returns longest-first so the most
    specific match is tried before a noisier short token.
    """
    # Stop list: dosage forms, routes, generic chemistry suffixes, salts, and
    # common excipient names. These tokens, if used as an ILIKE probe, produce
    # high-rate false positives against unrelated docs (e.g. 'Carbonate'
    # matches SODIUM BICARBONATE labels, 'Formulation' matches "Long Acting
    # Injectable KarXT Formulation"). Verified empirically against the
    # 2026-05-14 backfill run.
    stop = {
        "cream", "tablet", "tablets", "solution", "injection", "capsule",
        "ophthalmic", "topical", "intravenous", "oral", "for", "the",
        "and", "with", "phase",
        # generic chemistry / salts / hydrates
        "sodium", "potassium", "calcium", "magnesium", "chloride", "sulfate",
        "carbonate", "bicarbonate", "phosphate", "citrate", "acetate",
        "hydrochloride", "hydrate", "dihydrate", "monohydrate", "anhydrous",
        "oxide", "hydroxide", "fumarate", "tartrate", "succinate", "maleate",
        # dosage forms / formulation qualifiers
        "formulation", "extended", "release", "modified", "immediate",
        "controlled", "delayed", "syrup", "suspension", "ointment", "patch",
        "lotion", "gel", "spray", "powder", "granules", "film", "drops",
        "implant", "depot", "infusion", "intramuscular", "subcutaneous",
        # label / clinical study qualifiers that match too generically
        "study", "trial", "clinical", "label", "package", "insert", "approval",
        "review", "report", "data", "single", "dose", "open", "ascending",
        "participants", "patients", "treatment", "therapy", "drug", "agent",
    }
    seen: Dict[str, None] = {}
    for cand in candidates:
        if not cand:
            continue
        for m in _TOKEN_RX.finditer(cand):
            tok = m.group(0)
            low = tok.lower()
            if len(tok) < MIN_TOKEN_LEN:
                continue
            if low in stop:
                continue
            seen.setdefault(tok, None)
    return sorted(seen.keys(), key=lambda t: (-len(t), t))


def is_garbage_drug_name(drug_name: str) -> bool:
    if not drug_name:
        return True
    return drug_name.strip().lower() in KNOWN_GARBAGE_DRUG_NAMES


# ---------------------------------------------------------------------------
# asset enumeration
# ---------------------------------------------------------------------------

def find_missing_assets(client: SupabaseClient, limit: int) -> List[AssetCase]:
    """Return active fda_assets that have no convergence_assessment row.

    We do a single GET on fda_assets and then filter out IDs that appear in
    convergence_assessments via a second GET — PostgREST doesn't support a
    NOT IN subquery directly, so this is the simplest two-call form.

    Tokens include drug_name + generic_name + extensions.compound_codes
    entries — the compound codes (e.g. CAP-1002 for Deramiocel) are how the
    trial registry actually names many of these programs.
    """
    active = client._rest(
        "GET",
        "fda_assets",
        params={
            "is_active": "eq.true",
            "select": ("id,ticker,drug_name,generic_name,entity_id,"
                       "application_number,extensions"),
            "limit": str(limit),
        },
    ) or []
    if not active:
        return []

    # Pull every asset_id that has at least one assessment.
    assessed = client._rest(
        "GET",
        "convergence_assessments",
        params={"select": "asset_id"},
    ) or []
    assessed_ids = {row["asset_id"] for row in assessed if row.get("asset_id")}

    cases: List[AssetCase] = []
    for row in active:
        if row["id"] in assessed_ids:
            continue
        ext = row.get("extensions") or {}
        compound_codes = ext.get("compound_codes") or []
        cases.append(AssetCase(
            id=row["id"],
            ticker=row.get("ticker") or "",
            drug_name=row.get("drug_name") or "",
            generic_name=row.get("generic_name"),
            entity_id=row.get("entity_id"),
            application_number=(row.get("application_number") or "").strip(),
            tokens=extract_tokens(
                row.get("drug_name"),
                row.get("generic_name"),
                *compound_codes,
            ),
        ))
    return cases


def has_primary_material_link(client: SupabaseClient, asset_id: str) -> bool:
    rows = client._rest(
        "GET",
        "asset_documents",
        params={
            "asset_id": f"eq.{asset_id}",
            "link_type": "eq.primary",
            "is_material": "eq.true",
            "select": "id",
            "limit": "1",
        },
    ) or []
    return bool(rows)


# ---------------------------------------------------------------------------
# representative-doc search
# ---------------------------------------------------------------------------

def find_representative_document(
    client: SupabaseClient, case: AssetCase
) -> Optional[Dict[str, Any]]:
    """Return the best documents row for the asset, or None.

    Strategy: iterate TOKEN-first (longest, most-specific token first), and
    within each token try sources in SOURCE_PRIORITY order. First non-empty
    result wins. The token-first ordering matters: it prevents a less-specific
    token (e.g. 'Carbonate' from 'Oxylanthanum Carbonate') matching an
    unrelated openfda label before the more-specific 'Oxylanthanum' has been
    tried on every source.

    We restrict to title-ILIKE (not raw_text) because raw_text matches drag
    in unrelated docs that merely mention the drug in a competitive-landscape
    paragraph — those are exactly what the asset_linker already marked as
    'mentions' / 'pipeline_context' rather than 'primary'.
    """
    # PostgREST doesn't accept SQL expressions in `gte.`, so compute the
    # cutoff client-side. ISO8601 with 'Z' is fine for timestamptz columns.
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(days=DOC_LOOKBACK_DAYS)).isoformat()

    for token in case.tokens:
        for source in SOURCE_PRIORITY:
            rows = client._rest(
                "GET",
                "documents",
                params={
                    "source": f"eq.{source}",
                    "title": f"ilike.*{token}*",
                    "published_at": f"gte.{cutoff}",
                    "select": "id,source,doc_type,title,published_at,url",
                    "order": "published_at.desc",
                    "limit": "1",
                },
            ) or []
            if rows:
                return rows[0]
    return None


# ---------------------------------------------------------------------------
# inserts
# ---------------------------------------------------------------------------

def insert_primary_asset_document(
    client: SupabaseClient,
    asset_id: str,
    document_id: str,
    *,
    dry_run: bool,
) -> str:
    """Insert the primary+material asset_documents row. Returns one of
    'inserted' / 'dedup' / 'error_<status>'. On 'inserted' the Supabase
    trigger asset_documents_primary_reactor_wh fires the reactor, which
    enqueues orchestrator_runs.
    """
    if dry_run:
        return "dry_run"
    payload = {
        "asset_id": asset_id,
        "document_id": document_id,
        "link_type": "primary",
        "extraction_method": "manual",
        "extraction_confidence": 1.0,
        "is_material": True,
        "verified_by_pass2": True,
    }
    try:
        client._rest(
            "POST",
            "asset_documents",
            json_body=payload,
            prefer="return=minimal",
        )
        return "inserted"
    except SupabaseError as exc:
        # 23505 unique_violation = the same (asset, doc, primary) triple
        # already exists. Treat as success (idempotent rerun).
        if exc.status == 409 or "23505" in (exc.body or ""):
            return "dedup"
        raise


def emit_operator_flag(
    client: SupabaseClient,
    *,
    case: AssetCase,
    severity: str,
    kind: str,
    title: str,
    body: str,
    evidence: Dict[str, Any],
    dry_run: bool,
) -> bool:
    """Emit an operator_flag. Returns True if a new row was written.

    Uses fda_assets.entity_id (when present) for the partial-unique key so
    each asset gets its own flag. When the asset has no entity_id (rare),
    the kind embeds the asset_id to keep the partial-unique tuple distinct.
    """
    if dry_run:
        return False
    effective_kind = kind if case.entity_id else f"{kind}:{case.id}"
    payload: Dict[str, Any] = {
        "severity": severity,
        "source": "backfill_v3_assessment",
        "kind": effective_kind,
        "title": title,
        "body": body,
        "evidence": evidence,
    }
    if case.entity_id:
        payload["entity_id"] = case.entity_id
    try:
        client._rest(
            "POST",
            "operator_flags",
            json_body=payload,
            prefer="return=minimal",
        )
        return True
    except SupabaseError as exc:
        if exc.status == 409 or "23505" in (exc.body or ""):
            # An open flag for this (source, kind, entity) already exists.
            return False
        raise


# ---------------------------------------------------------------------------
# per-asset
# ---------------------------------------------------------------------------

def process_asset(
    client: SupabaseClient, case: AssetCase, *, dry_run: bool, stats: Stats
) -> None:
    # Corrupted drug_name — don't fabricate a primary doc link. Flag and skip.
    if is_garbage_drug_name(case.drug_name):
        stats.skipped_corrupted_drug += 1
        wrote = emit_operator_flag(
            client,
            case=case,
            severity="warn",
            kind="corrupted_drug_name",
            title=f"{case.ticker} fda_assets.drug_name is unusable for backfill",
            body=(
                f"Cannot trigger orchestrator for asset {case.id} because "
                f"drug_name={case.drug_name!r} is a known-bad scrape "
                f"(likely from an EDGAR exhibit identifier). Curate the "
                f"asset row before re-running."
            ),
            evidence={
                "asset_id": case.id,
                "ticker": case.ticker,
                "drug_name": case.drug_name,
                "application_number": case.application_number,
            },
            dry_run=dry_run,
        )
        if wrote:
            stats.operator_flags_written += 1
        return

    if has_primary_material_link(client, case.id):
        # A primary+material link already exists — the orchestrator was
        # already triggered (or is pending). Nothing to do here; the
        # outer dispatch loop will pick it up.
        stats.already_have_primary_doc += 1
        return

    doc = find_representative_document(client, case)
    if doc is None:
        stats.skipped_no_doc_found += 1
        wrote = emit_operator_flag(
            client,
            case=case,
            severity="warn",
            kind="no_document_found",
            title=f"{case.ticker} {case.drug_name}: no representative document",
            body=(
                f"Backfill could not find a documents row matching the "
                f"asset tokens {case.tokens!r} on any priority source "
                f"({', '.join(SOURCE_PRIORITY)}) within the last "
                f"{DOC_LOOKBACK_DAYS} days. A human must either attach an "
                f"asset_documents row by hand or schedule an "
                f"operator_refresh orchestrator_run."
            ),
            evidence={
                "asset_id": case.id,
                "ticker": case.ticker,
                "drug_name": case.drug_name,
                "tokens_searched": case.tokens,
                "application_number": case.application_number,
            },
            dry_run=dry_run,
        )
        if wrote:
            stats.operator_flags_written += 1
        return

    # We have a doc. Insert the primary+material link. The Supabase trigger
    # asset_documents_primary_reactor_wh fires the reactor which enqueues
    # orchestrator_runs.
    try:
        result = insert_primary_asset_document(
            client, case.id, doc["id"], dry_run=dry_run
        )
    except Exception as exc:  # noqa: BLE001
        stats.errors += 1
        logger.warning("asset_documents INSERT failed for asset=%s doc=%s: %s",
                       case.id, doc["id"], exc)
        return

    if result in ("inserted", "dry_run"):
        stats.enqueued_via_new_doc += 1
    elif result == "dedup":
        stats.already_have_primary_doc += 1
        return

    wrote = emit_operator_flag(
        client,
        case=case,
        severity="info",
        kind="enqueued",
        title=f"{case.ticker} {case.drug_name}: orchestrator enqueue triggered",
        body=(
            f"Inserted primary+material asset_documents row linking asset "
            f"{case.id} to document {doc['id']} "
            f"(source={doc.get('source')}, doc_type={doc.get('doc_type')}, "
            f"published_at={doc.get('published_at')}). The reactor trigger "
            f"will enqueue orchestrator_runs with trigger_type=new_doc."
        ),
        evidence={
            "asset_id": case.id,
            "ticker": case.ticker,
            "drug_name": case.drug_name,
            "document_id": doc["id"],
            "document_source": doc.get("source"),
            "document_doc_type": doc.get("doc_type"),
            "document_title": doc.get("title"),
            "document_url": doc.get("url"),
            "matched_tokens": case.tokens,
            "dry_run": dry_run,
        },
        dry_run=dry_run,
    )
    if wrote:
        stats.operator_flags_written += 1


# ---------------------------------------------------------------------------
# residual count
# ---------------------------------------------------------------------------

def report_residual(client: SupabaseClient) -> int:
    """Count active assets that still lack a convergence_assessment.

    This is queried twice: once via PostgREST select=count for the active
    set, and once via the assessed set, then subtracted. Matches the SQL
    `WHERE is_active=true AND id NOT IN (SELECT asset_id FROM
    convergence_assessments WHERE asset_id IS NOT NULL)`.
    """
    active = client._rest(
        "GET",
        "fda_assets",
        params={
            "is_active": "eq.true",
            "select": "id",
            "limit": "1000",
        },
    ) or []
    assessed = client._rest(
        "GET",
        "convergence_assessments",
        params={"select": "asset_id"},
    ) or []
    assessed_ids = {row["asset_id"] for row in assessed if row.get("asset_id")}
    return sum(1 for a in active if a["id"] not in assessed_ids)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="backfill_v3_assessments")
    p.add_argument("--limit", type=int, default=200,
                   help="Max active assets to inspect per run")
    p.add_argument("--dry-run", action="store_true",
                   help="Do not INSERT — log what would happen")
    p.add_argument("--verbose", action="store_true",
                   help="Per-asset logging")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    client = SupabaseClient()
    stats = Stats()

    cases = find_missing_assets(client, limit=args.limit)
    stats.assets_seen = len(cases)
    logger.info("Found %d active fda_assets missing convergence_assessments",
                len(cases))

    for case in cases:
        if args.verbose:
            logger.debug("processing %s %s drug=%r tokens=%r",
                         case.ticker, case.id[:8], case.drug_name, case.tokens)
        try:
            process_asset(client, case, dry_run=args.dry_run, stats=stats)
        except Exception as exc:  # noqa: BLE001
            stats.errors += 1
            logger.exception("process_asset failed for %s: %s", case.id, exc)

    residual = report_residual(client)

    logger.info(
        "backfill summary: assets_seen=%d enqueued=%d already_primary=%d "
        "corrupted_drug=%d no_doc=%d errors=%d operator_flags=%d "
        "residual_missing_after_run=%d",
        stats.assets_seen, stats.enqueued_via_new_doc,
        stats.already_have_primary_doc, stats.skipped_corrupted_drug,
        stats.skipped_no_doc_found, stats.errors,
        stats.operator_flags_written, residual,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
