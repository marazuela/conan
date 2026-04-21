"""
EDGAR Filing Monitor  (v2.4 — 2026-04-13)
==========================================
Scans SEC EDGAR full-text search (EFTS) for filings containing activist,
distress, M&A, and governance keywords. Produces standardized JSON signals
for the investment discovery pipeline.

Data Sources:
- EFTS: https://efts.sec.gov/LATEST/search-index (full-text search, free, no auth)
- Submissions: https://data.sec.gov/submissions/CIK{cik}.json (company metadata)
- Rate limit: 10 req/sec with User-Agent header (must include valid email)

Usage:
    python edgar_filing_monitor.py                    # Run full scan, output to signals/
    python edgar_filing_monitor.py --days 3            # Scan last 3 days
    python edgar_filing_monitor.py --category distress # Scan single category
    python edgar_filing_monitor.py --dry-run           # Print signals, don't save

API Response Fields (verified 2026-04-09):
    _source.ciks[]           — CIK numbers (array, use [0])
    _source.adsh             — accession number (e.g., "0001234567-26-000001")
    _source.display_names[]  — "Company Name  (CIK 0001234567)"
    _source.form             — form type (e.g., "10-K", "SC 13D")
    _source.file_date        — filing date "YYYY-MM-DD"
    _source.file_description — filing description
    _source.sics[]           — SIC codes
    No 'tickers' field — must resolve via data.sec.gov or OpenFIGI.
"""

import json
import os
import re
import sys
import time
import logging
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple

import requests

# Try to import from tools.mcap_cache; fall back to local import if running standalone
try:
    from tools.mcap_cache import get_market_cap_cached as _get_market_cap
except ImportError:
    from mcap_cache import get_market_cap_cached as _get_market_cap

try:
    from tools.profile_map import profile_for  # type: ignore
except ImportError:
    from profile_map import profile_for  # type: ignore

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

USER_AGENT = "InvestmentResearch research@example.com"  # SEC requires valid email
EFTS_URL = "https://efts.sec.gov/LATEST/search-index"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"

# Rate limiting
SEC_RATE_LIMIT = 8  # requests per second (conservative, SEC allows 10)
REQUEST_TIMEOUT = 10  # per-request timeout (reduced from 15 to prevent pipeline hangs)

# Runtime budget defaults. Can be overridden by pipeline_runner via
# UNIFIED_SOFT_TIMEOUT or manually via CLI.
WALL_CLOCK_BUDGET_S = 35
MIN_QUERY_BUDGET_S = 2.5
FILING_PHASE_RESERVE_S = 8.0

# EFTS retry policy for transient SEC flakiness.
MAX_EFTS_RETRIES = 2
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
MAX_EFTS_FAILURE_DETAILS = 10

# Market cap triage threshold
MARKET_CAP_FLOOR_MM = 215  # €200M ≈ $215M — minimum for liquidity

# Signal dedup window
DEDUP_WINDOW_DAYS = 45  # Reduced from 90 — filings are time-sensitive

# Output — default paths based on script location. Overridden by CLI main().
NAME = "edgar_filing_monitor"
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_DIR = os.path.dirname(_SCRIPT_DIR)
CONFIG_DIR = os.path.join(_PROJECT_DIR, "config")
SIGNALS_DIR = os.path.join(_PROJECT_DIR, "signals")
DEDUP_FILE = os.path.join(_PROJECT_DIR, "signals", "edgar_dedup.json")
ROTATION_FILE = os.path.join(_PROJECT_DIR, "signals", "edgar_rotation_state.json")
OUT_FILE = os.path.join(SIGNALS_DIR, f"{NAME}_scanner_output.json")
ISSUER_FILTER_FILE = os.path.join(CONFIG_DIR, "edgar_issuer_filter.json")
MAX_ISSUER_FILTER_SAMPLES = 10

# Rotation order (prioritized by signal value)
ROTATION_ORDER = ["activist", "mna", "distress", "governance"]

# Logging
logger = logging.getLogger("edgar_monitor")
_ISSUER_FILTER_CACHE: Optional[Dict[str, Any]] = None


def _resolve_budget_seconds(override: Optional[float] = None) -> float:
    """Resolve the wall-clock budget for this invocation.

    Precedence:
      1. explicit CLI/programmatic override
      2. pipeline_runner-provided UNIFIED_SOFT_TIMEOUT
      3. module default
    """
    if override is not None:
        try:
            return max(0.0, float(override))
        except Exception:
            return float(WALL_CLOCK_BUDGET_S)

    soft_timeout = os.environ.get("UNIFIED_SOFT_TIMEOUT")
    if soft_timeout:
        try:
            return max(0.0, float(soft_timeout))
        except Exception:
            pass
    return float(WALL_CLOCK_BUDGET_S)


def _remaining_budget_s(started_at: float, budget_s: float) -> float:
    if budget_s <= 0:
        return float("inf")
    return max(0.0, budget_s - (time.time() - started_at))


def _filing_phase_reserve_s(budget_s: float) -> float:
    if budget_s <= 0:
        return 0.0
    return min(FILING_PHASE_RESERVE_S, max(0.0, budget_s * 0.25))


def _has_budget_for_query(started_at: float, budget_s: float, reserve_s: float = 0.0) -> bool:
    if budget_s <= 0:
        return True
    return _remaining_budget_s(started_at, budget_s) > (reserve_s + MIN_QUERY_BUDGET_S)


def _new_run_metrics(budget_s: float) -> Dict[str, Any]:
    return {
        "budget_seconds": budget_s,
        "budget_exhausted": False,
        "efts_failures": 0,
        "retries_attempted": 0,
        "categories_scanned": [],
        "filing_types_scanned": [],
        "skipped_due_to_budget": 0,
        "partial": False,
        "partial_reasons": [],
        "efts_failure_details": [],
        "issuer_filtered_total": 0,
        "issuer_filtered_by_reason": {},
        "issuer_filter_samples": [],
    }


def _mark_partial(metrics: Optional[Dict[str, Any]], reason: str) -> None:
    if not metrics:
        return
    metrics["partial"] = True
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


def _record_efts_failure(metrics: Optional[Dict[str, Any]],
                         *,
                         query: str,
                         form_type: str,
                         status_code: Optional[int],
                         retries_attempted: int,
                         error: str,
                         retriable: bool) -> None:
    if not metrics:
        return
    metrics["efts_failures"] = metrics.get("efts_failures", 0) + 1
    if retriable:
        _mark_partial(metrics, "transient_efts_failure")
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


def _load_issuer_filter() -> Dict[str, Any]:
    global _ISSUER_FILTER_CACHE
    if _ISSUER_FILTER_CACHE is not None:
        return _ISSUER_FILTER_CACHE

    loaded: Dict[str, Any] = {
        "blocked_ciks": set(),
        "allowlist_ciks": set(),
        "allowlist_tickers": set(),
        "name_patterns_ci": [],
        "description_patterns_ci": [],
        "_name_regexes": [],
        "_description_regexes": [],
    }
    if not os.path.exists(ISSUER_FILTER_FILE):
        _ISSUER_FILTER_CACHE = loaded
        return loaded

    try:
        raw = json.loads(Path(ISSUER_FILTER_FILE).read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"Failed to load issuer filter config {ISSUER_FILTER_FILE}: {e}")
        _ISSUER_FILTER_CACHE = loaded
        return loaded

    if not isinstance(raw, dict):
        _ISSUER_FILTER_CACHE = loaded
        return loaded

    loaded["blocked_ciks"] = {str(cik).zfill(10) for cik in (raw.get("blocked_ciks") or []) if str(cik).strip()}
    loaded["allowlist_ciks"] = {str(cik).zfill(10) for cik in (raw.get("allowlist_ciks") or []) if str(cik).strip()}
    loaded["allowlist_tickers"] = {str(ticker).upper() for ticker in (raw.get("allowlist_tickers") or []) if str(ticker).strip()}
    loaded["name_patterns_ci"] = [str(pat) for pat in (raw.get("name_patterns_ci") or []) if str(pat).strip()]
    loaded["description_patterns_ci"] = [str(pat) for pat in (raw.get("description_patterns_ci") or []) if str(pat).strip()]

    for pattern in loaded["name_patterns_ci"]:
        try:
            loaded["_name_regexes"].append((pattern, re.compile(pattern, re.IGNORECASE)))
        except re.error as e:
            logger.warning(f"Invalid issuer filter name regex {pattern!r}: {e}")
    for pattern in loaded["description_patterns_ci"]:
        try:
            loaded["_description_regexes"].append((pattern, re.compile(pattern, re.IGNORECASE)))
        except re.error as e:
            logger.warning(f"Invalid issuer filter description regex {pattern!r}: {e}")

    _ISSUER_FILTER_CACHE = loaded
    return loaded


def _record_issuer_filtered(metrics: Optional[Dict[str, Any]],
                            hit: Dict[str, Any],
                            reason: str,
                            *,
                            ticker: Optional[str] = None,
                            cik: Optional[str] = None) -> None:
    if not metrics:
        return
    seen = metrics.setdefault("_issuer_filter_seen", set())
    dedup_key = (
        str(cik or hit.get("cik") or ""),
        str(hit.get("adsh") or ""),
        str(reason or ""),
    )
    if dedup_key in seen:
        return
    seen.add(dedup_key)
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


def _match_pattern(texts: List[str], regexes: List[Tuple[str, Any]]) -> Optional[str]:
    for label, regex in regexes:
        for text in texts:
            if text and regex.search(text):
                return label
    return None


def _is_spac_or_shell_issuer(hit: Dict[str, Any],
                             *,
                             ticker: Optional[str] = None,
                             cik: Optional[str] = None,
                             issuer_filter: Optional[Dict[str, Any]] = None) -> Tuple[bool, Optional[str], Optional[str]]:
    issuer_filter = issuer_filter or _load_issuer_filter()
    cik = str(cik or hit.get("cik") or "").zfill(10) if (cik or hit.get("cik")) else None
    ticker = (ticker or "").upper() or None

    if cik and cik in issuer_filter.get("allowlist_ciks", set()):
        return False, None, ticker
    if ticker and ticker in issuer_filter.get("allowlist_tickers", set()):
        return False, None, ticker
    if cik and cik in issuer_filter.get("blocked_ciks", set()):
        return True, "blocked_cik", ticker

    company_name = str(hit.get("company_name") or "")
    company_raw = str(hit.get("company_raw") or "")
    file_description = str(hit.get("file_description") or "")

    name_reason = _match_pattern([company_name, company_raw], issuer_filter.get("_name_regexes", []))
    desc_reason = _match_pattern([file_description], issuer_filter.get("_description_regexes", []))
    if not name_reason and not desc_reason:
        return False, None, ticker

    if not ticker and cik and issuer_filter.get("allowlist_tickers"):
        resolved_tickers, _ = _get_company_tickers(cik)
        if resolved_tickers:
            ticker = resolved_tickers[0] or None
            if ticker:
                ticker = ticker.upper()
            if ticker and ticker in issuer_filter.get("allowlist_tickers", set()):
                return False, None, ticker

    if name_reason:
        return True, f"name_pattern:{name_reason}", ticker
    if desc_reason:
        return True, f"description_pattern:{desc_reason}", ticker
    return False, None, ticker

# ---------------------------------------------------------------------------
# Keyword dictionaries
# ---------------------------------------------------------------------------

SIGNAL_KEYWORDS = {
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
}

# Filing types that are themselves signals
SIGNAL_FILING_TYPES = {
    "activist_ownership": ["SC 13D", "SC 13D/A"],
    "late_filings": ["NT 10-K", "NT 10-K/A", "NT 10-Q", "NT 10-Q/A"],
}

# Filing types to SKIP during keyword scanning — these contain boilerplate
# language that triggers false positive keyword matches (D-042: AMT ARS false positive)
KEYWORD_SKIP_FORMS = {
    "ARS",           # Annual Report to Shareholders — boilerplate governance language
    "DEF 14A",       # Definitive proxy (routine governance boilerplate)
    "DEFA14A",       # Additional definitive proxy soliciting material
    "DEFM14A",       # Definitive proxy for merger (handled by M&A filing types)
    "PRE 14A",       # Preliminary proxy
    "N-CSR",         # Certified shareholder report (fund, not corporate)
    "N-CSRS",        # Semi-annual fund shareholder report
    "497",           # Definitive materials (fund prospectus)
    "497K",          # Summary prospectus
    "NPORT-P",       # Monthly portfolio report
}

# Per-category form whitelists (Q-009, Q-010, D-030, D-031).
# If a category has an entry here, ONLY these form types are accepted.
# If a category is absent, all forms (minus KEYWORD_SKIP_FORMS) are accepted.
# This dramatically reduces SPAC/proxy-season noise.
CATEGORY_FORM_WHITELIST = {
    "distress": {"10-K", "10-K/A", "10-Q", "10-Q/A", "8-K", "8-K/A"},
    "activist": {"8-K", "8-K/A", "SC 13D", "SC 13D/A", "SC 14D9",
                 "PRER14A", "DFAN14A"},
    "mna": {"8-K", "8-K/A", "SC 13D", "SC 13D/A", "SC TO-T",
            "SC TO-T/A", "SC 13E3", "SC 13E3/A", "PREM14A"},
    "governance": {"8-K", "8-K/A", "10-K", "10-K/A", "10-Q", "10-Q/A"},
}

# Additional form blacklist for SPAC/de-SPAC/IPO noise (Q-010).
# These forms are excluded from ALL keyword categories.
SPAC_IPO_FORM_BLACKLIST = {
    "S-1", "S-1/A", "S-4", "S-4/A", "F-1", "F-1/A", "F-4", "F-4/A",
    "DRS", "DRS/A", "SB-2", "SB-2/A",
    "425",           # Prospectus communications (deal/SPAC flow)
    "SC TO-C",       # Tender offer commentary
    "SC TO-C/A",
    "424B3", "424B4", "424B5",  # Prospectus supplements
}


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

class _SECRateLimiter:
    """Ensures we don't exceed SEC's 10 req/sec limit."""
    def __init__(self, max_per_sec: int = SEC_RATE_LIMIT):
        self.max_per_sec = max_per_sec
        self._timestamps: List[float] = []

    def wait(self):
        now = time.time()
        self._timestamps = [t for t in self._timestamps if now - t < 1.0]
        if len(self._timestamps) >= self.max_per_sec:
            sleep_time = 1.0 - (now - self._timestamps[0]) + 0.05
            if sleep_time > 0:
                time.sleep(sleep_time)
        self._timestamps.append(time.time())


_rate_limiter = _SECRateLimiter()


# ---------------------------------------------------------------------------
# EFTS API functions
# ---------------------------------------------------------------------------

def _efts_search(query: str, date_from: str, date_to: str,
                 form_type: str = "", max_results: int = 50,
                 metrics: Optional[Dict[str, Any]] = None) -> List[dict]:
    """Query EDGAR EFTS and return raw hit sources.

    Returns list of dicts with keys: cik, adsh, company_raw, form, file_date,
    file_description, sics.
    """
    params = {
        "q": query,
        "dateRange": "custom",
        "startdt": date_from,
        "enddt": date_to,
    }
    if form_type:
        params["forms"] = form_type

    headers = {"User-Agent": USER_AGENT}

    attempt = 0
    while True:
        _rate_limiter.wait()
        try:
            resp = requests.get(EFTS_URL, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            break
        except requests.exceptions.RequestException as e:
            retriable = _is_retriable_efts_failure(e)
            status_code = _status_code_from_exc(e)
            if retriable and attempt < MAX_EFTS_RETRIES:
                attempt += 1
                if metrics is not None:
                    metrics["retries_attempted"] = metrics.get("retries_attempted", 0) + 1
                sleep_s = _retry_backoff_s(attempt)
                logger.warning(
                    "EFTS query retry %d/%d for query=%r form=%r status=%s after error: %s",
                    attempt,
                    MAX_EFTS_RETRIES,
                    query,
                    form_type or "*",
                    status_code,
                    e,
                )
                time.sleep(sleep_s)
                continue

            logger.error(
                "EFTS query failed for query=%r form=%r status=%s retries=%d: %s",
                query,
                form_type or "*",
                status_code,
                attempt,
                e,
            )
            _record_efts_failure(
                metrics,
                query=query,
                form_type=form_type,
                status_code=status_code,
                retries_attempted=attempt,
                error=str(e),
                retriable=retriable,
            )
            return []

    results = []
    for hit in data.get("hits", {}).get("hits", [])[:max_results]:
        src = hit.get("_source", {})
        cik = src.get("ciks", [""])[0] if src.get("ciks") else ""
        adsh = src.get("adsh", "")

        # Parse company name from display_names (format: "Company Name  (CIK 0001234567)")
        raw_name = src.get("display_names", [""])[0] if src.get("display_names") else ""
        company_name = re.sub(r"\s*\(CIK\s+\d+\)\s*$", "", raw_name).strip()

        # Build filing URL
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


def _get_company_tickers(cik: str) -> Tuple[List[str], Optional[str]]:
    """Look up tickers and exchange for a CIK via data.sec.gov submissions API.

    Returns (tickers_list, exchange_name).
    """
    if not cik:
        return [], None

    cik_padded = cik.zfill(10)
    url = SUBMISSIONS_URL.format(cik=cik_padded)
    headers = {"User-Agent": USER_AGENT}

    _rate_limiter.wait()
    try:
        resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            data = resp.json()
            tickers = data.get("tickers", [])
            exchanges = data.get("exchanges", [])
            exchange = exchanges[0] if exchanges else None
            return tickers, exchange
    except Exception as e:
        logger.debug(f"Failed to get tickers for CIK {cik}: {e}")

    return [], None


# ---------------------------------------------------------------------------
# Market cap triage
# ---------------------------------------------------------------------------

# DEPRECATED — use mcap_cache.get_market_cap_cached() instead (D-044)
# def _get_market_cap(ticker: str) -> Optional[float]:
#     """Get market cap in millions via yfinance. Returns None on failure."""
#     if not ticker:
#         return None
#     try:
#         import yfinance as yf
#         stock = yf.Ticker(ticker)
#         info = stock.info
#         mcap = info.get("marketCap")
#         if mcap:
#             return mcap / 1_000_000  # Convert to millions
#     except Exception as e:
#         logger.debug(f"yfinance market cap lookup failed for {ticker}: {e}")
#     return None


# ---------------------------------------------------------------------------
# Signal dedup
# ---------------------------------------------------------------------------

def _load_dedup_log(filepath: str) -> Dict[str, str]:
    """Load dedup log: maps signal_hash -> date_first_seen."""
    if filepath and os.path.exists(filepath):
        try:
            with open(filepath) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_dedup_log(filepath: str, log: dict):
    """Save dedup log."""
    if filepath:
        try:
            with open(filepath, "w") as f:
                json.dump(log, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save dedup log: {e}")


def _signal_hash(cik: str, keyword: str, category: str) -> str:
    """Deterministic hash for dedup: same company + same keyword + same category."""
    raw = f"{cik}|{keyword}|{category}"
    return hashlib.md5(raw.encode()).hexdigest()


def _is_novel(cik: str, keyword: str, category: str,
              dedup_log: dict, window_days: int = DEDUP_WINDOW_DAYS) -> bool:
    """Check if this signal is novel (not seen in the dedup window)."""
    h = _signal_hash(cik, keyword, category)
    if h in dedup_log:
        first_seen = dedup_log[h]
        try:
            first_date = datetime.strptime(first_seen, "%Y-%m-%d")
            if (datetime.now() - first_date).days < window_days:
                return False  # Not novel — seen recently
        except ValueError:
            pass
    return True


# ---------------------------------------------------------------------------
# Signal builder
# ---------------------------------------------------------------------------

def _build_signal(hit: dict, keyword: str, category: str,
                  ticker: str = "", market_cap_mm: float = 0) -> dict:
    """Build a signal dict in the common pipeline format."""
    # Estimate strength based on category and form type
    strength = 2  # default baseline
    form = hit.get("form", "")

    if category == "activist" and "13D" in form:
        strength = 4  # SC 13D is a strong activist signal
    elif category == "distress" and keyword in ("going concern", "substantial doubt"):
        strength = 4
    elif category == "mna":
        if keyword in ("definitive agreement", "merger agreement"):
            # 8-K with merger agreement = new deal announcement (high strength)
            # SC TO-T, PREM14A, S-4, SC TO-C, SC 13E3 = ongoing deal paperwork (lower)
            ongoing_forms = ("SC TO-T", "SC TO-C", "PREM14A", "DEFM14A",
                             "S-4", "SC 13E3", "SC TO-I", "SC TO-T/A",
                             "SC TO-C/A", "S-4/A", "SC 13E3/A", "SC TO-I/A",
                             "DFAN14A", "DEFA14A")
            if any(form.upper().startswith(f) for f in ongoing_forms):
                strength = 2  # ongoing deal filing — info already public
            else:
                strength = 5  # likely new announcement (8-K, etc.)
        elif keyword in ("tender offer",):
            # SC TO-T = initiating tender, SC TO-C = commenting on tender
            if "SC TO-T" in form.upper() and "/A" not in form.upper():
                strength = 4  # new tender offer filing
            else:
                strength = 2  # amendment or commentary — ongoing
        else:
            strength = 3  # other MNA keywords
    elif category == "governance" and keyword in ("poison pill", "rights plan"):
        strength = 3

    return {
        "ticker": ticker,
        "isin": None,
        "company_name": hit.get("company_name", ""),
        "market_cap_mm": round(market_cap_mm, 1) if market_cap_mm else None,
        "signal_type": f"{category}_keyword",
        "signal_category": "edgar",
        "strength_estimate": strength,
        "source_url": hit.get("filing_url", ""),
        "source_date": hit.get("file_date", ""),
        "scan_date": datetime.now().strftime("%Y-%m-%d"),
        "raw_data": {
            "keyword": keyword,
            "filing_type": form,
            "cik": hit.get("cik", ""),
            "adsh": hit.get("adsh", ""),
            "file_description": hit.get("file_description", ""),
            "company_raw": hit.get("company_raw", ""),
        },
    }


# ---------------------------------------------------------------------------
# Main scan functions
# ---------------------------------------------------------------------------

def _load_rotation_state() -> dict:
    """Load category rotation state from persistent file."""
    if os.path.exists(ROTATION_FILE):
        try:
            with open(ROTATION_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"rotation_index": -1, "scan_history": {}}


def _save_rotation_state(state: dict) -> None:
    """Save category rotation state to persistent file."""
    try:
        os.makedirs(os.path.dirname(ROTATION_FILE), exist_ok=True)
        with open(ROTATION_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        logger.warning(f"Failed to save rotation state: {e}")


def get_next_rotation_category() -> str:
    """Get the next keyword category to scan based on rotation state.

    Returns the next category in ROTATION_ORDER, advancing the index.
    If state is corrupted or missing, starts from "activist" (index 0).
    """
    state = _load_rotation_state()
    current_idx = state.get("rotation_index", -1)
    next_idx = (current_idx + 1) % len(ROTATION_ORDER)
    category = ROTATION_ORDER[next_idx]

    # Update state
    state["rotation_index"] = next_idx
    state["last_category"] = category
    state["last_scan_ts"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    if "scan_history" not in state:
        state["scan_history"] = {}
    state["scan_history"][category] = state["last_scan_ts"]
    _save_rotation_state(state)

    logger.info(f"Rotation: scanning category '{category}' (index {next_idx}/{len(ROTATION_ORDER)})")
    return category


def scan_keywords(categories: Optional[List[str]] = None,
                  days_back: int = 2,
                  market_cap_filter: bool = True,
                  budget_s: float = 0.0,
                  started_at: Optional[float] = None,
                  reserve_budget_s: float = 0.0,
                  metrics: Optional[Dict[str, Any]] = None) -> List[dict]:
    """Run keyword scan across specified categories.

    Args:
        categories: List of keyword categories to scan (default: all)
        days_back: How many days back to search
        market_cap_filter: Whether to apply $300M market cap floor

    Returns:
        List of signal dicts in common format
    """
    if categories is None:
        categories = list(SIGNAL_KEYWORDS.keys())

    date_from = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    date_to = datetime.now().strftime("%Y-%m-%d")

    # Load dedup log
    dedup_log = _load_dedup_log(DEDUP_FILE)
    issuer_filter = _load_issuer_filter()

    all_signals = []
    seen_adsh = set()  # Dedup within this scan
    scan_start = started_at if started_at is not None else time.time()
    budget_exceeded = False

    for cat_idx, category in enumerate(categories):
        if budget_exceeded:
            break
        if metrics is not None and category not in metrics["categories_scanned"]:
            metrics["categories_scanned"].append(category)
        keywords = SIGNAL_KEYWORDS.get(category, [])
        logger.info(f"Scanning category '{category}' ({len(keywords)} keywords)")

        for kw_idx, keyword in enumerate(keywords):
            # Check wall-clock budget before each keyword query
            if not _has_budget_for_query(scan_start, budget_s, reserve_budget_s):
                remaining_queries = (len(keywords) - kw_idx) + sum(
                    len(SIGNAL_KEYWORDS.get(rest_cat, [])) for rest_cat in categories[cat_idx + 1:]
                )
                if metrics is not None:
                    metrics["budget_exhausted"] = True
                    metrics["skipped_due_to_budget"] = metrics.get("skipped_due_to_budget", 0) + remaining_queries
                _mark_partial(metrics, "budget_exhausted_keyword_phase")
                logger.warning(
                    "Wall-clock budget (%.1fs) exhausted in keyword phase, remaining budget %.1fs, reserve %.1fs. "
                    "Stopping before query=%r. Processed %d hits so far.",
                    budget_s,
                    _remaining_budget_s(scan_start, budget_s),
                    reserve_budget_s,
                    keyword,
                    len(seen_adsh),
                )
                budget_exceeded = True
                break

            hits = _efts_search(
                query=f'"{keyword}"',
                date_from=date_from,
                date_to=date_to,
                max_results=30,
                metrics=metrics,
            )

            for hit in hits:
                adsh = hit.get("adsh", "")
                cik = hit.get("cik", "")

                # Skip duplicates within this scan
                dedup_key = f"{adsh}|{keyword}"
                if dedup_key in seen_adsh:
                    continue
                seen_adsh.add(dedup_key)

                # Skip boilerplate filing types (D-042: ARS, proxies, fund reports)
                form_type = hit.get("form", "").strip()
                if form_type in KEYWORD_SKIP_FORMS:
                    logger.debug(f"Skipping boilerplate form '{form_type}': "
                                 f"{hit.get('company_name')} / {keyword}")
                    continue

                # Skip SPAC/IPO forms (Q-010: de-SPAC noise filter)
                if any(form_type.upper().startswith(bl) for bl in SPAC_IPO_FORM_BLACKLIST):
                    logger.debug(f"Skipping SPAC/IPO form '{form_type}': "
                                 f"{hit.get('company_name')} / {keyword}")
                    continue

                # Per-category form whitelist (Q-009, D-030, D-031)
                if category in CATEGORY_FORM_WHITELIST:
                    whitelist = CATEGORY_FORM_WHITELIST[category]
                    if not any(form_type.upper().startswith(wl) for wl in whitelist):
                        logger.debug(f"Skipping non-whitelisted form '{form_type}' "
                                     f"for category '{category}': "
                                     f"{hit.get('company_name')} / {keyword}")
                        continue

                blocked, filter_reason, maybe_ticker = _is_spac_or_shell_issuer(
                    hit,
                    cik=cik,
                    issuer_filter=issuer_filter,
                )
                if blocked:
                    logger.debug(
                        "Skipping SPAC/shell issuer '%s' (CIK %s) for category '%s' keyword '%s' via %s",
                        hit.get("company_name"),
                        cik,
                        category,
                        keyword,
                        filter_reason,
                    )
                    _record_issuer_filtered(metrics, hit, filter_reason or "issuer_filter", ticker=maybe_ticker, cik=cik)
                    continue

                # Novelty check (90-day dedup)
                if not _is_novel(cik, keyword, category, dedup_log):
                    logger.debug(f"Skipping non-novel signal: {hit.get('company_name')} / {keyword}")
                    continue

                # Resolve ticker via data.sec.gov
                ticker = maybe_ticker or ""
                if not ticker:
                    tickers, exchange = _get_company_tickers(cik)
                    ticker = tickers[0] if tickers else ""

                # Market cap triage
                market_cap_mm = 0
                if ticker and market_cap_filter:
                    market_cap_mm = _get_market_cap(ticker) or 0
                    if market_cap_mm < MARKET_CAP_FLOOR_MM and market_cap_mm > 0:
                        logger.debug(f"Below market cap floor: {ticker} ${market_cap_mm:.0f}M < ${MARKET_CAP_FLOOR_MM}M")
                        continue
                    elif market_cap_mm == 0:
                        # Could not determine market cap — keep but flag
                        logger.debug(f"Market cap unknown for {ticker or cik}, keeping signal")

                signal = _build_signal(hit, keyword, category, ticker, market_cap_mm)
                all_signals.append(signal)

                # Update dedup log
                h = _signal_hash(cik, keyword, category)
                if h not in dedup_log:
                    dedup_log[h] = date_to

    # Save updated dedup log
    _save_dedup_log(DEDUP_FILE, dedup_log)

    logger.info(f"Keyword scan complete: {len(all_signals)} signals from {len(seen_adsh)} unique hits")
    return all_signals


def scan_filing_types(days_back: int = 2,
                      market_cap_filter: bool = True,
                      budget_s: float = 0.0,
                      started_at: Optional[float] = None,
                      metrics: Optional[Dict[str, Any]] = None) -> List[dict]:
    """Scan for specific filing types that are themselves signals (SC 13D, NT 10-K).

    Returns list of signal dicts in common format.
    """
    date_from = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    date_to = datetime.now().strftime("%Y-%m-%d")

    all_signals = []
    seen_adsh = set()
    scan_start = started_at if started_at is not None else time.time()
    issuer_filter = _load_issuer_filter()
    filing_plan = [(signal_type, form_type) for signal_type, form_types in SIGNAL_FILING_TYPES.items() for form_type in form_types]

    for plan_idx, (signal_type, form_type) in enumerate(filing_plan):
        if metrics is not None and form_type not in metrics["filing_types_scanned"]:
            metrics["filing_types_scanned"].append(form_type)
        if not _has_budget_for_query(scan_start, budget_s):
            remaining_queries = len(filing_plan) - plan_idx
            if metrics is not None:
                metrics["budget_exhausted"] = True
                metrics["skipped_due_to_budget"] = metrics.get("skipped_due_to_budget", 0) + remaining_queries
            _mark_partial(metrics, "budget_exhausted_filing_phase")
            logger.warning(
                "Wall-clock budget (%.1fs) exhausted in filing-type phase, remaining budget %.1fs. "
                "Stopping before form=%r.",
                budget_s,
                _remaining_budget_s(scan_start, budget_s),
                form_type,
            )
            break

        logger.info(f"Scanning filing type: {form_type}")
        hits = _efts_search(
            query="*",
            date_from=date_from,
            date_to=date_to,
            form_type=form_type,
            max_results=50,
            metrics=metrics,
        )

        for hit in hits:
            adsh = hit.get("adsh", "")
            if adsh in seen_adsh:
                continue
            seen_adsh.add(adsh)

            cik = hit.get("cik", "")
            blocked, filter_reason, maybe_ticker = _is_spac_or_shell_issuer(
                hit,
                cik=cik,
                issuer_filter=issuer_filter,
            )
            if blocked:
                logger.debug(
                    "Skipping SPAC/shell issuer '%s' (CIK %s) for filing form '%s' via %s",
                    hit.get("company_name"),
                    cik,
                    form_type,
                    filter_reason,
                )
                _record_issuer_filtered(metrics, hit, filter_reason or "issuer_filter", ticker=maybe_ticker, cik=cik)
                continue

            ticker = maybe_ticker or ""
            if not ticker:
                tickers, exchange = _get_company_tickers(cik)
                ticker = tickers[0] if tickers else ""

            market_cap_mm = 0
            if ticker and market_cap_filter:
                market_cap_mm = _get_market_cap(ticker) or 0
                if 0 < market_cap_mm < MARKET_CAP_FLOOR_MM:
                    continue

            signal = _build_signal(
                hit, keyword=form_type, category=signal_type,
                ticker=ticker, market_cap_mm=market_cap_mm,
            )
            # Boost strength for 13D filings
            if "13D" in form_type:
                signal["strength_estimate"] = max(signal["strength_estimate"], 4)
            elif "NT 10" in form_type:
                signal["strength_estimate"] = max(signal["strength_estimate"], 3)

            all_signals.append(signal)

    logger.info(f"Filing type scan complete: {len(all_signals)} signals")
    return all_signals


def run_full_scan(days_back: int = 2,
                  market_cap_filter: bool = True,
                  save_signals: bool = True,
                  budget_s: float = 0.0,
                  metrics: Optional[Dict[str, Any]] = None) -> List[dict]:
    """Run complete EDGAR scan: keywords + filing types.

    Args:
        days_back: How many days back to search
        market_cap_filter: Apply $300M market cap floor
        save_signals: Whether to write signals to signals/ directory

    Returns:
        List of all signals found
    """
    all_signals = []

    run_metrics = metrics if metrics is not None else _new_run_metrics(budget_s)
    scan_start = time.time()
    reserve_budget_s = _filing_phase_reserve_s(budget_s)

    # Keyword scan
    keyword_signals = scan_keywords(
        days_back=days_back,
        market_cap_filter=market_cap_filter,
        budget_s=budget_s,
        started_at=scan_start,
        reserve_budget_s=reserve_budget_s,
        metrics=run_metrics,
    )
    all_signals.extend(keyword_signals)

    # Filing type scan
    type_signals = scan_filing_types(
        days_back=days_back,
        market_cap_filter=market_cap_filter,
        budget_s=budget_s,
        started_at=scan_start,
        metrics=run_metrics,
    )
    all_signals.extend(type_signals)

    # Save signals to JSON
    if save_signals and SIGNALS_DIR and all_signals:
        os.makedirs(SIGNALS_DIR, exist_ok=True)
        output_file = os.path.join(
            SIGNALS_DIR,
            f"edgar_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        )
        with open(output_file, "w") as f:
            json.dump(all_signals, f, indent=2)
        logger.info(f"Saved {len(all_signals)} signals to {output_file}")

    return all_signals


def _signal_id(sig: dict) -> str:
    raw = sig.get("raw_data", {}) or {}
    adsh = raw.get("adsh") or sig.get("source_url") or sig.get("company_name") or ""
    keyword = raw.get("keyword") or raw.get("filing_type") or sig.get("signal_type") or ""
    source_date = sig.get("source_date") or ""
    seed = f"{adsh}|{sig.get('signal_type','')}|{keyword}|{source_date}"
    return hashlib.sha256(seed.encode()).hexdigest()[:32]


def _source_content_hash(sig: dict) -> str:
    raw = sig.get("raw_data", {}) or {}
    adsh = raw.get("adsh") or sig.get("source_url") or sig.get("company_name") or ""
    keyword = raw.get("keyword") or raw.get("filing_type") or sig.get("signal_type") or ""
    seed = f"{adsh}|{sig.get('signal_type','')}|{keyword}"
    return hashlib.sha256(seed.encode()).hexdigest()[:16]


def _normalize_signal_for_unified_envelope(sig: dict) -> dict:
    raw = dict(sig.get("raw_data", {}) or {})
    signal_type = sig.get("signal_type") or ""
    source_date = sig.get("source_date") or datetime.now().strftime("%Y-%m-%d")
    company = sig.get("company_name") or ""
    profile = profile_for(signal_type, NAME) or "activist_governance"

    return {
        "signal_id": _signal_id(sig),
        "source_content_hash": _source_content_hash(sig),
        "scanner_source": NAME,
        "upstream_scanner": NAME,
        "scoring_profile": profile,
        "ticker": sig.get("ticker") or None,
        "isin": sig.get("isin"),
        "company_name_en": company,
        "headline": raw.get("file_description") or f"{signal_type}: {raw.get('keyword') or company}",
        "summary": raw.get("file_description") or "",
        "signal_type": signal_type,
        "signal_category": sig.get("signal_category") or "edgar",
        "strength_estimate": sig.get("strength_estimate"),
        "source_url": sig.get("source_url") or "",
        "filing_url": sig.get("source_url") or "",
        "source_date": source_date,
        "scan_date": sig.get("scan_date") or datetime.now().strftime("%Y-%m-%d"),
        "market_cap_mm": sig.get("market_cap_mm"),
        "raw_data": raw,
    }


def _write_json_atomic(path: str, payload: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _write_legacy_snapshot(signals: List[dict]) -> Optional[str]:
    if not signals:
        return None
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_file = os.path.join(SIGNALS_DIR, f"edgar_{ts}.json")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(signals, f, indent=2, ensure_ascii=False)
    logger.info(f"Saved {len(signals)} signals to {out_file}")
    return out_file


def scan(days_back: int = 2,
         categories: Optional[List[str]] = None,
         market_cap_filter: bool = True,
         full_scan: bool = True,
         budget_s: Optional[float] = None) -> dict:
    """Wrapper that returns a unified scanner envelope."""
    resolved_budget_s = _resolve_budget_seconds(budget_s)
    metrics = _new_run_metrics(resolved_budget_s)
    try:
        if full_scan and categories is None:
            raw_signals = run_full_scan(
                days_back=days_back,
                market_cap_filter=market_cap_filter,
                save_signals=False,
                budget_s=resolved_budget_s,
                metrics=metrics,
            )
            mode = "full"
        else:
            raw_signals = scan_keywords(
                categories=categories,
                days_back=days_back,
                market_cap_filter=market_cap_filter,
                budget_s=resolved_budget_s,
                started_at=time.time(),
                reserve_budget_s=0.0,
                metrics=metrics,
            )
            mode = "keyword_only"
    except Exception as e:
        return {
            "scanner": NAME,
            "ran_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": "error",
            "signals": [],
            "error": f"{type(e).__name__}: {e}",
            "mode": "full" if full_scan and categories is None else "keyword_only",
            "budget_seconds": resolved_budget_s,
            "budget_exhausted": metrics["budget_exhausted"],
            "efts_failures": metrics["efts_failures"],
            "retries_attempted": metrics["retries_attempted"],
            "categories_scanned": metrics["categories_scanned"],
            "filing_types_scanned": metrics["filing_types_scanned"],
            "skipped_due_to_budget": metrics["skipped_due_to_budget"],
            "partial_reasons": metrics["partial_reasons"],
            "efts_failure_details": metrics["efts_failure_details"],
            "issuer_filtered_total": metrics["issuer_filtered_total"],
            "issuer_filtered_by_reason": metrics["issuer_filtered_by_reason"],
            "issuer_filter_samples": metrics["issuer_filter_samples"],
        }

    normalized = [_normalize_signal_for_unified_envelope(sig) for sig in raw_signals]
    status = "partial" if metrics.get("partial") else "ok"
    return {
        "scanner": NAME,
        "ran_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": status,
        "signals": normalized,
        "raw_signal_count": len(raw_signals),
        "unique_signals": len(normalized),
        "mode": mode,
        "window_days": days_back,
        "budget_seconds": resolved_budget_s,
        "budget_exhausted": metrics["budget_exhausted"],
        "efts_failures": metrics["efts_failures"],
        "retries_attempted": metrics["retries_attempted"],
        "categories_scanned": metrics["categories_scanned"],
        "filing_types_scanned": metrics["filing_types_scanned"],
        "skipped_due_to_budget": metrics["skipped_due_to_budget"],
        "partial_reasons": metrics["partial_reasons"],
        "efts_failure_details": metrics["efts_failure_details"],
        "issuer_filtered_total": metrics["issuer_filtered_total"],
        "issuer_filtered_by_reason": metrics["issuer_filtered_by_reason"],
        "issuer_filter_samples": metrics["issuer_filter_samples"],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    import argparse

    parser = argparse.ArgumentParser(description="EDGAR Filing Monitor")
    parser.add_argument("--days", type=int, default=2, help="Days to look back (default: 2)")
    parser.add_argument("--category", type=str, default=None,
                        help="Single category to scan (activist, distress, mna, governance)")
    parser.add_argument("--no-market-cap", action="store_true",
                        help="Disable market cap filtering")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print signals without saving")
    parser.add_argument("--rotate", action="store_true",
                        help="Use category rotation — scans next category in rotation order")
    parser.add_argument("--budget", type=float, default=None,
                        help="Override wall-clock budget in seconds (0 disables budget)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    # Set up paths relative to script location
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_dir = os.path.dirname(script_dir)
    signals_dir = os.path.join(project_dir, "signals")

    os.makedirs(SIGNALS_DIR, exist_ok=True)

    # Determine category
    if args.rotate:
        category = get_next_rotation_category()
        categories_to_scan = [category]
        full_scan = False
    elif args.category:
        categories_to_scan = [args.category]
        full_scan = False
    else:
        categories_to_scan = None
        full_scan = True

    result = scan(
        days_back=args.days,
        categories=categories_to_scan,
        market_cap_filter=not args.no_market_cap,
        full_scan=full_scan,
        budget_s=args.budget,
    )

    if not args.dry_run:
        _write_json_atomic(OUT_FILE, result)
        _write_legacy_snapshot(result["signals"])
    elif result.get("signals"):
        for sig in result["signals"]:
            print(json.dumps(sig, indent=2))

    print(json.dumps({
        "signals": len(result.get("signals", [])),
        "scanner": NAME,
        "status": result.get("status"),
        "mode": result.get("mode"),
        "budget_seconds": result.get("budget_seconds"),
        "budget_exhausted": result.get("budget_exhausted"),
        "efts_failures": result.get("efts_failures"),
        "retries_attempted": result.get("retries_attempted"),
        "issuer_filtered_total": result.get("issuer_filtered_total"),
        "output": None if args.dry_run else OUT_FILE,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()

# --- END OF FILE ---
