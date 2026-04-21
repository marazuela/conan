"""
CourtListener scanner -- Modal port of tools/courtlistener_scanner.py.

Preserved from v1 (byte-equivalent where relevant):
  - NOS targeting: securities=850, contract/M&A=190, patent=830/835, antitrust=410.
  - 7-day lookback window (LOOKBACK_DAYS).
  - Classification logic (_classify_signal_type): class_certified, settlement,
    summary_judgment, mtd_denied, federal_civil_filed -- identical precedence.
  - Thesis-direction mapping: federal_civil_filed / mtd_denied / class_certified
    => "short"; settlement / summary_judgment => "neutral".
  - Ticker-hint extraction via `(TICKER)` paren regex on case_name.
  - Per-run dedup via source_content_hash seen-set.
  - Auth contract: COURTLISTENER_TOKEN missing => MissingAuthError (v1 returned
    status=auth_required; here the wrapper converts the raise into the same).

Deviations from v1:
  - Fetches via `/search/?type=r` (Solr-backed) instead of `/dockets/`. v1's
    `/dockets/?nature_of_suit=...` hangs >45s server-side because the NOS column
    is sparse (PACER/RECAP-sourced, usually empty). Search endpoint returns in
    ~1-3s. Field names come back camelCase (caseName, dateFiled, suitNature);
    `_docket_to_signal` reads both new and legacy snake_case for safety.
  - No OUT_FILE / no __main__ block; signals returned via ScannerResult for
    run_scanner plumbing.
  - source_content_hash now carries the spec.md sha256:<64hex> prefix (v1 used
    a 16-char truncated hex string without prefix) -- required for reactor
    convergence keying.
  - HTTP fetch uses `requests` directly (v1 used tools/http_client.HttpClient
    which isn't available in the Modal image).
  - Best-effort OpenFIGI resolution on extracted ticker hint (v1 left figi=None);
    cache routed through Supabase Storage via openfigi_resolver.set_cache_backend.
  - Wall-clock budget guard on paginated NOS fetch; bails with status="partial"
    if cfg.timeout_soft_s is exhausted.
  - scoring_profile="litigation" is resolved from cfg.signal_type_profile_map
    by run_scanner rather than hardcoded in the signal body.

IO contract:
  scan(cfg: ScannerConfig) -> ScannerResult
    - raises MissingAuthError if COURTLISTENER_TOKEN env unset.
    - Uses cfg.timeout_soft_s as wall-clock budget for paginated NOS fetches.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests

from modal_workers.shared.scanner_base import MissingAuthError, Signal, ScannerResult
from modal_workers.shared.supabase_client import EntityHints, ScannerConfig, SupabaseClient

NAME = "courtlistener_scanner"

# ---------------------------------------------------------------------------
# Constants (verbatim from v1)
# ---------------------------------------------------------------------------

BASE_URL = "https://www.courtlistener.com/api/rest/v4"
# Solr-backed search endpoint. Replaces /dockets/ because CL's /dockets/
# `nature_of_suit` column is sparse (PACER/RECAP-sourced, usually empty) and the
# filter hangs >45s server-side. /search/?type=r is indexed and returns in ~1-3s.
SEARCH_URL = f"{BASE_URL}/search/"

NOS_SECURITIES = {"850"}
NOS_CONTRACT_MA = {"190"}
NOS_PATENT = {"830", "835"}
NOS_ANTITRUST = {"410"}
TARGET_NOS = NOS_SECURITIES | NOS_CONTRACT_MA | NOS_PATENT | NOS_ANTITRUST
LOOKBACK_DAYS = 7
PAGE_SIZE = 50
REQUEST_TIMEOUT = 15  # per-request seconds

TICKER_HINT_RE = re.compile(r'\(\s*"?([A-Z]{2,5})"?\s*\)')

# Signal-type -> thesis direction. Preserved byte-for-byte from v1's inline map.
_DIRECTION_MAP: Dict[str, str] = {
    "federal_civil_filed": "short",
    "mtd_denied": "short",
    "class_certified": "short",
    "settlement": "neutral",
    "summary_judgment": "neutral",
}


# ---------------------------------------------------------------------------
# Classification (verbatim from v1)
# ---------------------------------------------------------------------------

def _extract_ticker_hint(text: str) -> Optional[str]:
    if not text:
        return None
    m = TICKER_HINT_RE.search(text)
    return m.group(1) if m else None


def _classify_signal_type(nos: str, case_name: str) -> str:
    nm = (case_name or "").lower()
    if "class certif" in nm:
        return "class_certified"
    if "settlement" in nm:
        return "settlement"
    if "summary judgment" in nm:
        return "summary_judgment"
    if "motion to dismiss" in nm and "denied" in nm:
        return "mtd_denied"
    return "federal_civil_filed"


def _case_family_from_nos(nos: str) -> str:
    if nos in NOS_SECURITIES:
        return "securities"
    if nos in NOS_ANTITRUST:
        return "antitrust"
    if nos in NOS_PATENT:
        return "patent_ip"
    if nos in NOS_CONTRACT_MA:
        return "contract_mna"
    return "general_civil"


def _procedural_stage(signal_type: str) -> tuple[str, str]:
    mapping = {
        "class_certified": ("class_certification", "3-6m"),
        "settlement": ("settlement", "1-3m"),
        "summary_judgment": ("summary_judgment", "1-3m"),
        "mtd_denied": ("post_mtd", "3-6m"),
        "federal_civil_filed": ("complaint_filed", ">12m"),
    }
    return mapping.get(signal_type, ("unknown", "unknown"))


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def _fetch_nos(
    nos: str,
    since: str,
    token: str,
) -> List[Dict[str, Any]]:
    headers = {"Authorization": f"Token {token}"}
    # /search/?type=r is RECAP dockets. `suitNature` is the Solr field name;
    # values are stored as "<code> <label>" (e.g. "850 Securities/Commodities"),
    # so the unquoted token match on the code works. `filed_after` is inclusive.
    params = {
        "type": "r",
        "q": f"suitNature:{nos}",
        "filed_after": since,
        "order_by": "dateFiled desc",
        "page_size": PAGE_SIZE,
    }
    try:
        resp = requests.get(
            SEARCH_URL,
            params=params,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException:
        return []
    except ValueError:
        return []

    results: List[Dict[str, Any]] = []
    for d in data.get("results") or []:
        d["_nos_queried"] = nos
        results.append(d)
    return results


# ---------------------------------------------------------------------------
# Signal builder
# ---------------------------------------------------------------------------

def _docket_to_signal(d: Dict[str, Any], scan_date: datetime) -> Optional[Signal]:
    # /search/?type=r returns camelCase (caseName, dateFiled, suitNature). The
    # snake_case fallbacks preserve parity with the legacy /dockets/ response
    # shape in case a caller ever feeds one of those dicts in.
    case_name = d.get("caseName") or d.get("case_name") or d.get("case_name_short") or ""
    if not case_name:
        return None

    nos = d.get("_nos_queried") or d.get("suitNature") or d.get("nature_of_suit") or ""
    # Prefer court_id (short stable code, e.g. "nysd"). Search endpoint returns
    # `court` as a display label ("District Court, S.D. New York"); the old
    # /dockets/ endpoint returned it as a REST URL. court_id is identical on both.
    court = d.get("court_id") or d.get("court") or ""
    filing_date = d.get("dateFiled") or d.get("date_filed") or ""
    docket_id = d.get("docket_id") or d.get("id")
    ticker_hint = _extract_ticker_hint(case_name)
    signal_type = _classify_signal_type(str(nos), case_name)
    direction = _DIRECTION_MAP.get(signal_type, "neutral")
    procedural_stage, timeline_bucket = _procedural_stage(signal_type)

    # Resolve FIGI best-effort (v1 left this None; v2 populates when possible so
    # the reactor / entity-resolver cascade has a head start).
    issuer_figi: Optional[str] = None
    if ticker_hint:
        try:
            from modal_workers.shared.openfigi_resolver import resolve_ticker
            res = resolve_ticker(ticker_hint, exch_code="US")
            if res.resolved:
                issuer_figi = res.issuer_figi
        except Exception:
            pass

    # Parse filing_date to UTC datetime (v1 used "YYYY-MM-DDT00:00:00Z" string).
    try:
        source_date = datetime.strptime(filing_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        source_date = scan_date

    docket_url = f"https://www.courtlistener.com/docket/{docket_id}/" if docket_id else None

    # source_content_hash keys off case_name|filing_date|court (v1 parity) but
    # now uses the spec.md sha256:<64hex> prefix.
    source_content_hash = (
        f"sha256:{hashlib.sha256(f'{case_name}|{filing_date}|{court}'.encode()).hexdigest()}"
    )
    # signal_id keys off docket_id|filing_date|signal_type (v1 parity, 32-char).
    signal_id = hashlib.sha256(
        f"{docket_id or '?'}:{filing_date or '?'}:{signal_type}".encode()
    ).hexdigest()[:32]

    raw_payload: Dict[str, Any] = {
        "scanner_source": NAME,
        "upstream_scanner": NAME,
        "signal_type": signal_type,
        "case_name": case_name,
        "court": str(court),
        "filing_date": filing_date,
        "docket_id": docket_id,
        "nos": str(nos),
        "nature_of_suit": str(nos),
        "court_id": str(court),
        "docket_url": docket_url,
        "ticker_hint": ticker_hint,
        "ticker_hint_source": "case_name_paren" if ticker_hint else None,
        "ticker_hint_present": bool(ticker_hint),
        "case_family": _case_family_from_nos(str(nos)),
        "procedural_stage": procedural_stage,
        "procedural_stage_confidence": "high",
        "resolution_timeline_bucket": timeline_bucket,
        "headline": f"{case_name} -- {signal_type.replace('_', ' ')}",
        "summary": f"NOS {nos} filed {filing_date} in {court}",
    }

    entity_hints = EntityHints(
        issuer_figi=issuer_figi,
        ticker=ticker_hint,
        mic=None,  # US MIC not determinable from CourtListener payload alone
        name=case_name,
        country="US",
    )

    return Signal(
        signal_id=signal_id,
        source_content_hash=source_content_hash,
        source_date=source_date,
        scan_date=scan_date,
        signal_type=signal_type,
        raw_payload=raw_payload,
        source_url=docket_url,
        issuer_figi=issuer_figi,
        entity_hints=entity_hints,
        thesis_direction=direction,
        strength_estimate=3,
    )


# ---------------------------------------------------------------------------
# scan entrypoint
# ---------------------------------------------------------------------------

def scan(cfg: ScannerConfig) -> ScannerResult:
    token = os.environ.get("COURTLISTENER_TOKEN")
    if not token:
        raise MissingAuthError(
            "COURTLISTENER_TOKEN env var missing -- CourtListener API requires a "
            "bearer token. Get a free token at "
            "https://www.courtlistener.com/help/api/rest/authentication/ and "
            "set via Modal secret `scanner-secrets`."
        )

    client = SupabaseClient()

    # Route OpenFIGI cache reads/writes through Supabase Storage (matches edgar).
    from modal_workers.shared.openfigi_resolver import set_cache_backend
    set_cache_backend(*client.openfigi_cache_backend())

    scan_date = datetime.now(timezone.utc)
    since = (scan_date - timedelta(days=LOOKBACK_DAYS)).date().isoformat()

    budget = max(10, cfg.timeout_soft_s - 5)  # leave headroom for final ops
    scan_start = time.time()
    warnings: List[str] = []
    signals: List[Signal] = []
    seen: set[str] = set()
    fetched_dockets = 0

    for nos in sorted(TARGET_NOS):
        if time.time() - scan_start > budget:
            warnings.append(
                f"wall-clock budget ({budget}s) exceeded before NOS {nos}"
            )
            break
        try:
            dockets = _fetch_nos(nos, since, token)
        except Exception as e:  # noqa: BLE001
            warnings.append(f"fetch NOS {nos} failed: {type(e).__name__}: {e}")
            continue

        fetched_dockets += len(dockets)
        for d in dockets:
            sig = _docket_to_signal(d, scan_date)
            if sig is None:
                continue
            if sig.source_content_hash in seen:
                continue
            seen.add(sig.source_content_hash)
            signals.append(sig)

    status = "partial" if warnings else "ok"

    return ScannerResult(
        scanner=NAME,
        status=status,
        signals=signals,
        warnings=warnings,
        fetched_records=fetched_dockets,
    )
