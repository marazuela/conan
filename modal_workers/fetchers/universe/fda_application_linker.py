"""Best-effort linker: resolve FDA application numbers for watched assets.

Most ``fda_assets`` sourced from the PDUFA watchlist carry an empty
``application_number`` (the watchlist source doesn't include it), which starves
the CRL rubric — it routes on the NDA/BLA application number and joins the
submission / inspection / warning features by it. This backfill queries openFDA
drugsfda by the asset's drug name and writes ``application_number`` **only on a
single high-confidence match** (exact normalized brand/generic-name match;
sponsor-token overlap used to disambiguate when several applications share the
name). It is conservative by construction:

* dry-run by default — never writes without ``dry_run=False``;
* never writes an *uncertain* number (0 or >1 ambiguous matches -> skip);
* dev-coded pre-approval drugs (e.g. ``AXS-05``) won't match an openFDA brand
  and are intentionally left empty.

Run (one-shot, from repo root, after the feature migrations are applied):

    python -m modal_workers.fetchers.universe.fda_application_linker --dry-run
    python -m modal_workers.fetchers.universe.fda_application_linker --commit --limit 500
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("fda_application_linker")

GetFn = Callable[[str, Dict[str, Any]], Optional[Dict[str, Any]]]

_SPONSOR_STOPWORDS = {
    "inc", "llc", "ltd", "corp", "co", "company", "pharmaceuticals", "pharma",
    "therapeutics", "plc", "ag", "sa", "nv", "the", "holdings", "group",
    "limited", "incorporated", "biosciences", "bio", "sciences", "labs",
    "laboratories", "international", "usa", "us", "gmbh",
}


def _default_get(path: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    # Lazy import keeps openFDA/requests out of import-time and lets tests inject.
    from modal_workers.ingestion.openfda_ingest import _openfda_get

    return _openfda_get(path, params)


def _norm_name(value: object) -> str:
    s = re.sub(r"[^a-z0-9 ]+", " ", str(value or "").lower())
    return re.sub(r"\s+", " ", s).strip()


def _sponsor_tokens(value: object) -> set:
    return {t for t in _norm_name(value).split() if t and t not in _SPONSOR_STOPWORDS}


def _appl_type(application_number: str) -> Optional[str]:
    s = (application_number or "").strip().upper()
    if s.startswith("BLA"):
        return "BLA"
    if s.startswith("NDA"):
        return "NDA"
    return None


def _candidate_names(app: Dict[str, Any]) -> set:
    names = set()
    of = app.get("openfda") or {}
    for key in ("brand_name", "generic_name"):
        for v in of.get(key) or []:
            n = _norm_name(v)
            if n:
                names.add(n)
    for p in app.get("products") or []:
        n = _norm_name(p.get("brand_name"))
        if n:
            names.add(n)
    return names


def resolve_application_number(
    asset: Dict[str, Any], *, get: Optional[GetFn] = None, limit: int = 25
) -> Optional[Dict[str, Any]]:
    """Return {application_number, application_type, match_method} for a single
    high-confidence drugsfda match, else None. Never guesses."""
    get = get or _default_get
    drug = asset.get("drug_name")
    if not drug:
        return None
    target = _norm_name(drug)
    if not target:
        return None
    sponsor = asset.get("sponsor_name") or (asset.get("extensions") or {}).get("company_name")

    # Query each name field separately to avoid openFDA OR-syntax ambiguity.
    seen_apps: Dict[str, Dict[str, Any]] = {}
    for field in ("openfda.brand_name", "openfda.generic_name", "products.brand_name"):
        data = get("drug/drugsfda.json", {"search": f'{field}:"{drug}"', "limit": limit})
        for app in (data or {}).get("results") or []:
            appno = str(app.get("application_number") or "").strip()
            if appno:
                seen_apps[appno] = app

    # Keep only applications whose product names actually match the asset drug.
    matches: Dict[str, set] = {}
    for appno, app in seen_apps.items():
        if target in _candidate_names(app):
            matches[appno] = _sponsor_tokens(app.get("sponsor_name"))
    if not matches:
        return None

    if len(matches) == 1:
        chosen, method = next(iter(matches)), "name_exact_single"
    else:
        stoks = _sponsor_tokens(sponsor)
        narrowed = [an for an, sp in matches.items() if stoks and (stoks & sp)]
        if len(narrowed) == 1:
            chosen, method = narrowed[0], "name_plus_sponsor"
        else:
            return None  # ambiguous — refuse to guess

    return {
        "application_number": chosen,
        "application_type": _appl_type(chosen),
        "match_method": method,
    }


def link_application_numbers(
    client: Any, *, dry_run: bool = True, limit: int = 500, get: Optional[GetFn] = None
) -> Dict[str, Any]:
    """Resolve + (optionally) write application_number for assets missing one."""
    rows = client._rest(
        "GET",
        "fda_assets",
        params={"select": "id,ticker,drug_name,sponsor_name,application_number,extensions",
                "limit": str(limit)},
    ) or []
    targets = [r for r in rows if not str(r.get("application_number") or "").strip() and r.get("drug_name")]

    summary: Dict[str, Any] = {
        "scanned": len(targets), "matched": 0, "written": 0,
        "skipped_no_match": 0, "dry_run": dry_run, "details": [],
    }
    for a in targets:
        res = resolve_application_number(a, get=get)
        if not res:
            summary["skipped_no_match"] += 1
            continue
        summary["matched"] += 1
        summary["details"].append(
            {"id": a["id"], "ticker": a.get("ticker"), "drug_name": a.get("drug_name"), **res}
        )
        if not dry_run:
            client._rest_with_retry(
                "PATCH",
                "fda_assets",
                params={"id": f"eq.{a['id']}", "select": "id"},
                json_body={"application_number": res["application_number"],
                           "application_type": res["application_type"]},
                prefer="return=representation",
            )
            summary["written"] += 1
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", default=True, help="Resolve + report, no writes (default).")
    group.add_argument("--commit", action="store_true", help="Write resolved application_numbers to fda_assets.")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level, format="%(levelname)s %(name)s %(message)s")

    from modal_workers.shared.supabase_client import SupabaseClient

    client = SupabaseClient(
        url=os.environ.get("SUPABASE_URL"),
        service_key=os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_KEY"),
    )
    summary = link_application_numbers(client, dry_run=not args.commit, limit=args.limit)
    summary["details"] = summary["details"][:50]  # cap log noise
    logger.info("application-number link summary:\n%s", json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
