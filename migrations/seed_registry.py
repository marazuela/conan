"""Seed the v2 Supabase registry tables from v1 configs + WEIGHTS.

Per spec.md §9.1. Populates (in order):
  1. sources           (one per unique scanner source kind)
  2. scanners          (17 rows from config/scanner_registry.json; market_cap_floor_usd_mm=215)
  3. rubrics           (6 rows from run_post_scan.WEIGHTS at rubric_version=1)
  4. pe_filer_allowlist (39 rows from config/pe_filer_allowlist.json)
  5. phase3_base_rates (39 rows from config/phase3_approval_base_rates.json)
  6. candidate_rationales (N rows from candidates/_curated_rationales.json, schema v2)

Idempotent: every INSERT uses PostgREST's Prefer: resolution=merge-duplicates header (PostgreSQL
ON CONFLICT semantics), keyed on each table's natural unique constraint. Re-running the script is
safe and updates rows whose source config has changed.

Dry-run:
    python3 migrations/seed_registry.py --dry-run

Live (writes to Supabase):
    SUPABASE_URL=https://xvwvwbnxdsjpnealarkh.supabase.co \
    SUPABASE_SERVICE_ROLE_KEY=sbp_... \
    python3 migrations/seed_registry.py

Dependencies: stdlib only (uses `requests` via modal_workers if available, else urllib).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

import requests


# --------------------------------------------------------------------
# Paths + source config locations
# --------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
V1_ROOT = REPO_ROOT / "unified_system" / "unified_system"
CONFIG_DIR = V1_ROOT / "config"
CANDIDATES_DIR = V1_ROOT / "candidates"

SCANNER_REGISTRY = CONFIG_DIR / "scanner_registry.json"
PE_FILER_ALLOWLIST = CONFIG_DIR / "pe_filer_allowlist.json"
PHASE3_BASE_RATES = CONFIG_DIR / "phase3_approval_base_rates.json"
CURATED_RATIONALES = CANDIDATES_DIR / "_curated_rationales.json"

# Import WEIGHTS from the ported module — single source of truth.
sys.path.insert(0, str(REPO_ROOT))
from modal_workers.shared.rubric_engine import WEIGHTS  # noqa: E402


# --------------------------------------------------------------------
# Source kind derivation — maps each scanner to its source kind enum value.
# See spec.md Appendix A: sources.kind CHECK vocabulary.
# --------------------------------------------------------------------

SCANNER_TO_SOURCE_KIND: Dict[str, str] = {
    "edgar_filing_monitor": "edgar",
    "takeover_candidate_scanner": "edgar",   # PE 13D/13G filings on EDGAR
    "sec_enforcement_scanner": "sec_enforcement",
    "congressional_trading": "edgar",        # STOCK Act periodic transaction reports; EDGAR-equivalent.
    "esma_short_scanner": "esma",
    "fda_pdufa_pipeline": "fda",
    "pre_phase3_readout_scanner": "clinicaltrials",
    "lse_rns_scanner": "lse",
    "tdnet_scanner": "tdnet",
    "asx_scanner": "asx",
    "sedar_plus_scanner": "sedar",
    "hkex_scanner": "hkex",
    "kind_scanner": "kind",
    "bse_nse_scanner": "bse_nse",
    "cvm_scanner": "cvm",
    "bmv_scanner": "bmv",
    "courtlistener_scanner": "courtlistener",
    "insider_form4_scanner": "edgar",          # SEC Section 16 Form 4 via EDGAR
    "delaware_chancery_scanner": "delaware_chancery",
}

SOURCE_NAME_TEMPLATE = {
    "edgar": "SEC EDGAR",
    "sec_enforcement": "SEC Enforcement",
    "esma": "ESMA short-position register (FCA/AMF/AFM/BaFin/CNMV/CONSOB)",
    "fda": "FDA openFDA + ClinicalTrials.gov",
    "clinicaltrials": "ClinicalTrials.gov (Phase 3 pre-readout)",
    "lse": "London Stock Exchange RNS",
    "tdnet": "Tokyo TDnet",
    "asx": "Australian Securities Exchange",
    "sedar": "Canadian SEDAR+",
    "hkex": "Hong Kong HKEx",
    "kind": "Korean KIND (OpenDART)",
    "bse_nse": "India BSE/NSE",
    "cvm": "Brazil CVM",
    "bmv": "Mexico BMV",
    "courtlistener": "CourtListener (US federal dockets)",
    "delaware_chancery": "Delaware Court of Chancery (opinions + CourtConnect dockets)",
}

# Canonical market-cap floor (spec §12 locked decision).
MARKET_CAP_FLOOR_USD_MM = 215


# --------------------------------------------------------------------
# PostgREST client
# --------------------------------------------------------------------

class Seeder:
    def __init__(self, url: str, service_key: str, dry_run: bool = False):
        self.url = url.rstrip("/")
        self.headers = {
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Content-Type": "application/json",
        }
        self.dry_run = dry_run
        self.counts: Dict[str, int] = {}

    def upsert(self, table: str, rows: List[Dict[str, Any]], on_conflict: str) -> int:
        """INSERT with ON CONFLICT DO UPDATE via Prefer: resolution=merge-duplicates."""
        if not rows:
            return 0
        if self.dry_run:
            print(f"[dry-run] would upsert {len(rows)} rows into {table} (on_conflict={on_conflict})")
            for r in rows[:2]:
                print(f"    sample: {json.dumps(r)[:140]}")
            return len(rows)

        r = requests.post(
            f"{self.url}/rest/v1/{table}?on_conflict={on_conflict}",
            headers={**self.headers, "Prefer": "resolution=merge-duplicates,return=minimal"},
            data=json.dumps(rows),
            timeout=30,
        )
        if r.status_code not in (200, 201, 204):
            raise RuntimeError(f"upsert into {table} failed ({r.status_code}): {r.text[:500]}")
        return len(rows)

    def fetch_id(self, table: str, where_col: str, where_val: str) -> Optional[str]:
        r = requests.get(
            f"{self.url}/rest/v1/{table}",
            headers={**self.headers, "Accept": "application/json"},
            params={"select": "id", f"{where_col}": f"eq.{where_val}", "limit": "1"},
            timeout=15,
        )
        if r.status_code != 200:
            raise RuntimeError(f"select from {table} failed ({r.status_code}): {r.text[:500]}")
        rows = r.json()
        return rows[0]["id"] if rows else None


# --------------------------------------------------------------------
# Seed builders — each returns the row list for its table.
# --------------------------------------------------------------------

def build_sources() -> List[Dict[str, Any]]:
    """One row per unique source kind across the 17 scanners."""
    kinds_seen = set(SCANNER_TO_SOURCE_KIND.values())
    rows = []
    for kind in sorted(kinds_seen):
        rows.append({
            "name": SOURCE_NAME_TEMPLATE.get(kind, kind),
            "kind": kind,
            "base_url": None,
            "notes": f"Seeded by migrations/seed_registry.py from scanner_registry.json.",
        })
    return rows


_SCANNER_TABLE_KEYS = {
    "name",
    "tool_path",
    "status",
    "geography",
    "cadence",
    "default_scoring_profile",
    "signal_type_profile_map",
    "timeout_soft_s",
    "timeout_hard_s",
    "last_run_utc",
    "last_run_status",
    "last_run_signals",
}


def _build_endpoints(scanner: Dict[str, Any]) -> Dict[str, Any]:
    """Preserve every endpoint_* key rather than only primary/secondary.

    The registry has already grown endpoint_fallback on FDA. Keeping this generic
    avoids future seed drift when scanners add tertiary or probe URLs.
    """
    return {
        key.removeprefix("endpoint_"): value
        for key, value in scanner.items()
        if key.startswith("endpoint_") and value is not None
    }


def build_scanners(sources_by_kind: Dict[str, str]) -> List[Dict[str, Any]]:
    """17 rows from config/scanner_registry.json. Maps:
      - endpoint_* → endpoints JSONB
      - absorbs tool_path, signal_type_profile_map, cadence, timeouts as-is
      - sets config.market_cap_floor_usd_mm = 215 per spec §12
      - preserves scanner-specific keys in config (notes, strategy_spec,
        requires_auth, probe_skip_reason, filter_excluded_filers, ranking caps,
        etc.) so re-seeding doesn't silently drop operational behavior
    """
    data = json.loads(SCANNER_REGISTRY.read_text())
    scanners = data["scanners"]
    rows = []
    for s in scanners:
        name = s["name"]
        kind = SCANNER_TO_SOURCE_KIND.get(name)
        if kind is None:
            raise RuntimeError(f"scanner {name!r} has no source-kind mapping; update SCANNER_TO_SOURCE_KIND")
        endpoints = _build_endpoints(s)
        config: Dict[str, Any] = {"market_cap_floor_usd_mm": MARKET_CAP_FLOOR_USD_MM}
        for key, value in s.items():
            if key in _SCANNER_TABLE_KEYS or key.startswith("endpoint_"):
                continue
            if value is not None:
                config[key] = s[key]
        rows.append({
            "name": name,
            "tool_path": s.get("tool_path"),
            "status": s.get("status", "operational"),
            "geography": s.get("geography"),
            "cadence": s.get("cadence"),
            "default_scoring_profile": s.get("default_scoring_profile"),
            "signal_type_profile_map": s.get("signal_type_profile_map") or {},
            "endpoints": endpoints,
            "timeout_soft_s": s.get("timeout_soft_s", 60),
            "timeout_hard_s": s.get("timeout_hard_s", 120),
            "config": config,
            "last_run_utc": s.get("last_run_utc"),
            "last_run_status": s.get("last_run_status"),
            "last_run_signals": s.get("last_run_signals"),
        })
    return rows


def build_rubrics() -> List[Dict[str, Any]]:
    """6 rows, one per scoring profile, at rubric_version=1. WEIGHTS is authoritative."""
    rows = []
    for profile, weights in WEIGHTS.items():
        rows.append({
            "profile": profile,
            "rubric_version": 1,
            "dimension_weights": weights,
            "notes": f"Seeded from modal_workers/shared/rubric_engine.py::WEIGHTS "
                     f"(byte-identical port of v1 run_post_scan.py).",
        })
    return rows


def build_pe_filers() -> List[Dict[str, Any]]:
    """39 rows from pe_filer_allowlist.json. All v1 types are PE-subtypes (pe_lbo,
    pe_software, etc.); the schema's filer_type CHECK is {'pe','activist_crossover'}, so
    we map all as 'pe' and preserve the nuanced v1 subtype in notes for traceability.
    """
    data = json.loads(PE_FILER_ALLOWLIST.read_text())
    filers = data["filers"]
    rows = []
    for name_norm, meta in filers.items():
        subtype = meta.get("type", "unknown")
        rows.append({
            "filer_name": name_norm,
            "cik": meta.get("cik"),
            "filer_type": "pe",
            "notes": f"v1_subtype={subtype}",
        })
    return rows


def build_phase3_rates() -> List[Dict[str, Any]]:
    """39 rows from phase3_approval_base_rates.json — indications with base rates.

    File shape (v1): indications are nested under the top-level `indications` key,
    alongside metadata blocks (`_schema_version`, `_provenance`, `_trial_design_adjustments`).
    The `_trial_design_adjustments` block is GLOBAL (9 entries — single_primary_endpoint,
    etc.), not per-indication; v2 stores it as a flat per-indication JSONB, so we leave
    each row's adjustments empty. If pre_phase3_readout_scanner needs the global block,
    it reads from scanner_registry.json.config or via a future table (out of seed scope).
    """
    data = json.loads(PHASE3_BASE_RATES.read_text())
    indications = data.get("indications") or {}
    rows = []
    for indication, meta in indications.items():
        if not isinstance(meta, dict):
            continue
        rate = meta.get("phase3_to_approval")
        if rate is None:
            continue
        rows.append({
            "indication": indication,
            "phase3_to_approval": float(rate),
            "trial_design_adjustments": meta.get("trial_design_adjustments") or {},
            "notes": meta.get("notes"),
        })
    return rows


def build_candidate_rationales() -> List[Dict[str, Any]]:
    """Hand-curated per-ticker rationale cards, schema v2.2.

    Top-level `_archived` sub-block (if present on a ticker) flattens to
    archived=true + archived_meta JSONB per spec §2 deviation.
    """
    data = json.loads(CURATED_RATIONALES.read_text())
    rows = []
    for ticker, payload in data.items():
        if ticker.startswith("_"):  # skip _meta
            continue
        if not isinstance(payload, dict):
            continue
        archived_meta = payload.get("_archived")
        rows.append({
            "ticker": ticker,
            "one_liner": payload.get("one_liner") or "",
            "hypothesis": payload.get("hypothesis") or "",
            "thesis": payload.get("thesis") or "",
            "expected_outcome": payload.get("expected_outcome") or "",
            "price_targets": payload.get("price_targets") or {},
            "time_sensitivity": payload.get("time_sensitivity") or "",
            "kill_watch": payload.get("kill_watch") or "",
            "catalyst_date_iso": payload.get("catalyst_date_iso"),
            "archived": bool(archived_meta),
            "archived_meta": archived_meta if archived_meta else None,
        })
    return rows


# --------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="show row counts and samples; no writes")
    args = ap.parse_args()

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not args.dry_run:
        if not url or not key:
            print("ERROR: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set for a live run.", file=sys.stderr)
            return 2
    else:
        url = url or "https://dry-run.example.com"
        key = key or "dry-run-key"

    s = Seeder(url, key, dry_run=args.dry_run)

    # 1. sources first — scanners FK references them? No, scanners.endpoints is JSONB,
    #    not a FK. But filings.source_id IS a FK, so sources must exist before the first
    #    scanner run. Seed here defensively.
    sources = build_sources()
    n = s.upsert("sources", sources, on_conflict="name")
    print(f"  sources:             {n}")
    s.counts["sources"] = n

    # 2. scanners
    src_ids: Dict[str, str] = {}  # not needed since scanners.endpoints is JSONB, but useful for future FK
    scanners = build_scanners(src_ids)
    n = s.upsert("scanners", scanners, on_conflict="name")
    print(f"  scanners:            {n}  (market_cap_floor_usd_mm=215)")
    s.counts["scanners"] = n

    # 3. rubrics
    rubrics = build_rubrics()
    n = s.upsert("rubrics", rubrics, on_conflict="profile,rubric_version")
    print(f"  rubrics:             {n}  (6 profiles at version 1)")
    s.counts["rubrics"] = n

    # 4. pe_filer_allowlist
    pe = build_pe_filers()
    n = s.upsert("pe_filer_allowlist", pe, on_conflict="filer_name")
    print(f"  pe_filer_allowlist:  {n}")
    s.counts["pe_filer_allowlist"] = n

    # 5. phase3_base_rates
    p3 = build_phase3_rates()
    n = s.upsert("phase3_base_rates", p3, on_conflict="indication")
    print(f"  phase3_base_rates:   {n}")
    s.counts["phase3_base_rates"] = n

    # 6. candidate_rationales
    rats = build_candidate_rationales()
    n = s.upsert("candidate_rationales", rats, on_conflict="ticker")
    print(f"  candidate_rationales:{n}")
    s.counts["candidate_rationales"] = n

    print("")
    print(f"seed complete ({'DRY RUN' if args.dry_run else 'LIVE'}): {sum(s.counts.values())} rows across 6 tables")
    return 0


if __name__ == "__main__":
    sys.exit(main())
