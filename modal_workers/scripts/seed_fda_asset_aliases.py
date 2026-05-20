"""seed_fda_asset_aliases — populate fda_asset_aliases from three sources.

Feeds the deterministic doc/asset prefilter. Empty aliases collapse recall
back to ticker/drug_name only, which is what the prefilter does without
Layer-2 enrichment. The three sources here lift recall by adding brand
names, generic↔brand crosswalks, NCT IDs, and code names (LY3502970-style).

Sources, in order of cost and confidence:

  1. ``openfda_label`` — openFDA ``/drug/label`` searched by the asset's
     generic_name. Every distinct ``openfda.brand_name`` becomes
     ``alias_kind='brand'``; every distinct ``openfda.generic_name``
     becomes ``alias_kind='generic'``. ~1 API call per active asset.

  2. ``clinicaltrials_v2`` — ClinicalTrials.gov v2 ``/studies`` searched
     by intervention (drug_name) + lead sponsor. Yields NCT IDs (one
     per matching trial) AND
     ``protocolSection.armsInterventionsModule.interventions[].otherNames[]``
     which is where code names like LY3502970 surface. ~1 API call per asset.

  3. ``extensions_mining`` — SQL pass over already-linked corpus:
     ``SELECT extensions FROM documents JOIN asset_documents`` for each
     asset; surface NCT IDs and intervention "otherNames" present in
     ``documents.extensions`` that aren't yet in fda_asset_aliases.

The ``curated_map`` source (sponsor_alias / sponsor_stem) was removed
2026-05-20: sponsor matches were dominating the prefilter queue at very
low precision (Pfizer alone produced 570 edges in one hash window), and
sponsor_alias / sponsor_stem are no longer valid alias_kind values.
Synthetic abbreviations are intentionally NOT seeded — false-positive rate
without curation is too high; the ``source='synthetic'`` slot is reserved
for a future pass.

CLI:

  python -m modal_workers.scripts.seed_fda_asset_aliases \\
      [--asset-id UUID]          # restrict to one asset (smoke)
      [--sources openfda_label,clinicaltrials_v2,extensions_mining]
      [--dry-run]                # log proposed inserts, don't write
      [--max-assets N]           # truncate the active-asset list (testing)

Idempotent: ``ON CONFLICT (asset_id, alias_normalized, alias_kind) DO NOTHING``.
Emits one ``asset_linker_runs`` row per invocation with ``pass='seed'``,
``model='seed-script'``, and token cost zero.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import requests

from modal_workers.shared.supabase_client import SupabaseClient

logger = logging.getLogger("seed_fda_asset_aliases")

ALL_SOURCES = (
    "openfda_label",
    "clinicaltrials_v2",
    "extensions_mining",
)

# Mirrors the DB-side CHECK on fda_asset_aliases.alias_normalized. Anything in
# this set will be rejected by the constraint; we filter client-side so a single
# bad alias doesn't blow up the whole batch.
NORMALIZED_BLOCKLIST = {
    "peptide", "concept", "default", "ex-99", "(auto-discovered)",
    "nucleotide", "drug", "tablet", "capsule", "injection",
}

NCT_PATTERN = re.compile(r"^nct[0-9]{8}$")
CODE_ALIAS_SIGNAL_PATTERN = re.compile(r"[0-9.-]")
LOW_INFORMATION_CODE_TERMS = (
    "placebo",
    "vehicle",
)

OPENFDA_BASE = "https://api.fda.gov"
CT_BASE = "https://clinicaltrials.gov/api/v2"
DEFAULT_HTTP_TIMEOUT_S = 20.0
INTER_REQUEST_SLEEP_S = 0.25  # gentle pacing to stay under public-tier limits

ALIAS_KINDS = frozenset({
    "brand", "generic", "code", "nct_id", "abbreviation", "drug_name",
})


@dataclass(frozen=True)
class AliasCandidate:
    asset_id: str
    alias: str
    alias_normalized: str
    alias_kind: str
    source: str
    source_ref: Optional[str] = None


@dataclass
class SeedStats:
    assets_processed: int = 0
    candidates_proposed: int = 0
    rows_inserted: int = 0
    api_calls: int = 0
    errors: int = 0
    by_source: Dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def normalize(s: str) -> str:
    return s.lower().strip()


def is_valid_alias(alias_normalized: str, alias_kind: str) -> bool:
    """Mirror the DB CHECKs client-side. False = drop before insert."""
    if not alias_normalized:
        return False
    if len(alias_normalized) < 3:
        return False
    if alias_normalized in NORMALIZED_BLOCKLIST:
        return False
    if alias_kind not in ALIAS_KINDS:
        return False
    if alias_kind == "nct_id" and not NCT_PATTERN.match(alias_normalized):
        return False
    if alias_kind == "code":
        if any(term in alias_normalized for term in LOW_INFORMATION_CODE_TERMS):
            return False
        if not CODE_ALIAS_SIGNAL_PATTERN.search(alias_normalized):
            return False
    return True


def make_candidate(asset_id: str, alias: str, kind: str, source: str,
                   source_ref: Optional[str] = None) -> Optional[AliasCandidate]:
    n = normalize(alias)
    if not is_valid_alias(n, kind):
        return None
    return AliasCandidate(
        asset_id=asset_id,
        alias=alias.strip(),
        alias_normalized=n,
        alias_kind=kind,
        source=source,
        source_ref=source_ref,
    )


# ---------------------------------------------------------------------------
# Source 1: openFDA /drug/label
# ---------------------------------------------------------------------------

def _openfda_get(path: str, params: Dict[str, Any],
                 session: requests.Session,
                 *, attempts: int = 3) -> Optional[Dict[str, Any]]:
    url = f"{OPENFDA_BASE}/{path.lstrip('/')}"
    for attempt in range(attempts):
        r = session.get(url, params=params, timeout=DEFAULT_HTTP_TIMEOUT_S)
        if r.status_code == 404:
            return None
        if r.status_code in (429,) or r.status_code >= 500:
            if attempt < attempts - 1:
                time.sleep(0.5 * (2 ** attempt))
                continue
            logger.warning("openfda %s: %d after %d attempts",
                           path, r.status_code, attempts)
            return None
        if r.status_code >= 400:
            logger.warning("openfda %s: %d (%s)", path, r.status_code,
                           r.text[:120])
            return None
        try:
            return r.json()
        except ValueError:
            return None
    return None


def aliases_from_openfda(asset: Dict[str, Any],
                         session: requests.Session,
                         stats: SeedStats) -> List[AliasCandidate]:
    """Query openFDA /drug/label by generic_name; harvest brand_name and
    generic_name from openfda metadata blocks of returned labels."""
    out: List[AliasCandidate] = []
    asset_id = asset["id"]
    generic = asset.get("generic_name") or asset.get("drug_name")
    if not generic:
        return out

    # search=openfda.generic_name:"X"
    query = f'openfda.generic_name:"{generic}"'
    body = _openfda_get("drug/label.json",
                       {"search": query, "limit": 50},
                       session)
    stats.api_calls += 1
    time.sleep(INTER_REQUEST_SLEEP_S)
    if not body:
        return out

    results = body.get("results") or []
    seen: Set[Tuple[str, str]] = set()

    for label in results:
        set_id = label.get("set_id")
        meta = label.get("openfda") or {}
        for brand in (meta.get("brand_name") or []):
            cand = make_candidate(asset_id, brand, "brand",
                                  "openfda_label", source_ref=set_id)
            if cand and (cand.alias_normalized, cand.alias_kind) not in seen:
                seen.add((cand.alias_normalized, cand.alias_kind))
                out.append(cand)
        for gen in (meta.get("generic_name") or []):
            cand = make_candidate(asset_id, gen, "generic",
                                  "openfda_label", source_ref=set_id)
            if cand and (cand.alias_normalized, cand.alias_kind) not in seen:
                seen.add((cand.alias_normalized, cand.alias_kind))
                out.append(cand)

    return out


# ---------------------------------------------------------------------------
# Source 2: ClinicalTrials.gov v2
# ---------------------------------------------------------------------------

def _ct_get(path: str, params: Dict[str, Any],
            session: requests.Session,
            *, attempts: int = 3) -> Optional[Dict[str, Any]]:
    url = f"{CT_BASE}/{path.lstrip('/')}"
    for attempt in range(attempts):
        r = session.get(url, params=params, timeout=DEFAULT_HTTP_TIMEOUT_S)
        if r.status_code == 404:
            return None
        if r.status_code in (429,) or r.status_code >= 500:
            if attempt < attempts - 1:
                time.sleep(0.5 * (2 ** attempt))
                continue
            logger.warning("clinicaltrials %s: %d after %d attempts",
                           path, r.status_code, attempts)
            return None
        if r.status_code >= 400:
            logger.warning("clinicaltrials %s: %d (%s)", path, r.status_code,
                           r.text[:120])
            return None
        try:
            return r.json()
        except ValueError:
            return None
    return None


def aliases_from_clinicaltrials(asset: Dict[str, Any],
                                session: requests.Session,
                                stats: SeedStats) -> List[AliasCandidate]:
    """Search ClinicalTrials.gov v2 by intervention drug_name and lead sponsor.
    Harvest NCT IDs (nct_id kind) and interventions[].otherNames (code kind).
    """
    out: List[AliasCandidate] = []
    asset_id = asset["id"]
    drug = asset.get("drug_name") or asset.get("generic_name")
    if not drug:
        return out

    params: Dict[str, Any] = {
        "query.intr": drug,
        "pageSize": 100,
        "format": "json",
    }
    sponsor = asset.get("sponsor_name")
    if sponsor:
        params["query.lead"] = sponsor

    body = _ct_get("studies", params, session)
    stats.api_calls += 1
    time.sleep(INTER_REQUEST_SLEEP_S)
    if not body:
        return out

    studies = body.get("studies") or []
    seen: Set[Tuple[str, str]] = set()

    drug_norm = normalize(drug)

    for study in studies:
        protocol = study.get("protocolSection") or {}
        ident = protocol.get("identificationModule") or {}
        nct_id = ident.get("nctId")
        if nct_id:
            cand = make_candidate(asset_id, nct_id, "nct_id",
                                  "clinicaltrials_v2", source_ref=nct_id)
            if cand and (cand.alias_normalized, cand.alias_kind) not in seen:
                seen.add((cand.alias_normalized, cand.alias_kind))
                out.append(cand)

        # interventions[].otherNames is where code names like LY3502970 live.
        arms = protocol.get("armsInterventionsModule") or {}
        for intv in (arms.get("interventions") or []):
            # Gate: only harvest otherNames from interventions whose primary
            # name actually mentions the asset's drug — otherwise we'd ingest
            # other drugs' code names from multi-arm trials.
            iname = (intv.get("name") or "").strip()
            if iname and drug_norm not in normalize(iname):
                continue
            for other in (intv.get("otherNames") or []):
                cand = make_candidate(asset_id, other, "code",
                                      "clinicaltrials_v2",
                                      source_ref=nct_id)
                if cand and (cand.alias_normalized, cand.alias_kind) not in seen:
                    seen.add((cand.alias_normalized, cand.alias_kind))
                    out.append(cand)

    return out


# ---------------------------------------------------------------------------
# Source 3: documents.extensions mining
# ---------------------------------------------------------------------------

def aliases_from_extensions(client: SupabaseClient,
                            asset: Dict[str, Any]) -> List[AliasCandidate]:
    """Read documents already linked to this asset via asset_documents and
    mine their ``extensions`` jsonb for NCT IDs and intervention 'otherNames'
    that aren't yet in fda_asset_aliases. Pure SQL via PostgREST — no Modal,
    no LLM."""
    out: List[AliasCandidate] = []
    asset_id = asset["id"]

    # Pull recent linked documents' extensions. RPC would be cleaner but
    # PostgREST lets us filter + select directly.
    rows: List[Dict[str, Any]] = client._rest_with_retry(  # type: ignore[attr-defined]
        "GET",
        "asset_documents",
        params={
            "asset_id": f"eq.{asset_id}",
            "select": "document_id,documents(extensions)",
            "limit": "500",
        },
    ) or []

    seen: Set[Tuple[str, str]] = set()

    for row in rows:
        doc = row.get("documents") or {}
        ext = doc.get("extensions") or {}
        # NCT ID
        nct = ext.get("nct_id")
        if nct:
            cand = make_candidate(asset_id, nct, "nct_id",
                                  "extensions_mining",
                                  source_ref=row.get("document_id"))
            if cand and (cand.alias_normalized, cand.alias_kind) not in seen:
                seen.add((cand.alias_normalized, cand.alias_kind))
                out.append(cand)
        # interventions array of {name, otherNames}
        for intv in (ext.get("interventions") or []):
            for other in (intv.get("otherNames") or []):
                cand = make_candidate(asset_id, other, "code",
                                      "extensions_mining",
                                      source_ref=row.get("document_id"))
                if cand and (cand.alias_normalized, cand.alias_kind) not in seen:
                    seen.add((cand.alias_normalized, cand.alias_kind))
                    out.append(cand)

    return out


# ---------------------------------------------------------------------------
# Upsert + run summary
# ---------------------------------------------------------------------------

def upsert_candidates(client: SupabaseClient,
                      candidates: List[AliasCandidate]) -> int:
    """Idempotent batch insert. Returns count of rows actually inserted (server-side)."""
    if not candidates:
        return 0
    rows = [
        {
            "asset_id":         c.asset_id,
            "alias":            c.alias,
            "alias_normalized": c.alias_normalized,
            "alias_kind":       c.alias_kind,
            "source":           c.source,
            "source_ref":       c.source_ref,
            "active":           True,
        }
        for c in candidates
    ]
    # PostgREST upsert via Prefer: resolution=ignore-duplicates returns the
    # full result set including dedup-skipped rows — we count returned rows as
    # "attempted" and rely on the unique index to skip dupes.
    result = client._rest_with_retry(  # type: ignore[attr-defined]
        "POST",
        "fda_asset_aliases",
        json_body=rows,
        prefer="return=representation,resolution=ignore-duplicates",
    ) or []
    return len(result) if isinstance(result, list) else 0


def record_seed_run(client: SupabaseClient, stats: SeedStats,
                    started_at: datetime, *,
                    status: str, notes: Optional[str] = None) -> None:
    """Mirror of asset_linker._record_linker_run_summary but for the
    seed pass. token cost = 0 by construction."""
    row = {
        "pass":               "seed",
        "model":              "seed-script",
        "started_at":         started_at.isoformat(),
        "completed_at":       datetime.now(timezone.utc).isoformat(),
        "status":             status,
        "docs_seen":          stats.assets_processed,
        "prefilter_passed":   stats.candidates_proposed,
        "prefilter_skipped":  0,
        "api_calls":          stats.api_calls,
        "errors":             stats.errors,
        "links_inserted":     stats.rows_inserted,
        "links_dedup_skipped": 0,
        "input_tokens":       0,
        "output_tokens":      0,
        "cost_usd":           0.0,
        "notes":              notes,
    }
    try:
        client._rest_with_retry(  # type: ignore[attr-defined]
            "POST", "asset_linker_runs",
            json_body=row, prefer="return=minimal",
        )
    except Exception as exc:  # pragma: no cover  (best-effort summary write)
        logger.warning("asset_linker_runs seed summary write failed: %s", exc)


# ---------------------------------------------------------------------------
# Active-asset loader
# ---------------------------------------------------------------------------

def load_active_assets(client: SupabaseClient,
                       only_asset_id: Optional[str] = None,
                       max_assets: Optional[int] = None) -> List[Dict[str, Any]]:
    params: Dict[str, Any] = {
        "select": "id,ticker,drug_name,generic_name,sponsor_name,indication",
        "is_active": "eq.true",
    }
    if only_asset_id:
        params["id"] = f"eq.{only_asset_id}"
    rows = client._rest_with_retry("GET", "fda_assets", params=params) or []  # type: ignore[attr-defined]

    # Filter out the placeholder drug_name slots that v_asset_linker_skill_assets
    # excludes — no point seeding aliases for assets the prefilter ignores.
    NOISY = {"(auto-discovered)", "ex-99", "peptide", "concept", "nucleotide", "default"}
    filtered = [
        r for r in rows
        if r.get("drug_name") and normalize(r["drug_name"]) not in NOISY
    ]
    if max_assets:
        filtered = filtered[:max_assets]
    return filtered


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=("Populate fda_asset_aliases from openFDA labels, "
                     "ClinicalTrials.gov, and existing documents.extensions."),
    )
    p.add_argument("--asset-id", default=None,
                   help="Restrict to one fda_assets row (smoke test).")
    p.add_argument("--sources", default=",".join(ALL_SOURCES),
                   help=(f"Comma-separated subset of {ALL_SOURCES}. "
                         "Default: all sources."))
    p.add_argument("--dry-run", action="store_true",
                   help="Log candidates per source per asset; do not write.")
    p.add_argument("--max-assets", type=int, default=None,
                   help="Cap number of assets processed (testing).")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="DEBUG logging.")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    unknown = set(sources) - set(ALL_SOURCES)
    if unknown:
        logger.error("Unknown --sources values: %s. Valid: %s",
                     sorted(unknown), ALL_SOURCES)
        return 2

    client = SupabaseClient()
    session = requests.Session()
    session.headers["User-Agent"] = "conan-seed-fda-asset-aliases/1.0"

    started_at = datetime.now(timezone.utc)
    stats = SeedStats()

    assets = load_active_assets(client,
                                only_asset_id=args.asset_id,
                                max_assets=args.max_assets)
    logger.info("Loaded %d active assets (sources=%s, dry_run=%s)",
                len(assets), sources, args.dry_run)

    try:
        for asset in assets:
            stats.assets_processed += 1
            per_asset: List[AliasCandidate] = []

            try:
                if "openfda_label" in sources:
                    cands = aliases_from_openfda(asset, session, stats)
                    per_asset.extend(cands)
                    stats.by_source["openfda_label"] = (
                        stats.by_source.get("openfda_label", 0) + len(cands)
                    )

                if "clinicaltrials_v2" in sources:
                    cands = aliases_from_clinicaltrials(asset, session, stats)
                    per_asset.extend(cands)
                    stats.by_source["clinicaltrials_v2"] = (
                        stats.by_source.get("clinicaltrials_v2", 0) + len(cands)
                    )

                if "extensions_mining" in sources:
                    cands = aliases_from_extensions(client, asset)
                    per_asset.extend(cands)
                    stats.by_source["extensions_mining"] = (
                        stats.by_source.get("extensions_mining", 0) + len(cands)
                    )
            except Exception as exc:
                logger.exception("seed: error on asset %s (%s): %s",
                                 asset.get("id"), asset.get("ticker"), exc)
                stats.errors += 1
                continue

            stats.candidates_proposed += len(per_asset)
            logger.info("  asset %s (%s, %s): %d candidates",
                        asset.get("ticker"), asset.get("drug_name"),
                        asset.get("id"), len(per_asset))

            if args.dry_run:
                for c in per_asset:
                    logger.info("    [dry] %s/%s/%s = %r",
                                c.source, c.alias_kind, c.alias_normalized, c.alias)
            else:
                inserted = upsert_candidates(client, per_asset)
                stats.rows_inserted += inserted

        logger.info(
            "seed complete: assets=%d candidates=%d inserted=%d api_calls=%d errors=%d by_source=%s",
            stats.assets_processed, stats.candidates_proposed,
            stats.rows_inserted, stats.api_calls, stats.errors,
            stats.by_source,
        )

        if not args.dry_run:
            record_seed_run(
                client, stats, started_at,
                status="ok" if stats.errors == 0 else "partial",
                notes=f"sources={sources} asset_id={args.asset_id} max_assets={args.max_assets}",
            )
        return 0
    except Exception:
        logger.exception("seed: fatal error")
        if not args.dry_run:
            record_seed_run(client, stats, started_at,
                            status="error", notes="fatal exception; see logs")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
