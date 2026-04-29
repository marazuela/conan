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

from modal_workers.shared.caption_party import extract_corporate_party
from modal_workers.shared.scanner_base import MissingAuthError, Signal, ScannerResult
from modal_workers.shared.sec_issuer_lookup import IssuerIndex, IssuerMatch
from modal_workers.shared.supabase_client import EntityHints, ScannerConfig, SupabaseClient

NAME = "courtlistener_scanner"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "https://www.courtlistener.com/api/rest/v4"
# Solr-backed search endpoint. Replaces /dockets/ because CL's /dockets/
# `nature_of_suit` column is sparse (PACER/RECAP-sourced, usually empty) and the
# filter hangs >45s server-side. /search/?type=r is indexed and returns in ~1-3s.
SEARCH_URL = f"{BASE_URL}/search/"

# ---------------------------------------------------------------------------
# Per-NOS configuration (2026-04-24 selectivity rework).
#
# Problem context (2026-04-23 log review): the scanner was emitting ~19 signals
# per day, with 99% of resolved entities having a case caption as their name
# (e.g., "Sipin v. Tesla Inc."), 0 FIGI coverage, and band distribution 66%
# archive / 13% watchlist / 0 immediate. NOS 190 (Other Contract) and 830/835
# (Patent) together were 70% of emissions and 70%+ archive — low-value noise.
#
# Fix: per-NOS priority tiering + universe-match gate. NOS with
# `require_universe_match=True` only emit if the extracted corporate party
# resolves to a public issuer via sec_issuer_lookup. NOS with `priority=off`
# are disabled entirely (config flag, re-enable via scanners row config).
#
# The legacy `TARGET_NOS` set is preserved below as a union so tests and any
# external importers still see the full whitelist.
# ---------------------------------------------------------------------------

NOS_CONFIG: Dict[str, Dict[str, Any]] = {
    "850": {  # Securities/Commodities
        "priority": "high",
        "strength": 4,
        "require_universe_match": False,
        "signal_type": "federal_civil_securities_filed",
        "case_family": "securities",
    },
    "410": {  # Antitrust
        "priority": "high",
        "strength": 4,
        "require_universe_match": False,
        "signal_type": "federal_civil_antitrust_filed",
        "case_family": "antitrust",
    },
    "830": {  # Patent
        "priority": "low",
        "strength": 3,
        "require_universe_match": True,
        "signal_type": "federal_civil_patent_filed",
        "case_family": "patent_ip",
    },
    "835": {  # Patent — Abbreviated New Drug Application
        "priority": "low",
        "strength": 3,
        "require_universe_match": True,
        "signal_type": "federal_civil_patent_filed",
        "case_family": "patent_ip",
    },
    "190": {  # Other Contract — the flood source; default OFF
        "priority": "off",
        "strength": 3,
        "require_universe_match": True,
        "signal_type": "federal_civil_contract_filed",
        "case_family": "contract_mna",
    },
}

NOS_SECURITIES = {k for k, v in NOS_CONFIG.items() if v["case_family"] == "securities"}
NOS_CONTRACT_MA = {k for k, v in NOS_CONFIG.items() if v["case_family"] == "contract_mna"}
NOS_PATENT = {k for k, v in NOS_CONFIG.items() if v["case_family"] == "patent_ip"}
NOS_ANTITRUST = {k for k, v in NOS_CONFIG.items() if v["case_family"] == "antitrust"}
TARGET_NOS = NOS_SECURITIES | NOS_CONTRACT_MA | NOS_PATENT | NOS_ANTITRUST

LOOKBACK_DAYS = 7
PAGE_SIZE = 50
REQUEST_TIMEOUT = 15  # per-request seconds

TICKER_HINT_RE = re.compile(r'\(\s*"?([A-Z]{2,5})"?\s*\)')


def _resolve_nos_config(nos: str, cfg_overrides: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Merge static NOS_CONFIG with per-scan overrides from cfg.config.

    Config flags (scanners.config jsonb):
      courtlistener_nos_190_enabled: bool  # override off → high
      courtlistener_nos_overrides:   dict   # per-NOS partial overrides
    """
    base = NOS_CONFIG.get(nos)
    if base is None:
        return None
    merged = dict(base)
    # Backwards-compat flag: re-enable NOS 190 when caption parsing is proven.
    if nos == "190" and cfg_overrides.get("courtlistener_nos_190_enabled") is True:
        merged["priority"] = "low"
        merged["require_universe_match"] = True
    # Free-form per-NOS override knob.
    per_nos = cfg_overrides.get("courtlistener_nos_overrides") or {}
    if isinstance(per_nos, dict) and nos in per_nos and isinstance(per_nos[nos], dict):
        merged.update(per_nos[nos])
    return merged

# Signal-type -> thesis direction. "filed" variants all lean short (new
# litigation = overhang), procedural stages keep v1 mappings.
_DIRECTION_MAP: Dict[str, str] = {
    "federal_civil_filed": "short",
    "federal_civil_securities_filed": "short",
    "federal_civil_antitrust_filed": "short",
    "federal_civil_patent_filed": "short",
    "federal_civil_contract_filed": "short",
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
        # All "filed" variants (the 2026-04-24 NOS split) are complaint-stage.
        "federal_civil_filed": ("complaint_filed", ">12m"),
        "federal_civil_securities_filed": ("complaint_filed", ">12m"),
        "federal_civil_antitrust_filed": ("complaint_filed", ">12m"),
        "federal_civil_patent_filed": ("complaint_filed", ">12m"),
        "federal_civil_contract_filed": ("complaint_filed", ">12m"),
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

def _classify_procedural_override(case_name: str) -> Optional[str]:
    """Derive a procedural-stage signal_type override from case_name keywords.

    When the caption mentions "class certified", "settlement", "summary
    judgment", or "motion to dismiss...denied", the signal_type becomes the
    procedural stage instead of the NOS-derived one. This preserves v1
    behaviour where procedural moves were the highest-value events.
    """
    nm = (case_name or "").lower()
    if "class certif" in nm:
        return "class_certified"
    if "settlement" in nm:
        return "settlement"
    if "summary judgment" in nm:
        return "summary_judgment"
    if "motion to dismiss" in nm and "denied" in nm:
        return "mtd_denied"
    return None


def _docket_to_signal(
    d: Dict[str, Any],
    *,
    scan_date: datetime,
    issuer_index: Optional[IssuerIndex],
    cfg_overrides: Dict[str, Any],
) -> Optional[Signal]:
    """Build a Signal from a CourtListener docket result.

    Returns None when:
      - case_name missing
      - NOS is `priority=off` (disabled)
      - NOS requires universe match and issuer lookup failed
    """
    # /search/?type=r returns camelCase (caseName, dateFiled, suitNature). The
    # snake_case fallbacks preserve parity with the legacy /dockets/ response.
    case_name = d.get("caseName") or d.get("case_name") or d.get("case_name_short") or ""
    if not case_name:
        return None

    nos = str(d.get("_nos_queried") or d.get("suitNature") or d.get("nature_of_suit") or "")
    nos_cfg = _resolve_nos_config(nos, cfg_overrides)
    if nos_cfg is None:
        return None

    # Procedural override is computed early because it bypasses NOS=off AND
    # require_universe_match gates. Class certifications, settlements,
    # summary judgments, and denied MTDs are high-value regardless of NOS or
    # universe match — dropping them would lose the best signals the scanner
    # can produce.
    procedural = _classify_procedural_override(case_name)

    # NOS-off gate: applies only when there's no procedural override.
    if procedural is None and nos_cfg.get("priority") == "off":
        return None

    court = d.get("court_id") or d.get("court") or ""
    filing_date = d.get("dateFiled") or d.get("date_filed") or ""
    docket_id = d.get("docket_id") or d.get("id")

    # Extract corporate party + confidence (shared helper — same logic the
    # Chancery scanner uses).
    extracted_party, party_confidence = extract_corporate_party(case_name)

    # Universe resolution: try SEC tickers list first (fast, free), fall back
    # to the paren-ticker heuristic if it's there.
    issuer_match: Optional[IssuerMatch] = None
    if issuer_index is not None and extracted_party:
        issuer_match = issuer_index.resolve(extracted_party)

    ticker_hint = _extract_ticker_hint(case_name)
    # Universe-match gate — only applies to require_universe_match NOS rows
    # AND only if the procedural fast-path doesn't fire. Securities (850) and
    # antitrust (410) always emit so thesis_writer can triage; noise-heavy
    # contract/patent rows are gated.
    if (
        procedural is None
        and nos_cfg.get("require_universe_match")
        and issuer_match is None
        and not ticker_hint
    ):
        return None

    # Signal type: procedural override beats NOS default
    signal_type = procedural or nos_cfg["signal_type"]
    # Back-compat: the legacy `federal_civil_filed` alias is preserved so
    # downstream listeners that match on it still route correctly.
    direction = _DIRECTION_MAP.get(signal_type) or _DIRECTION_MAP.get(
        "federal_civil_filed", "neutral"
    )
    procedural_stage, timeline_bucket = _procedural_stage(signal_type)

    # Resolve FIGI best-effort. Two sources of ticker, in priority order:
    #   1. issuer_match.ticker — from SEC company_tickers.json lookup keyed on
    #      the extracted caption party. Authoritative for US issuers.
    #   2. ticker_hint — from the case_name paren regex. Hard-gated behind
    #      OpenFIGI verification (audit 2026-04-23): the regex `[A-Z]{2,5}`
    #      matches any 2–5 uppercase acronym ("Foo Corp (UNOPS) v. Bar LLC"
    #      emits UNOPS — the UN Office for Project Services, not a ticker).
    #      Only propagate to EntityHints when OpenFIGI confirms it resolves;
    #      raw_payload still carries ticker_hint + ticker_hint_source for trace.
    issuer_figi: Optional[str] = None
    resolved_paren_ticker: Optional[str] = None
    ticker_for_resolve = (issuer_match.ticker if issuer_match else None) or ticker_hint
    if ticker_for_resolve:
        try:
            from modal_workers.shared.openfigi_resolver import resolve_ticker
            res = resolve_ticker(ticker_for_resolve, exch_code="US")
            if res.resolved:
                issuer_figi = res.issuer_figi
                # Only consider the paren hint "verified" if OpenFIGI resolved
                # the *paren* ticker specifically (issuer_match path is already
                # SEC-verified and doesn't need this gate).
                if issuer_match is None and ticker_hint:
                    resolved_paren_ticker = ticker_hint
        except Exception:  # noqa: BLE001 — best-effort
            pass

    # Parse filing_date to UTC datetime.
    try:
        source_date = datetime.strptime(filing_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        source_date = scan_date

    docket_url = f"https://www.courtlistener.com/docket/{docket_id}/" if docket_id else None

    source_content_hash = (
        f"sha256:{hashlib.sha256(f'{case_name}|{filing_date}|{court}'.encode()).hexdigest()}"
    )
    signal_id = hashlib.sha256(
        f"{docket_id or '?'}:{filing_date or '?'}:{signal_type}".encode()
    ).hexdigest()[:32]

    # Convergence key — cross-run dedup for the same party+court. The reactor
    # edge function uses this to collapse repeat alerts.
    party_normalized = (extracted_party or case_name).lower().strip()
    party_normalized = re.sub(r"[^a-z0-9]+", "", party_normalized)[:64]
    convergence_key = f"fed|{court}|{party_normalized}" if party_normalized else None

    # Effective party_resolution_confidence for the rubric cap:
    #  - Caption confidence (0–1) → 1–5 scale
    #  - Bump if issuer_match resolved cleanly
    prc = max(1, round(1 + party_confidence * 4))  # 0→1, 1.0→5
    if issuer_match is not None:
        prc = 5

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
        "case_family": nos_cfg["case_family"],
        "procedural_stage": procedural_stage,
        "procedural_stage_confidence": "high",
        "resolution_timeline_bucket": timeline_bucket,
        "headline": f"{case_name} -- {signal_type.replace('_', ' ')}",
        "summary": f"NOS {nos} filed {filing_date} in {court}",
        # ---- new selectivity fields (read by rubric + fanout) ----
        "extracted_party": extracted_party,
        "party_resolution_confidence": prc,
        "universe_resolved": issuer_match is not None,
        "universe_match_kind": issuer_match.match_kind if issuer_match else None,
        "universe_ticker": issuer_match.ticker if issuer_match else None,
        "universe_cik": issuer_match.cik if issuer_match else None,
        "universe_title": issuer_match.title if issuer_match else None,
        "convergence_key": convergence_key,
        # For the rubric to consume via signal["raw_data"]:
        "nos_priority": nos_cfg.get("priority"),
    }

    # Entity hints now carry the EXTRACTED party (not the full caption) plus
    # the SEC-resolved ticker/CIK when available. This is the main fix for
    # the "99% captions-as-names" entity-table pollution.
    name_for_hint = (
        issuer_match.title if issuer_match
        else (extracted_party or case_name)
    )
    # Final ticker for entity hint: SEC-issuer-match wins (already authoritative);
    # else the paren hint, but only if OpenFIGI verified it (resolved_paren_ticker).
    # Unverified paren hints are intentionally NOT propagated — they pollute the
    # entity table with non-issuer acronyms (UNOPS et al.). raw_payload still
    # carries the raw ticker_hint for forensic trace.
    entity_hint_ticker = (
        issuer_match.ticker if issuer_match
        else resolved_paren_ticker
    )
    entity_hints = EntityHints(
        issuer_figi=issuer_figi,
        ticker=entity_hint_ticker,
        mic=None,  # US MIC not determinable from CourtListener payload alone
        cik=(issuer_match.cik if issuer_match else None),
        name=name_for_hint,
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
        strength_estimate=int(nos_cfg.get("strength", 3)),
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

    # Load SEC issuer index once per run — used by _docket_to_signal to resolve
    # extracted party names to public tickers/CIKs. Failure is non-fatal; the
    # scanner falls back to name-only entity_hints (still better than the
    # full-caption regression).
    sec_user_agent = os.environ.get("SEC_USER_AGENT") or "Conan Scanner"
    try:
        issuer_index = IssuerIndex.load(client, user_agent=sec_user_agent)
    except Exception:  # noqa: BLE001
        issuer_index = None

    scan_date = datetime.now(timezone.utc)
    since = (scan_date - timedelta(days=LOOKBACK_DAYS)).date().isoformat()
    cfg_overrides = cfg.config or {}

    budget = max(10, cfg.timeout_soft_s - 5)  # leave headroom for final ops
    scan_start = time.time()
    warnings: List[str] = []
    signals: List[Signal] = []
    seen: set[str] = set()
    fetched_dockets = 0
    universe_filtered = 0    # dockets dropped for failing universe match
    disabled_nos_skipped: List[str] = []

    # Only query NOS rows with priority != "off". Disabled rows are skipped
    # upstream so we don't even issue the HTTP call — saves API budget.
    active_nos = []
    for nos in sorted(TARGET_NOS):
        merged = _resolve_nos_config(nos, cfg_overrides)
        if merged and merged.get("priority") != "off":
            active_nos.append(nos)
        elif merged:
            disabled_nos_skipped.append(nos)

    # Disabled NOS codes are an intentional config state (set in scanners.config
    # by the operator). They surface in the metrics block so dashboards can show
    # which NOS are off — but they are not "warnings" that should force the run
    # to status=partial. Real warnings (fetch failures, wall-clock timeout,
    # auth_required) still trigger partial via the warnings.append calls below.

    for nos in active_nos:
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
            sig = _docket_to_signal(
                d,
                scan_date=scan_date,
                issuer_index=issuer_index,
                cfg_overrides=cfg_overrides,
            )
            if sig is None:
                # Could be disabled-NOS (already filtered) or universe miss.
                # Count only universe-filter drops for observability.
                case_name = d.get("caseName") or d.get("case_name") or ""
                if case_name:
                    universe_filtered += 1
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
        run_metrics={
            "courtlistener_fetched_dockets": fetched_dockets,
            "courtlistener_signals_emitted": len(signals),
            "courtlistener_universe_filtered": universe_filtered,
            "courtlistener_disabled_nos": disabled_nos_skipped,
            "courtlistener_issuer_index_loaded": issuer_index is not None,
        },
    )
