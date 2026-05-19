"""
EDGAR filing monitor — flagship Modal implementation.

Core goals:
  - Budgeted full keyword coverage by default, with optional rotation fallback.
  - Legacy-grade quality controls: issuer/SPAC filtering + market-cap triage.
  - Honest runtime telemetry: retries, budget exhaustion, filter counts, degraded
    reasons, and explicit after-insert persistence for dedup / rotation state.
  - Modal is the canonical EDGAR implementation; the retired unified_system tool
    is historical context only, not a competing runtime path.

IO contract:
  scan(cfg: ScannerConfig) -> ScannerResult
    - raises MissingAuthError if SEC_USER_AGENT env unset.
    - Uses cfg.timeout_soft_s as a wall-clock budget.
    - Returns structured `run_metrics` for scanner_runs / health surfaces.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from modal_workers.shared.scanner_base import MissingAuthError, Signal, ScannerResult
from modal_workers.shared.supabase_client import EntityHints, ScannerConfig, SupabaseClient
from modal_workers.scanners.edgar_issuer_filter_defaults import DEFAULT_EDGAR_ISSUER_FILTER

# ---------------------------------------------------------------------------
# Constants (verbatim from v1)
# ---------------------------------------------------------------------------

EFTS_URL = "https://efts.sec.gov/LATEST/search-index"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_RATE_LIMIT = 9              # req/sec (SEC ceiling is 10; 10% safety margin)
REQUEST_TIMEOUT = 10            # per-request seconds
DEDUP_WINDOW_DAYS = 45          # signal novelty window
ROTATION_ORDER = ["activist", "mna", "distress", "governance", "fda_outcome"]
DEFAULT_COVERAGE_MODE = "full"

MIN_QUERY_BUDGET_S = 2.0
FILING_PHASE_RESERVE_S = 8.0
MAX_EFTS_RETRIES = 2
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
MAX_EFTS_FAILURE_DETAILS = 10

MARKET_CAP_CACHE_TTL_S = 24 * 3600
DEFAULT_MARKET_CAP_FLOOR_USD_MM = 215.0
MARKET_CAP_CACHE_PREFIX = "market-snapshots"
ISSUER_FILTER_FILE = Path(__file__).with_name("edgar_issuer_filter.json")
MAX_ISSUER_FILTER_SAMPLES = 10

# Persistent cross-run caches (Supabase Storage, scanner-caches/edgar/*).
# Prior to 2026-04-22 these lived only in process memory, so each 3h run
# re-issued ~300–400 rate-limited data.sec.gov/submissions/CIK{cik}.json
# lookups for issuers we had just resolved hours earlier — burning ~50s of
# the 85s budget and forcing `partial` runs. Now the resolved map is
# persisted between runs with a 7-day per-entry TTL (tickers/exchanges
# rarely change) and the merger-sibling suppression cache is persisted
# with a 30-day prune horizon (answers are stable; older file_dates fall
# outside the ±7d query window anyway).
COMPANY_CACHE_FILE = "company_tickers.json"
COMPANY_CACHE_TTL_S = 7 * 24 * 3600
MERGER_SIBLING_CACHE_FILE = "merger_siblings.json"
MERGER_SIBLING_PRUNE_DAYS = 30

# Post-scan market-cap resolution budget. The post-scan pass runs AFTER the
# SEC rate-limited loops finish, but still inside Modal's 120s wall. yfinance
# cold calls are 0.5–2s each and can hang; a parallel pool + soft budget
# protects the wall timeout. Matches the esma_short_scanner pattern (file
# ref: modal_workers/scanners/esma_short_scanner.py:821).
POST_SCAN_MARKET_CAP_BUDGET_S = 25.0
POST_SCAN_MARKET_CAP_WORKERS = 10

_MARKET_CAP_MEMO: Dict[str, Optional[float]] = {}
_DEFAULT_ISSUER_FILTER_CACHE: Optional[Dict[str, Any]] = None

SIGNAL_KEYWORDS: Dict[str, List[str]] = {
    "activist": [
        "strategic alternatives", "board representation", "maximize shareholder value",
        "undervalued", "change in control", "special committee", "proxy contest",
        "consent solicitation",
    ],
    "distress": [
        "going concern", "covenant breach", "waiver", "forbearance agreement",
        "material weakness", "restatement", "liquidity shortfall",
        "substantial doubt", "debtor-in-possession",
    ],
    "mna": [
        "merger agreement", "tender offer", "fairness opinion",
        "change of control", "break-up fee", "definitive agreement",
        "received indication of interest",
    ],
    "governance": [
        "poison pill", "rights plan", "bylaw amendment", "declassify board",
        "auditor resignation", "whistleblower", "internal investigation",
    ],
    # P0 #2 (2026-05-08) — close the FDA-outcome detection gap that left AXSM
    # stuck-active for 8d post-PDUFA. Curated low-FP phrases only; whitelisted
    # to 8-K / 8-K/A so risk-factor boilerplate in 10-K/10-Q doesn't fire.
    # Per-keyword direction is resolved via _FDA_OUTCOME_KEYWORD_DIRECTION
    # below since this bucket emits both long (approval) and short (CRL).
    "fda_outcome": [
        "approved by the U.S. Food and Drug Administration",
        "received FDA approval",
        "supplemental new drug application approved",
        "NDA approved",
        "BLA approved",
        "Complete Response Letter",
        "received a CRL",
    ],
}

_FDA_OUTCOME_KEYWORD_DIRECTION: Dict[str, str] = {
    "approved by the U.S. Food and Drug Administration": "long",
    "received FDA approval": "long",
    "supplemental new drug application approved": "long",
    "NDA approved": "long",
    "BLA approved": "long",
    "Complete Response Letter": "short",
    "received a CRL": "short",
}

# Per-keyword raw_payload.status emitted for fda_outcome hits. Matches the
# pre_edge_monitor binary-resolution vocabulary so an fda_outcome signal flows
# through `_binary_resolution` and triggers a deterministic deliver/kill.
_FDA_OUTCOME_KEYWORD_STATUS: Dict[str, str] = {
    "approved by the U.S. Food and Drug Administration": "approved",
    "received FDA approval": "approved",
    "supplemental new drug application approved": "approved",
    "NDA approved": "approved",
    "BLA approved": "approved",
    "Complete Response Letter": "crl",
    "received a CRL": "crl",
}

SIGNAL_FILING_TYPES: Dict[str, List[str]] = {
    "activist_ownership": ["SC 13D", "SC 13D/A"],
    "late_filings": ["NT 10-K", "NT 10-K/A", "NT 10-Q", "NT 10-Q/A"],
}

KEYWORD_SKIP_FORMS = {
    "ARS", "DEF 14A", "DEFA14A", "DEFM14A", "PRE 14A",
    "N-CSR", "N-CSRS", "497", "497K", "NPORT-P",
}

CATEGORY_FORM_WHITELIST: Dict[str, set] = {
    "distress":   {"10-K", "10-K/A", "10-Q", "10-Q/A", "8-K", "8-K/A"},
    "activist":   {"8-K", "8-K/A", "SC 13D", "SC 13D/A", "SC 14D9", "PRER14A", "DFAN14A"},
    "mna":        {"8-K", "8-K/A", "SC 13D", "SC 13D/A", "SC TO-T", "SC TO-T/A",
                   "SC 13E3", "SC 13E3/A", "PREM14A"},
    # Governance keywords (poison pill / rights plan / auditor resignation /
    # internal investigation) appear in every 10-K and 10-Q as standard risk-
    # factor and disclosure-control boilerplate. Substantive governance events
    # are 8-K-disclosable. Restricting to 8-K trades a small recall loss on
    # buried-in-MD&A disclosures for a large precision win.
    "governance": {"8-K", "8-K/A"},
    # FDA approval / CRL announcements are filed under 8-K Item 8.01 (Other
    # Events) or 7.01 (Reg FD). Whitelisting 8-K only avoids matches on the
    # boilerplate "FDA approval" phrase that appears in every drug-issuer 10-K
    # risk-factor section.
    "fda_outcome": {"8-K", "8-K/A"},
}

SPAC_IPO_FORM_BLACKLIST = {
    "S-1", "S-1/A", "S-4", "S-4/A", "F-1", "F-1/A", "F-4", "F-4/A",
    "DRS", "DRS/A", "SB-2", "SB-2/A",
    "425", "SC TO-C", "SC TO-C/A", "424B3", "424B4", "424B5",
}

# Merger-agreement sibling forms used to disqualify activist-category keyword hits
# on 8-K (see `_has_merger_sibling`).
#
# Broadened 2026-04-21 after the QXO/TopBuild DLQ incident (operator_flags
# kind='scanner_miscategorization_activist_vs_mna'): the 2026-04-18 $17B all-cash
# merger 8-K fired activist_keyword on the "board representation" governance
# clause inside the merger agreement, but the narrow form list + 3d window
# missed the companion S-4 / DEFM14A that were filed on related but different
# days. The current list covers the common M&A co-filing ecosystem:
#
#   425     — prospectus communications during business combination
#   PREM14A — preliminary merger proxy
#   DEFM14A — definitive merger proxy (added 2026-04-21)
#   DEFA14A — additional soliciting material during M&A (added 2026-04-21)
#   SC TO-T — third-party tender offer
#   SC TO-I — issuer self-tender (added 2026-04-21)
#   SC 14D9 — target's response to tender (added 2026-04-21)
#   S-4     — registration of securities in business combination (added 2026-04-21)
#
# A real activist campaign does not co-file any of these within a week of its
# 8-K. Window extended 3→7 days to tolerate timing drift.
MERGER_SIBLING_FORMS = (
    "425", "PREM14A", "DEFM14A", "DEFA14A",
    "SC TO-T", "SC TO-I", "SC 14D9", "S-4",
)
MERGER_SIBLING_WINDOW_DAYS = 7

# Category → thesis direction (v2 addition; v1 scanner didn't emit this but the
# reactor + convergence classification need it for contradiction detection).
# `fda_outcome` resolves direction per-keyword (see _resolve_thesis_direction)
# because the bucket carries both long (approval) and short (CRL) phrases.
_CATEGORY_DIRECTION: Dict[str, str] = {
    "activist": "long",      # 13D accumulator bullish on target
    "mna": "long",           # target expected to rise
    "distress": "short",     # going concern / restatement bearish
    "governance": "neutral", # poison pill etc. ambiguous until actor known
    "fda_outcome": "neutral",  # placeholder; per-keyword override below
}


def _resolve_thesis_direction(category: str, keyword: str) -> Optional[str]:
    """Direction for a keyword hit. Special-cases fda_outcome which carries
    both directions in one bucket (approvals = long, CRLs = short)."""
    if category == "fda_outcome":
        return _FDA_OUTCOME_KEYWORD_DIRECTION.get(keyword) or _CATEGORY_DIRECTION.get(category)
    return _CATEGORY_DIRECTION.get(category)

_FILING_TYPE_DIRECTION: Dict[str, str] = {
    "activist_ownership": "long",
    "late_filings": "short",
}

_EXCHANGE_TO_MIC: Dict[str, str] = {
    "NASDAQ": "XNAS",
    "NYSE": "XNYS",
    "NYSE AMERICAN": "XASE",
    "AMERICAN STOCK EXCHANGE": "XASE",
    "AMEX": "XASE",
    "ARCA": "ARCX",
    "BATS": "BATS",
    "IEX": "IEXG",
}

# Category → signal_type name. Preserved from v1 (`{category}_keyword`).
_CATEGORY_SIGNAL_TYPE: Dict[str, str] = {
    "activist":   "activist_keyword",
    "distress":   "distress_keyword",
    "mna":        "mna_keyword",
    "governance": "governance_keyword",
    # fda_outcome emits a distinct signal_type so downstream (pre_edge_monitor,
    # convergence) can route it to FDA-specific resolution logic.
    "fda_outcome": "fda_outcome_8k",
}

# ---------------------------------------------------------------------------
# Rate limiter (verbatim from v1)
# ---------------------------------------------------------------------------

class _SECRateLimiter:
    def __init__(self, max_per_sec: int = SEC_RATE_LIMIT):
        self.max_per_sec = max_per_sec
        self._timestamps: List[float] = []

    def wait(self) -> None:
        now = time.time()
        self._timestamps = [t for t in self._timestamps if now - t < 1.0]
        if len(self._timestamps) >= self.max_per_sec:
            sleep_time = 1.0 - (now - self._timestamps[0]) + 0.05
            if sleep_time > 0:
                time.sleep(sleep_time)
        self._timestamps.append(time.time())


_rate_limiter = _SECRateLimiter()


# ---------------------------------------------------------------------------
# HTTP session (shared across all SEC calls for TCP/TLS keep-alive)
# ---------------------------------------------------------------------------

_SEC_SESSION: Optional[requests.Session] = None


def _sec_session() -> requests.Session:
    """Shared requests.Session used for every SEC GET.

    Before 2026-04-22 each `_efts_search` / `_get_company_tickers` /
    `_has_merger_sibling` call invoked `requests.get(...)` directly, which
    opens a fresh TCP+TLS handshake every time. With 37 EFTS queries plus
    300–400 submissions lookups per run, that handshake overhead is 30–40%
    of per-request latency. A pooled session reuses connections across the
    whole scan.
    """
    global _SEC_SESSION
    if _SEC_SESSION is None:
        session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=10,
            pool_maxsize=10,
            max_retries=0,  # retries handled per-caller with our own backoff
        )
        session.mount("https://", adapter)
        _SEC_SESSION = session
    return _SEC_SESSION


def _http_get(url: str, *, params: Optional[Dict[str, Any]] = None,
              headers: Optional[Dict[str, str]] = None,
              timeout: Optional[float] = None) -> requests.Response:
    """Single seam for SEC HTTP GETs. Tests patch this (not `requests.get`)
    so that the pooled session is covered by the same mock surface."""
    return _sec_session().get(url, params=params, headers=headers, timeout=timeout)


# ---------------------------------------------------------------------------
# Budget + metrics
# ---------------------------------------------------------------------------

def _remaining_budget_s(started_at: float, budget_s: float) -> float:
    if budget_s <= 0:
        return float("inf")
    return max(0.0, budget_s - (time.time() - started_at))


def _has_budget_for_query(started_at: float, budget_s: float, reserve_s: float = 0.0) -> bool:
    if budget_s <= 0:
        return True
    return _remaining_budget_s(started_at, budget_s) > (reserve_s + MIN_QUERY_BUDGET_S)


def _filing_phase_reserve_s(budget_s: float) -> float:
    if budget_s <= 0:
        return 0.0
    return min(FILING_PHASE_RESERVE_S, max(0.0, budget_s * 0.25))


def _new_run_metrics(
    *,
    budget_s: float,
    coverage_mode: str,
    categories_requested: List[str],
    filing_types_requested: List[str],
) -> Dict[str, Any]:
    return {
        "coverage_mode": coverage_mode,
        "budget_seconds": budget_s,
        "budget_remaining_seconds": None,
        "budget_exhausted": False,
        "degraded": False,
        "partial_reasons": [],
        "categories_requested": categories_requested,
        "categories_completed": [],
        "filing_types_requested": filing_types_requested,
        "filing_types_completed": [],
        "keyword_queries_attempted": 0,
        "filing_queries_attempted": 0,
        "retries_attempted": 0,
        "efts_failures": 0,
        "efts_failure_details": [],
        "dedup_skipped": 0,
        "issuer_filtered_total": 0,
        "issuer_filtered_by_reason": {},
        "issuer_filter_samples": [],
        "market_cap_filtered_total": 0,
        "market_cap_unknown_total": 0,
        "market_cap_filter_enabled": True,
        "market_cap_floor_usd_mm": DEFAULT_MARKET_CAP_FLOOR_USD_MM,
        "merger_suppressed_total": 0,
        "signals_detected": 0,
        "fetched_records": 0,
        "skipped_due_to_budget": 0,
    }


def _mark_partial(metrics: Dict[str, Any], reason: str) -> None:
    metrics["degraded"] = True
    reasons = metrics.setdefault("partial_reasons", [])
    if reason not in reasons:
        reasons.append(reason)


def _status_code_from_exc(exc: Exception) -> Optional[int]:
    resp = getattr(exc, "response", None)
    return getattr(resp, "status_code", None) if resp is not None else None


def _is_retriable_efts_failure(exc: Exception) -> bool:
    if isinstance(exc, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
        return True
    status_code = _status_code_from_exc(exc)
    return status_code in RETRYABLE_STATUS_CODES


def _retry_backoff_s(attempt: int) -> float:
    return min(4.0, 0.6 * (2 ** max(0, attempt - 1))) + 0.05


def _record_efts_failure(
    metrics: Dict[str, Any],
    *,
    query: str,
    form_type: str,
    status_code: Optional[int],
    retries_attempted: int,
    error: str,
    retriable: bool,
) -> None:
    metrics["efts_failures"] = metrics.get("efts_failures", 0) + 1
    _mark_partial(metrics, "transient_efts_failure" if retriable else "efts_failure")
    details = metrics.setdefault("efts_failure_details", [])
    if len(details) < MAX_EFTS_FAILURE_DETAILS:
        details.append({
            "query": query,
            "form_type": form_type or "",
            "status_code": status_code,
            "retries_attempted": retries_attempted,
            "retriable": retriable,
            "error": error[:240],
        })


def _record_issuer_filtered(
    metrics: Dict[str, Any],
    hit: Dict[str, Any],
    reason: str,
    *,
    ticker: Optional[str] = None,
    cik: Optional[str] = None,
) -> None:
    metrics["issuer_filtered_total"] = metrics.get("issuer_filtered_total", 0) + 1
    by_reason = metrics.setdefault("issuer_filtered_by_reason", {})
    by_reason[reason] = by_reason.get(reason, 0) + 1
    samples = metrics.setdefault("issuer_filter_samples", [])
    if len(samples) < MAX_ISSUER_FILTER_SAMPLES:
        samples.append({
            "company_name": hit.get("company_name", ""),
            "cik": cik or hit.get("cik") or "",
            "ticker": ticker or "",
            "form": hit.get("form", ""),
            "reason": reason,
            "file_description": (hit.get("file_description") or "")[:160],
        })


def _record_market_cap_filtered(
    metrics: Dict[str, Any],
    *,
    ticker: str,
    market_cap_usd_mm: float,
) -> None:
    metrics["market_cap_filtered_total"] = metrics.get("market_cap_filtered_total", 0) + 1
    samples = metrics.setdefault("market_cap_filtered_samples", [])
    if len(samples) < 10:
        samples.append({
            "ticker": ticker,
            "market_cap_usd_mm": round(market_cap_usd_mm, 2),
        })


# ---------------------------------------------------------------------------
# EFTS + submissions
# ---------------------------------------------------------------------------

def _efts_search(query: str, date_from: str, date_to: str,
                 form_type: str = "", max_results: int = 50,
                 *, user_agent: str,
                 metrics: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    params: Dict[str, Any] = {
        "q": query, "dateRange": "custom",
        "startdt": date_from, "enddt": date_to,
    }
    if form_type:
        params["forms"] = form_type

    attempt = 0
    while True:
        _rate_limiter.wait()
        try:
            resp = _http_get(EFTS_URL, params=params,
                             headers={"User-Agent": user_agent},
                             timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            break
        except requests.exceptions.RequestException as exc:
            retriable = _is_retriable_efts_failure(exc)
            status_code = _status_code_from_exc(exc)
            if retriable and attempt < MAX_EFTS_RETRIES:
                attempt += 1
                if metrics is not None:
                    metrics["retries_attempted"] = metrics.get("retries_attempted", 0) + 1
                time.sleep(_retry_backoff_s(attempt))
                continue
            if metrics is not None:
                _record_efts_failure(
                    metrics,
                    query=query,
                    form_type=form_type,
                    status_code=status_code,
                    retries_attempted=attempt,
                    error=str(exc),
                    retriable=retriable,
                )
            return []

    results: List[Dict[str, Any]] = []
    for hit in data.get("hits", {}).get("hits", [])[:max_results]:
        src = hit.get("_source", {})
        cik = src.get("ciks", [""])[0] if src.get("ciks") else ""
        adsh = src.get("adsh", "")
        raw_name = src.get("display_names", [""])[0] if src.get("display_names") else ""
        company_name = re.sub(r"\s*\(CIK\s+\d+\)\s*$", "", raw_name).strip()

        filing_url = ""
        if cik and adsh:
            cik_stripped = cik.lstrip("0") or "0"
            adsh_clean = adsh.replace("-", "")
            filing_url = f"https://www.sec.gov/Archives/edgar/data/{cik_stripped}/{adsh_clean}"

        results.append({
            "cik": cik,
            "adsh": adsh,
            "company_name": company_name,
            "company_raw": raw_name,
            "form": src.get("form", ""),
            "file_date": src.get("file_date", ""),
            "file_description": src.get("file_description", ""),
            "filing_url": filing_url,
            "sics": src.get("sics", []),
        })
    return results


def _get_company_tickers(cik: str, *, user_agent: str) -> Tuple[List[str], Optional[str]]:
    if not cik:
        return [], None
    cik_padded = cik.zfill(10)
    url = SUBMISSIONS_URL.format(cik=cik_padded)
    _rate_limiter.wait()
    try:
        resp = _http_get(url, headers={"User-Agent": user_agent},
                         timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            data = resp.json()
            tickers = data.get("tickers", []) or []
            exchanges = data.get("exchanges", []) or []
            return tickers, exchanges[0] if exchanges else None
    except Exception:
        pass
    return [], None


def _load_company_context(
    cik: str,
    *,
    user_agent: str,
    cache: Dict[str, Dict[str, Any]],
    dirty: Optional[List[bool]] = None,
) -> Tuple[List[str], Optional[str]]:
    """Resolve CIK → (tickers, primary_exchange) via a persistent per-CIK cache.

    Before 2026-04-22 this cache lived only in `scan()`-local scope and was
    rebuilt from scratch every 3h run, issuing ~300–400 rate-limited
    `data.sec.gov/submissions/CIK{cik}.json` calls per run. With a
    Supabase-backed map reused across runs (7-day per-entry TTL — tickers
    change rarely) cold calls drop to "net-new CIKs we've never seen".

    `cache` is a `{cik_padded: {tickers, exchange, cached_at}}` dict shared
    with the persistent loader. `dirty[0]` is flipped to True whenever a
    fetch-and-store happens so `scan()` can skip the save round-trip when
    no entries changed.
    """
    if not cik:
        return [], None
    padded = cik.zfill(10)
    now = time.time()
    entry = cache.get(padded)
    if isinstance(entry, dict):
        cached_at = float(entry.get("cached_at") or 0)
        if now - cached_at < COMPANY_CACHE_TTL_S:
            tickers = list(entry.get("tickers") or [])
            exchange = entry.get("exchange")
            return tickers, exchange if isinstance(exchange, str) or exchange is None else None
    tickers, exchange = _get_company_tickers(cik, user_agent=user_agent)
    cache[padded] = {
        "tickers": list(tickers),
        "exchange": exchange,
        "cached_at": now,
    }
    if dirty is not None:
        dirty[0] = True
    return tickers, exchange


def _mic_for_exchange(exchange: Optional[str]) -> Optional[str]:
    if not exchange:
        return None
    key = re.sub(r"\s+", " ", exchange.strip().upper())
    return _EXCHANGE_TO_MIC.get(key)


def _merger_sibling_key(cik: str, file_date_str: str) -> str:
    """Serializable (cik|file_date) key used for both in-memory and
    persisted merger-sibling caches."""
    return f"{cik}|{file_date_str}"


def _has_merger_sibling(cik: str, file_date_str: str, *, user_agent: str,
                        cache: Dict[str, bool],
                        session_cache: Optional[Dict[str, bool]] = None,
                        dirty: Optional[List[bool]] = None) -> bool:
    """Return True if the same CIK has a merger-agreement sibling filing
    (425 / PREM14A / SC TO-T) within ±MERGER_SIBLING_WINDOW_DAYS of file_date.

    Used to suppress activist-category keyword hits on 8-K that are really
    mechanical governance clauses inside a merger announcement (see
    QXO-TopBuild 2026-04-18 DLQ incident).

    Two-tier caching — the crucial invariant is that `True` answers are
    **monotonic** (filings don't un-file) but `False` answers are NOT (a
    companion PREM14A / DEFM14A can be filed days after the 8-K). So:

      - `cache` is the cross-run persistent map — we **only write True
        results** here. Cached True entries serve every subsequent run
        without re-querying.
      - `session_cache` is an optional same-run dict that dedupes repeated
        `False`-answer lookups for the same (cik, file_date) within one
        scan (e.g., three activist keywords landing on one 8-K). Missing
        → default behavior is safe but re-queries per hit.

    `dirty[0]` flips to True only when a new True entry lands in the
    persistent cache, so the caller can skip re-saving when nothing
    persistent changed.
    """
    if not cik or not file_date_str:
        return False
    key = _merger_sibling_key(cik, file_date_str)
    # Persistent cache stores only True. A hit here is authoritative.
    if cache.get(key) is True:
        return True
    # Same-run dedup for False answers (if caller provided a session cache).
    if session_cache is not None and key in session_cache:
        return session_cache[key]
    try:
        anchor = datetime.strptime(file_date_str, "%Y-%m-%d")
    except ValueError:
        if session_cache is not None:
            session_cache[key] = False
        return False
    date_from = (anchor - timedelta(days=MERGER_SIBLING_WINDOW_DAYS)).strftime("%Y-%m-%d")
    date_to = (anchor + timedelta(days=MERGER_SIBLING_WINDOW_DAYS)).strftime("%Y-%m-%d")

    params: Dict[str, Any] = {
        "q": "",
        "dateRange": "custom",
        "startdt": date_from,
        "enddt": date_to,
        "forms": ",".join(MERGER_SIBLING_FORMS),
        "ciks": cik.zfill(10),
    }
    _rate_limiter.wait()
    try:
        resp = _http_get(EFTS_URL, params=params,
                         headers={"User-Agent": user_agent},
                         timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException:
        # Fail open — do not suppress on network error. Record the False in
        # session cache only so the same hit within this run doesn't re-query.
        if session_cache is not None:
            session_cache[key] = False
        return False

    hit = bool(data.get("hits", {}).get("hits", []))
    if hit:
        cache[key] = True
        if dirty is not None:
            dirty[0] = True
    if session_cache is not None:
        session_cache[key] = hit
    return hit


# ---------------------------------------------------------------------------
# Issuer filters
# ---------------------------------------------------------------------------

def _empty_issuer_filter() -> Dict[str, Any]:
    return {
        "blocked_ciks": set(),
        "allowlist_ciks": set(),
        "allowlist_tickers": set(),
        "name_patterns_ci": [],
        "description_patterns_ci": [],
        "_name_regexes": [],
        "_description_regexes": [],
    }


def _build_issuer_filter(raw: Dict[str, Any]) -> Dict[str, Any]:
    loaded = _empty_issuer_filter()
    loaded["blocked_ciks"] = {
        str(cik).zfill(10)
        for cik in (raw.get("blocked_ciks") or [])
        if str(cik).strip()
    }
    loaded["allowlist_ciks"] = {
        str(cik).zfill(10)
        for cik in (raw.get("allowlist_ciks") or [])
        if str(cik).strip()
    }
    loaded["allowlist_tickers"] = {
        str(ticker).upper()
        for ticker in (raw.get("allowlist_tickers") or [])
        if str(ticker).strip()
    }
    loaded["name_patterns_ci"] = [
        str(pattern)
        for pattern in (raw.get("name_patterns_ci") or [])
        if str(pattern).strip()
    ]
    loaded["description_patterns_ci"] = [
        str(pattern)
        for pattern in (raw.get("description_patterns_ci") or [])
        if str(pattern).strip()
    ]

    for pattern in loaded["name_patterns_ci"]:
        try:
            loaded["_name_regexes"].append((pattern, re.compile(pattern, re.IGNORECASE)))
        except re.error:
            continue
    for pattern in loaded["description_patterns_ci"]:
        try:
            loaded["_description_regexes"].append((pattern, re.compile(pattern, re.IGNORECASE)))
        except re.error:
            continue
    return loaded


def _load_default_issuer_filter() -> Dict[str, Any]:
    global _DEFAULT_ISSUER_FILTER_CACHE
    if _DEFAULT_ISSUER_FILTER_CACHE is not None:
        return _DEFAULT_ISSUER_FILTER_CACHE
    raw: Dict[str, Any] = dict(DEFAULT_EDGAR_ISSUER_FILTER)
    if not ISSUER_FILTER_FILE.exists():
        _DEFAULT_ISSUER_FILTER_CACHE = _build_issuer_filter(raw)
        return _DEFAULT_ISSUER_FILTER_CACHE
    try:
        loaded = json.loads(ISSUER_FILTER_FILE.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            raw = loaded
    except Exception:
        pass
    _DEFAULT_ISSUER_FILTER_CACHE = _build_issuer_filter(raw)
    return _DEFAULT_ISSUER_FILTER_CACHE


def _resolve_issuer_filter(cfg: ScannerConfig) -> Dict[str, Any]:
    base = _load_default_issuer_filter()
    overrides = cfg.config.get("issuer_filter_overrides")
    if not isinstance(overrides, dict):
        return base
    merged_raw = {
        "blocked_ciks": sorted(base["blocked_ciks"] | {
            str(cik).zfill(10)
            for cik in (overrides.get("blocked_ciks") or [])
            if str(cik).strip()
        }),
        "allowlist_ciks": sorted(base["allowlist_ciks"] | {
            str(cik).zfill(10)
            for cik in (overrides.get("allowlist_ciks") or [])
            if str(cik).strip()
        }),
        "allowlist_tickers": sorted(base["allowlist_tickers"] | {
            str(ticker).upper()
            for ticker in (overrides.get("allowlist_tickers") or [])
            if str(ticker).strip()
        }),
        "name_patterns_ci": base["name_patterns_ci"] + [
            str(pattern)
            for pattern in (overrides.get("name_patterns_ci") or [])
            if str(pattern).strip()
        ],
        "description_patterns_ci": base["description_patterns_ci"] + [
            str(pattern)
            for pattern in (overrides.get("description_patterns_ci") or [])
            if str(pattern).strip()
        ],
    }
    return _build_issuer_filter(merged_raw)


def _match_pattern(texts: List[str], regexes: List[Tuple[str, Any]]) -> Optional[str]:
    for label, regex in regexes:
        for text in texts:
            if text and regex.search(text):
                return label
    return None


def _is_spac_or_shell_issuer(
    hit: Dict[str, Any],
    *,
    cik: Optional[str],
    issuer_filter: Dict[str, Any],
    ticker_resolver: Optional[Callable[[], Optional[str]]] = None,
) -> Tuple[bool, Optional[str], Optional[str]]:
    normalized_cik = str(cik or hit.get("cik") or "").zfill(10) if (cik or hit.get("cik")) else None

    if normalized_cik and normalized_cik in issuer_filter.get("allowlist_ciks", set()):
        return False, None, None
    if normalized_cik and normalized_cik in issuer_filter.get("blocked_ciks", set()):
        return True, "blocked_cik", None

    company_name = str(hit.get("company_name") or "")
    company_raw = str(hit.get("company_raw") or "")
    file_description = str(hit.get("file_description") or "")

    name_reason = _match_pattern([company_name, company_raw], issuer_filter.get("_name_regexes", []))
    desc_reason = _match_pattern([file_description], issuer_filter.get("_description_regexes", []))
    if not name_reason and not desc_reason:
        return False, None, None

    ticker: Optional[str] = None
    if ticker_resolver is not None and issuer_filter.get("allowlist_tickers"):
        ticker = ticker_resolver()
        if ticker and ticker.upper() in issuer_filter.get("allowlist_tickers", set()):
            return False, None, ticker.upper()

    if name_reason:
        return True, f"name_pattern:{name_reason}", ticker
    if desc_reason:
        return True, f"description_pattern:{desc_reason}", ticker
    return False, None, ticker


# ---------------------------------------------------------------------------
# Dedup + rotation (Storage-backed)
# ---------------------------------------------------------------------------

def _signal_hash(cik: str, keyword: str, signal_type: str) -> str:
    return hashlib.md5(f"{cik}|{keyword}|{signal_type}".encode()).hexdigest()


def _is_novel(cik: str, keyword: str, signal_type: str,
              dedup_log: Dict[str, str],
              window_days: int = DEDUP_WINDOW_DAYS) -> bool:
    h = _signal_hash(cik, keyword, signal_type)
    if h in dedup_log:
        try:
            first_date = datetime.strptime(dedup_log[h], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - first_date).days < window_days:
                return False
        except ValueError:
            pass
    return True


def _load_dedup(client: SupabaseClient) -> Dict[str, str]:
    raw = client.read_cache("edgar", "dedup.json")
    if raw is None:
        return {}
    try:
        import json
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return {}


def _save_dedup(client: SupabaseClient, log: Dict[str, str]) -> None:
    import json
    client.write_cache("edgar", "dedup.json", json.dumps(log).encode("utf-8"),
                       content_type="application/json")


def _load_rotation(client: SupabaseClient) -> Dict[str, Any]:
    raw = client.read_cache("edgar", "rotation.json")
    if raw is None:
        return {"rotation_index": -1, "scan_history": {}}
    try:
        import json
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return {"rotation_index": -1, "scan_history": {}}


def _save_rotation(client: SupabaseClient, state: Dict[str, Any]) -> None:
    import json
    client.write_cache("edgar", "rotation.json", json.dumps(state).encode("utf-8"),
                       content_type="application/json")


def _coerce_cache_blob(raw: Any) -> Optional[Dict[str, Any]]:
    """Decode a Supabase-Storage cache blob. Returns None on any problem.

    Defensive against non-bytes returns (MagicMock in tests, partial reads,
    corrupted blobs). Treating any failure as "cold cache" is safe — we
    just re-fetch and re-save — so this swallows broadly rather than raising.
    """
    if raw is None:
        return None
    if not isinstance(raw, (bytes, bytearray, str)):
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _load_persistent_ticker_cache(client: SupabaseClient) -> Dict[str, Dict[str, Any]]:
    """Load the cross-run CIK → {tickers, exchange, cached_at} map from
    scanner-caches/edgar/company_tickers.json. Returns {} on any failure."""
    try:
        raw = client.read_cache("edgar", COMPANY_CACHE_FILE, timeout=4.0)
    except Exception:
        return {}
    data = _coerce_cache_blob(raw)
    if data is None:
        return {}
    entries = data.get("entries")
    if not isinstance(entries, dict):
        return {}
    # Normalize: drop malformed rows defensively.
    clean: Dict[str, Dict[str, Any]] = {}
    for k, v in entries.items():
        if isinstance(k, str) and isinstance(v, dict):
            clean[k] = v
    return clean


def _save_persistent_ticker_cache(
    client: SupabaseClient, entries: Dict[str, Dict[str, Any]]
) -> None:
    """Best-effort save. Storage failures are swallowed so a stalled Supabase
    write never breaks the scanner's completion path."""
    try:
        payload = {"cached_at": time.time(), "entries": entries}
        client.write_cache(
            "edgar", COMPANY_CACHE_FILE,
            json.dumps(payload).encode("utf-8"),
            content_type="application/json",
        )
    except Exception:
        pass


def _load_merger_sibling_cache(client: SupabaseClient) -> Dict[str, bool]:
    """Load the cross-run `{cik|file_date: has_sibling}` cache from
    scanner-caches/edgar/merger_siblings.json. Returns {} on any failure.

    No TTL on the blob — the answer for a given (cik, file_date) is stable
    (SEC filings don't change retroactively). Pruning of stale file_dates
    happens at save time via `_prune_merger_sibling_cache`.
    """
    try:
        raw = client.read_cache("edgar", MERGER_SIBLING_CACHE_FILE, timeout=4.0)
    except Exception:
        return {}
    data = _coerce_cache_blob(raw)
    if data is None:
        return {}
    entries = data.get("entries")
    if not isinstance(entries, dict):
        return {}
    clean: Dict[str, bool] = {}
    for k, v in entries.items():
        if isinstance(k, str) and isinstance(v, bool):
            clean[k] = v
    return clean


def _prune_merger_sibling_cache(entries: Dict[str, bool]) -> Dict[str, bool]:
    """Drop cache rows whose file_date is older than MERGER_SIBLING_PRUNE_DAYS.
    Entries with no parseable date are kept (safe default)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=MERGER_SIBLING_PRUNE_DAYS)).strftime("%Y-%m-%d")
    pruned: Dict[str, bool] = {}
    for key, value in entries.items():
        if "|" not in key:
            pruned[key] = value
            continue
        _, file_date = key.split("|", 1)
        if file_date >= cutoff:  # ISO YYYY-MM-DD lexical compare == chronological
            pruned[key] = value
    return pruned


def _save_merger_sibling_cache(client: SupabaseClient, entries: Dict[str, bool]) -> None:
    """Best-effort save with stale-date pruning."""
    try:
        payload = {
            "cached_at": time.time(),
            "entries": _prune_merger_sibling_cache(entries),
        }
        client.write_cache(
            "edgar", MERGER_SIBLING_CACHE_FILE,
            json.dumps(payload).encode("utf-8"),
            content_type="application/json",
        )
    except Exception:
        pass


def _rotation_state_for_mode(client: SupabaseClient, coverage_mode: str) -> Tuple[List[str], Optional[Dict[str, Any]]]:
    if coverage_mode == "rotation":
        rotation_state = _load_rotation(client)
        next_idx = (rotation_state.get("rotation_index", -1) + 1) % len(ROTATION_ORDER)
        category = ROTATION_ORDER[next_idx]
        rotation_state["rotation_index"] = next_idx
        rotation_state["last_category"] = category
        rotation_state["last_scan_ts"] = datetime.now(timezone.utc).isoformat()
        rotation_state.setdefault("scan_history", {})[category] = rotation_state["last_scan_ts"]
        return [category], rotation_state
    return list(ROTATION_ORDER), None


def _market_cap_cache_key(ticker: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.@-]", "_", ticker.upper())
    return f"{safe}@US"


def _load_market_cap_usd_mm(
    client: SupabaseClient,
    ticker: str,
    memo: Dict[str, Optional[float]],
) -> Optional[float]:
    normalized = ticker.upper().strip()
    if not normalized:
        return None
    if normalized in memo:
        return memo[normalized]

    cache_key = _market_cap_cache_key(normalized)
    try:
        raw = client.read_cache(MARKET_CAP_CACHE_PREFIX, f"{cache_key}.json", timeout=4.0)
    except Exception:
        raw = None

    if raw is not None:
        try:
            payload = json.loads(raw)
            cached_at = float(payload.get("cached_at") or 0)
            snapshot = payload.get("snapshot") or {}
            market_cap_usd = snapshot.get("market_cap_usd")
            if cached_at and time.time() - cached_at <= MARKET_CAP_CACHE_TTL_S and market_cap_usd is not None:
                memo[normalized] = round(float(market_cap_usd) / 1_000_000, 2)
                return memo[normalized]
        except Exception:
            pass

    try:
        import yfinance as yf  # type: ignore
    except ImportError:
        memo[normalized] = None
        return None

    market_cap = None
    try:
        instrument = yf.Ticker(normalized)
        fast_info = instrument.fast_info or {}
        info = instrument.info or {}
        market_cap = fast_info.get("marketCap") or info.get("marketCap")
    except Exception:
        memo[normalized] = None
        return None

    if market_cap is None:
        memo[normalized] = None
        return None

    market_cap_usd = float(market_cap)
    memo[normalized] = round(market_cap_usd / 1_000_000, 2)
    try:
        client.write_cache(
            MARKET_CAP_CACHE_PREFIX,
            f"{cache_key}.json",
            json.dumps({
                "cached_at": time.time(),
                "snapshot": {
                    "market_cap_usd": market_cap_usd,
                    "market_snapshot_source": "yfinance",
                    "market_snapshot_symbol": normalized,
                    "market_snapshot_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                },
            }).encode("utf-8"),
            content_type="application/json",
        )
    except Exception:
        pass
    return memo[normalized]


def _resolve_market_caps_parallel(
    candidates: List[Tuple["Signal", Optional[str], Optional[str]]],
    client: SupabaseClient,
    memo: Dict[str, Optional[float]],
    *,
    budget_s: float = POST_SCAN_MARKET_CAP_BUDGET_S,
    max_workers: int = POST_SCAN_MARKET_CAP_WORKERS,
) -> Tuple[Dict[int, Optional[float]], bool]:
    """Resolve market caps for every candidate in parallel, honoring a
    soft wall-clock budget.

    Returns (`by_index`, `budget_exhausted`):
      - `by_index[i]` is the resolved cap (or None) for candidate `i`.
        Every index in `range(len(candidates))` is present — un-resolved
        candidates map to None, which the caller treats as "unknown".
      - `budget_exhausted` is True if we hit the wall before every future
        completed, so scan() can surface `market_cap_budget_exhausted` as
        a partial_reason.

    Fast path: candidates whose ticker is already in `memo` skip the
    ThreadPoolExecutor entirely. Cold candidates fan out to the pool.

    Threading pattern mirrors modal_workers/scanners/esma_short_scanner.py
    (line 821): the executor is NOT used as a `with` context; `shutdown`
    is called in `finally` with `cancel_futures=True` so a stuck yfinance
    call never blocks Modal's wall timeout. Thread-safety verified — each
    `_load_market_cap_usd_mm` call works on distinct per-ticker state and
    the shared `memo` / `SupabaseClient._session` are safe for concurrent
    use (worst case: redundant yfinance call on a race, not corruption).
    """
    by_index: Dict[int, Optional[float]] = {}
    cold_candidates: List[Tuple[int, str]] = []

    for i, (_, ticker, _) in enumerate(candidates):
        if not ticker:
            by_index[i] = None
            continue
        normalized = ticker.upper().strip()
        if not normalized:
            by_index[i] = None
            continue
        # Memo fast path — we already resolved this ticker earlier in the
        # run (same-run same-ticker cache). Skip the pool.
        if normalized in memo:
            by_index[i] = memo[normalized]
            continue
        cold_candidates.append((i, ticker))

    if not cold_candidates:
        return by_index, False

    budget_exhausted = False
    ex = ThreadPoolExecutor(max_workers=max_workers)
    try:
        futures = {
            ex.submit(_load_market_cap_usd_mm, client, ticker, memo): (i, ticker)
            for i, ticker in cold_candidates
        }
        start = time.time()
        for future, (i, ticker) in futures.items():
            remaining = budget_s - (time.time() - start)
            if remaining <= 0:
                # Budget exhausted: mark remaining as unknown and bail.
                budget_exhausted = True
                for other_future, (j, _) in futures.items():
                    if j not in by_index:
                        other_future.cancel()
                        by_index[j] = None
                break
            try:
                by_index[i] = future.result(timeout=remaining)
            except FuturesTimeout:
                budget_exhausted = True
                by_index[i] = None
            except Exception:  # noqa: BLE001
                # A failed lookup should not kill the run — treat as unknown.
                by_index[i] = None
    finally:
        # Don't wait for stuck yfinance calls — cancel_futures requires 3.9+.
        ex.shutdown(wait=False, cancel_futures=True)

    # Fill in any index we never got around to (shouldn't happen given the
    # loop above, but belt-and-suspenders for the budget-exhausted path).
    for i in range(len(candidates)):
        if i not in by_index:
            by_index[i] = None

    return by_index, budget_exhausted


def _resolve_figi_for_ticker(
    ticker: Optional[str],
    figi_cache: Dict[str, Optional[str]],
) -> Optional[str]:
    if not ticker:
        return None
    normalized = ticker.upper().strip()
    if not normalized:
        return None
    if normalized in figi_cache:
        return figi_cache[normalized]
    try:
        from modal_workers.shared.openfigi_resolver import resolve_ticker

        resolution = resolve_ticker(normalized, exch_code="US")
        figi_cache[normalized] = resolution.issuer_figi if resolution.resolved else None
    except Exception:
        figi_cache[normalized] = None
    return figi_cache[normalized]


# ---------------------------------------------------------------------------
# Strength heuristics (verbatim from v1 _build_signal)
# ---------------------------------------------------------------------------

def _compute_strength(category: str, keyword: str, form: str) -> int:
    strength = 2
    if category == "activist" and "13D" in form:
        strength = 4
    elif category == "distress" and keyword in ("going concern", "substantial doubt"):
        strength = 4
    elif category == "mna":
        if keyword in ("definitive agreement", "merger agreement"):
            ongoing_forms = (
                "SC TO-T", "SC TO-C", "PREM14A", "DEFM14A",
                "S-4", "SC 13E3", "SC TO-I", "SC TO-T/A",
                "SC TO-C/A", "S-4/A", "SC 13E3/A", "SC TO-I/A",
                "DFAN14A", "DEFA14A",
            )
            if any(form.upper().startswith(f) for f in ongoing_forms):
                strength = 2
            else:
                strength = 5
        elif keyword == "tender offer":
            if "SC TO-T" in form.upper() and "/A" not in form.upper():
                strength = 4
            else:
                strength = 2
        else:
            strength = 3
    elif category == "governance" and keyword in ("poison pill", "rights plan"):
        strength = 3
    elif category == "fda_outcome":
        # 8-K approval / CRL announcements are decisive resolution events.
        # Approvals (long) carry slightly higher strength than CRLs because
        # "Complete Response Letter" can appear in narrative summaries of
        # prior years, while "approved by the U.S. Food and Drug
        # Administration" requires the issuer to actively claim the event.
        if _FDA_OUTCOME_KEYWORD_DIRECTION.get(keyword) == "long":
            strength = 5
        else:
            strength = 4
    return strength


# ---------------------------------------------------------------------------
# Signal builder
# ---------------------------------------------------------------------------

def _build_signal(
    hit: Dict[str, Any],
    *,
    matched_keyword: str,
    signal_type: str,
    thesis_direction: Optional[str],
    strength_estimate: int,
    scan_date: datetime,
    tickers: List[str],
    exchange: Optional[str],
    market_cap_usd_mm: Optional[float],
    issuer_figi: Optional[str],
) -> Optional[Signal]:
    cik = hit.get("cik", "")
    adsh = hit.get("adsh", "")
    if not adsh:
        return None

    ticker = tickers[0] if tickers else None
    form = hit.get("form", "")

    source_content_hash = f"sha256:{hashlib.sha256(f'{adsh}|{matched_keyword}|{signal_type}'.encode()).hexdigest()}"
    signal_id = f"edgar_{adsh.replace('-', '')}_{signal_type}_{hashlib.md5(matched_keyword.encode()).hexdigest()[:8]}"

    source_date_str = hit.get("file_date", "")
    try:
        source_date = datetime.strptime(source_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        source_date = scan_date

    raw_payload: Dict[str, Any] = {
        "matched_keyword": matched_keyword,
        "keyword": matched_keyword,
        "excerpt": hit.get("file_description", ""),
        "filing_type": form,
        "cik": cik,
        "adsh": adsh,
        "file_description": hit.get("file_description", ""),
        "company_raw": hit.get("company_raw", ""),
        "company_name": hit.get("company_name", ""),
        "tickers": tickers,
        "exchange": exchange,
        "market_cap_usd_mm": market_cap_usd_mm,
    }

    # P0 #2 — fda_outcome signals carry a `status` field that matches the
    # pre_edge_monitor binary-resolution vocabulary (approved/crl). This lets
    # `_binary_resolution` route the signal to a deterministic deliver/kill
    # without any additional translation layer.
    if signal_type == "fda_outcome_8k":
        fda_status = _FDA_OUTCOME_KEYWORD_STATUS.get(matched_keyword)
        if fda_status:
            raw_payload["status"] = fda_status

    entity_hints = EntityHints(
        issuer_figi=issuer_figi,
        ticker=ticker,
        mic=_mic_for_exchange(exchange),
        cik=cik or None,
        name=hit.get("company_name") or None,
        country="US",
    )

    return Signal(
        signal_id=signal_id,
        source_content_hash=source_content_hash,
        source_date=source_date,
        scan_date=scan_date,
        signal_type=signal_type,
        raw_payload=raw_payload,
        source_url=hit.get("filing_url") or None,
        issuer_figi=issuer_figi,
        entity_hints=entity_hints,
        thesis_direction=thesis_direction,
        strength_estimate=strength_estimate,
    )


def _metric_warnings(metrics: Dict[str, Any]) -> List[str]:
    warnings: List[str] = []
    partial_reasons = metrics.get("partial_reasons") or []
    if metrics.get("budget_exhausted"):
        warnings.append(
            f"budget exhausted after {metrics.get('categories_completed', [])} categories and "
            f"{metrics.get('filing_types_completed', [])} filing types"
        )
    if metrics.get("efts_failures"):
        warnings.append(
            f"efts failures={metrics.get('efts_failures')} retries={metrics.get('retries_attempted', 0)}"
        )
    if metrics.get("merger_suppressed_total"):
        warnings.append(
            f"suppressed {metrics['merger_suppressed_total']} activist 8-K hit(s) via merger-sibling defense"
        )
    if metrics.get("issuer_filtered_total"):
        warnings.append(
            f"issuer filter dropped {metrics['issuer_filtered_total']} candidate hit(s)"
        )
    if metrics.get("market_cap_filtered_total"):
        warnings.append(
            f"market cap filter dropped {metrics['market_cap_filtered_total']} candidate hit(s)"
        )
    if not warnings and partial_reasons:
        warnings.extend([f"partial reason: {reason}" for reason in partial_reasons])
    return warnings


# ---------------------------------------------------------------------------
# scan entrypoint
# ---------------------------------------------------------------------------

def scan(cfg: ScannerConfig) -> ScannerResult:
    user_agent = os.environ.get("SEC_USER_AGENT")
    if not user_agent:
        raise MissingAuthError(
            "SEC_USER_AGENT env var missing — SEC requires a valid contact email "
            "in the User-Agent header. Set via Modal secret `scanner-secrets`.")

    client = SupabaseClient()

    # Route openfigi cache reads/writes through Supabase Storage.
    from modal_workers.shared.openfigi_resolver import set_cache_backend
    set_cache_backend(*client.openfigi_cache_backend())

    days_back = int(cfg.config.get("days_back", 2))
    scan_date = datetime.now(timezone.utc)
    date_from = (scan_date - timedelta(days=days_back)).strftime("%Y-%m-%d")
    date_to = scan_date.strftime("%Y-%m-%d")
    coverage_mode = str(cfg.config.get("coverage_mode", DEFAULT_COVERAGE_MODE)).lower()
    if coverage_mode not in {"full", "rotation"}:
        coverage_mode = DEFAULT_COVERAGE_MODE
    categories, rotation_state = _rotation_state_for_mode(client, coverage_mode)

    dedup_log = _load_dedup(client)
    budget = max(10, cfg.timeout_soft_s - 5)  # leave headroom for final ops
    scan_start = time.time()
    reserve_budget_s = _filing_phase_reserve_s(budget)
    filing_types_requested = [form_type for form_types in SIGNAL_FILING_TYPES.values() for form_type in form_types]
    run_metrics = _new_run_metrics(
        budget_s=budget,
        coverage_mode=coverage_mode,
        categories_requested=categories,
        filing_types_requested=filing_types_requested,
    )
    signals: List[Signal] = []
    seen_adsh_keyword: set[str] = set()
    seen_adsh_filing: set[str] = set()
    hits_processed = 0
    dedup_updates: Dict[str, str] = {}

    # Candidate signals deferred until the post-scan market-cap pass. Holding
    # (signal, ticker, dedup_hash_or_none) lets us run all yfinance I/O after
    # the SEC-rate-limited loops finish, so budget exhaustion in those loops
    # stays a pure function of SEC work.
    candidates: List[Tuple[Signal, Optional[str], Optional[str]]] = []

    activist_merger_suppression = bool(
        cfg.config.get("activist_merger_sibling_suppression", True))
    # Merger-sibling caches — two-tier (see `_has_merger_sibling` docstring):
    #   - Persistent (cross-run) cache stores ONLY True answers. True is
    #     monotonic so caching forever is correct. False is NOT monotonic
    #     (a companion proxy can land days after the 8-K), so we re-query.
    #   - Session cache is ephemeral (this run only) and dedupes repeat
    #     False-answer lookups for the same (cik, file_date).
    merger_sibling_cache: Dict[str, bool] = _load_merger_sibling_cache(client)
    merger_sibling_session_cache: Dict[str, bool] = {}
    merger_sibling_dirty: List[bool] = [False]
    issuer_filter_enabled = bool(cfg.config.get("issuer_filter_enabled", True))
    issuer_filter = _resolve_issuer_filter(cfg) if issuer_filter_enabled else _empty_issuer_filter()
    market_cap_floor_usd_mm = float(cfg.config.get("market_cap_floor_usd_mm", DEFAULT_MARKET_CAP_FLOOR_USD_MM))
    market_cap_filter_enabled = bool(
        cfg.config.get("market_cap_filter_enabled", market_cap_floor_usd_mm > 0)
    )
    run_metrics["market_cap_filter_enabled"] = market_cap_filter_enabled
    run_metrics["market_cap_floor_usd_mm"] = market_cap_floor_usd_mm

    # Persistent CIK → tickers/exchange cache (Supabase Storage, 7d per-entry
    # TTL). Pre-2026-04-22 this was session-only and burned ~50s/run of
    # data.sec.gov rate-limited lookups for issuers we had just seen.
    company_context_cache: Dict[str, Dict[str, Any]] = _load_persistent_ticker_cache(client)
    company_context_dirty: List[bool] = [False]
    figi_cache: Dict[str, Optional[str]] = {}
    market_cap_cache: Dict[str, Optional[float]] = {}

    # --- Keyword scan ---
    keyword_budget_exhausted = False
    for cat_idx, category in enumerate(categories):
        category_complete = True
        for kw_idx, keyword in enumerate(SIGNAL_KEYWORDS.get(category, [])):
            if not _has_budget_for_query(scan_start, budget, reserve_budget_s):
                remaining_queries = (len(SIGNAL_KEYWORDS[category]) - kw_idx) + sum(
                    len(SIGNAL_KEYWORDS.get(rest_category, []))
                    for rest_category in categories[cat_idx + 1:]
                )
                run_metrics["budget_exhausted"] = True
                run_metrics["skipped_due_to_budget"] += remaining_queries
                _mark_partial(run_metrics, "budget_exhausted_keyword_phase")
                keyword_budget_exhausted = True
                category_complete = False
                break

            run_metrics["keyword_queries_attempted"] += 1
            hits = _efts_search(
                f'"{keyword}"',
                date_from,
                date_to,
                max_results=30,
                user_agent=user_agent,
                metrics=run_metrics,
            )
            for hit in hits:
                hits_processed += 1
                adsh = hit.get("adsh", "")
                cik = hit.get("cik", "")
                dedup_key = f"{adsh}|{keyword}"
                if dedup_key in seen_adsh_keyword:
                    run_metrics["dedup_skipped"] += 1
                    continue
                seen_adsh_keyword.add(dedup_key)

                form = hit.get("form", "").strip()
                if form in KEYWORD_SKIP_FORMS:
                    continue
                if any(form.upper().startswith(bl) for bl in SPAC_IPO_FORM_BLACKLIST):
                    continue
                if category in CATEGORY_FORM_WHITELIST:
                    whitelist = CATEGORY_FORM_WHITELIST[category]
                    if not any(form.upper().startswith(wl) for wl in whitelist):
                        continue

                def _resolve_ticker() -> Optional[str]:
                    tickers, _ = _load_company_context(
                        cik,
                        user_agent=user_agent,
                        cache=company_context_cache,
                        dirty=company_context_dirty,
                    )
                    return tickers[0].upper() if tickers else None

                blocked, filter_reason, maybe_ticker = _is_spac_or_shell_issuer(
                    hit,
                    cik=cik,
                    issuer_filter=issuer_filter,
                    ticker_resolver=_resolve_ticker,
                )
                if blocked:
                    _record_issuer_filtered(
                        run_metrics,
                        hit,
                        filter_reason or "issuer_filter",
                        ticker=maybe_ticker,
                        cik=cik,
                    )
                    continue

                signal_type = _CATEGORY_SIGNAL_TYPE.get(category, f"{category}_keyword")
                if not _is_novel(cik, keyword, signal_type, dedup_log):
                    run_metrics["dedup_skipped"] += 1
                    continue

                if (activist_merger_suppression
                        and category == "activist"
                        and form.upper().startswith("8-K")):
                    if _has_merger_sibling(cik, hit.get("file_date", ""),
                                           user_agent=user_agent,
                                           cache=merger_sibling_cache,
                                           session_cache=merger_sibling_session_cache,
                                           dirty=merger_sibling_dirty):
                        run_metrics["merger_suppressed_total"] += 1
                        continue

                tickers, exchange = _load_company_context(
                    cik,
                    user_agent=user_agent,
                    cache=company_context_cache,
                    dirty=company_context_dirty,
                )
                ticker = maybe_ticker or (tickers[0].upper() if tickers else None)

                # Market-cap resolution + filter is deferred to the post-scan
                # pass below — see the candidates loop after the filing phase.
                issuer_figi = _resolve_figi_for_ticker(ticker, figi_cache)
                sig = _build_signal(
                    hit,
                    matched_keyword=keyword,
                    signal_type=signal_type,
                    thesis_direction=_resolve_thesis_direction(category, keyword),
                    strength_estimate=_compute_strength(category, keyword, form),
                    scan_date=scan_date,
                    tickers=tickers,
                    exchange=exchange,
                    market_cap_usd_mm=None,
                    issuer_figi=issuer_figi,
                )
                if sig is None:
                    continue
                candidates.append(
                    (sig, ticker, _signal_hash(cik, keyword, signal_type))
                )

        if category_complete:
            run_metrics["categories_completed"].append(category)
        if keyword_budget_exhausted:
            break

    # --- Filing type scan (SC 13D, NT 10-K variants) ---
    filing_plan = [
        (signal_type_key, form_type)
        for signal_type_key, form_types in SIGNAL_FILING_TYPES.items()
        for form_type in form_types
    ]
    for plan_idx, (signal_type_key, form_type) in enumerate(filing_plan):
        if not _has_budget_for_query(scan_start, budget):
            remaining_queries = len(filing_plan) - plan_idx
            run_metrics["budget_exhausted"] = True
            run_metrics["skipped_due_to_budget"] += remaining_queries
            _mark_partial(run_metrics, "budget_exhausted_filing_phase")
            break

        run_metrics["filing_queries_attempted"] += 1
        hits = _efts_search(
            "*",
            date_from,
            date_to,
            form_type=form_type,
            max_results=50,
            user_agent=user_agent,
            metrics=run_metrics,
        )
        for hit in hits:
            hits_processed += 1
            adsh = hit.get("adsh", "")
            if adsh in seen_adsh_filing:
                run_metrics["dedup_skipped"] += 1
                continue
            seen_adsh_filing.add(adsh)

            cik = hit.get("cik", "")

            def _resolve_ticker() -> Optional[str]:
                tickers, _ = _load_company_context(
                    cik,
                    user_agent=user_agent,
                    cache=company_context_cache,
                    dirty=company_context_dirty,
                )
                return tickers[0].upper() if tickers else None

            blocked, filter_reason, maybe_ticker = _is_spac_or_shell_issuer(
                hit,
                cik=cik,
                issuer_filter=issuer_filter,
                ticker_resolver=_resolve_ticker,
            )
            if blocked:
                _record_issuer_filtered(
                    run_metrics,
                    hit,
                    filter_reason or "issuer_filter",
                    ticker=maybe_ticker,
                    cik=cik,
                )
                continue

            tickers, exchange = _load_company_context(
                cik,
                user_agent=user_agent,
                cache=company_context_cache,
                dirty=company_context_dirty,
            )
            ticker = maybe_ticker or (tickers[0].upper() if tickers else None)

            # Market-cap resolution + filter is deferred to the post-scan
            # pass below — see the candidates loop after this block.
            issuer_figi = _resolve_figi_for_ticker(ticker, figi_cache)
            strength_estimate = 4 if "13D" in form_type else 3 if "NT 10" in form_type else 2
            sig = _build_signal(
                hit,
                matched_keyword=form_type,
                signal_type=signal_type_key,
                thesis_direction=_FILING_TYPE_DIRECTION.get(signal_type_key),
                strength_estimate=strength_estimate,
                scan_date=scan_date,
                tickers=tickers,
                exchange=exchange,
                market_cap_usd_mm=None,
                issuer_figi=issuer_figi,
            )
            if sig is None:
                continue
            # Filing-type hits don't track cross-run dedup (adsh is unique
            # per filing so `seen_adsh_filing` suffices). Pass None as the
            # dedup hash so the post-scan pass won't write a dedup row.
            candidates.append((sig, ticker, None))
        run_metrics["filing_types_completed"].append(form_type)

    # --- Post-scan market-cap filter pass ---
    # yfinance lookups (unthrottled but 0.5–2s each cold) happen here, after
    # the SEC rate-limited loops have released the budget. Resolution is
    # parallelized via ThreadPoolExecutor with a soft wall-clock cap so a
    # stuck yfinance call can't blow Modal's 120s wall timeout. The filter
    # logic (floor check, unknown bookkeeping, survivor commit) stays serial
    # and unchanged — only the resolution step is parallel.
    if market_cap_filter_enabled and candidates:
        cap_by_idx, cap_budget_exhausted = _resolve_market_caps_parallel(
            candidates, client, market_cap_cache,
        )
        if cap_budget_exhausted:
            _mark_partial(run_metrics, "market_cap_budget_exhausted")
    else:
        cap_by_idx = {i: None for i in range(len(candidates))}

    for i, (candidate_sig, candidate_ticker, candidate_dedup_hash) in enumerate(candidates):
        market_cap_usd_mm: Optional[float] = cap_by_idx.get(i)
        if candidate_ticker and market_cap_filter_enabled:
            if market_cap_usd_mm is None:
                run_metrics["market_cap_unknown_total"] += 1
            elif 0 < market_cap_usd_mm < market_cap_floor_usd_mm:
                _record_market_cap_filtered(
                    run_metrics,
                    ticker=candidate_ticker,
                    market_cap_usd_mm=market_cap_usd_mm,
                )
                continue
        candidate_sig.raw_payload["market_cap_usd_mm"] = market_cap_usd_mm
        signals.append(candidate_sig)
        if candidate_dedup_hash is not None:
            dedup_updates[candidate_dedup_hash] = date_to

    run_metrics["fetched_records"] = hits_processed
    run_metrics["signals_detected"] = len(signals)
    run_metrics["budget_remaining_seconds"] = round(_remaining_budget_s(scan_start, budget), 2)
    run_metrics["degraded"] = bool(
        run_metrics.get("partial_reasons")
        or run_metrics.get("efts_failures")
        or run_metrics.get("budget_exhausted")
    )

    warnings = _metric_warnings(run_metrics)

    def _persist_after_insert() -> None:
        if dedup_updates:
            updated = dict(dedup_log)
            updated.update(dedup_updates)
            _save_dedup(client, updated)
        if rotation_state is not None:
            _save_rotation(client, rotation_state)
        # Only re-save the persistent caches if we actually added/refreshed
        # entries this run — avoids unnecessary Storage writes when every
        # lookup hit a fresh cache entry.
        if company_context_dirty[0]:
            _save_persistent_ticker_cache(client, company_context_cache)
        if merger_sibling_dirty[0]:
            _save_merger_sibling_cache(client, merger_sibling_cache)

    return ScannerResult(
        scanner="edgar_filing_monitor",
        status="partial" if run_metrics["degraded"] else "ok",
        signals=signals,
        warnings=warnings,
        fetched_records=hits_processed,
        run_metrics=run_metrics,
        after_insert=_persist_after_insert,
    )
