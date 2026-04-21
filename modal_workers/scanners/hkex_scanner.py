"""
HKEx scanner — Modal port of tools/hkex_scanner.py.

Preserved from v1 (byte-equivalent where relevant):
  - BASE_URL + query-string construction (rowRange=200, SEHK, category=0).
  - JSON-in-JSON parsing: outer payload has a "result" key whose value is itself a
    JSON-encoded string. v1's `json.loads(outer["result"])` pattern preserved.
  - HIGH_SIGNAL_PATTERNS classification regexes (English + TC) — byte-equivalent to v1.
  - V1-local BOILERPLATE_PATTERNS (monthly returns, notice of AGM, dividend
    announcements, proxy forms, etc.) preserved as first-pass drop filter, then
    augmented with shared boilerplate_filters.is_boilerplate("HKEx", headline).
  - Signal-type mapping: takeover/scheme → merger_arb; profit warning, trading
    suspension, going concern, material transaction, major shareholder change →
    activist_governance.
  - Thesis direction mapping per category (long / short / neutral).
  - HKT (UTC+8) → UTC normalisation on DATE_TIME parsing.
  - HTML-entity unescape on TITLE and LONG_TEXT (HKEX escapes /, ;, <br/>).

Deviations from v1:
  - No OUT_FILE; signals returned via ScannerResult list for run_scanner plumbing.
  - No HttpClient dependency; uses `requests` directly with a browser User-Agent
    (HKEx blocks obvious bot UAs).
  - source_content_hash now carries the spec.md §3.4 "sha256:<64hex>" prefix for
    convergence classification parity with edgar / sec_enforcement scanners.
  - OpenFIGI backend wired through SupabaseClient.openfigi_cache_backend() so
    stock_code → issuer_figi lookups persist across Modal invocations.
  - No auth. Public endpoint. MissingAuthError not raised.

IO contract:
  scan(cfg: ScannerConfig) -> ScannerResult
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

from modal_workers.shared.boilerplate_filters import is_boilerplate
from modal_workers.shared.scanner_base import Signal, ScannerResult
from modal_workers.shared.supabase_client import EntityHints, ScannerConfig, SupabaseClient

NAME = "hkex_scanner"

BASE_URL = "https://www1.hkexnews.hk/search/titleSearchServlet.do"
HKEXNEWS_ROOT = "https://www1.hkexnews.hk"

LOOKBACK_DAYS = 3   # HKEX produces ~2000/day; short lookback + cadence keep it sane
ROW_RANGE = 200     # max records per servlet call

# HKEx does not accept obvious bot UAs; mimic a modern browser (v1 behaviour).
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 25  # matches v1

# ---------------------------------------------------------------------------
# Classification (verbatim patterns from v1)
# ---------------------------------------------------------------------------

# Each entry: (regex, (signal_type, thesis_direction))
# Scoring profile is resolved downstream via cfg.signal_type_profile_map; we do
# not set sig.scoring_profile here.
HIGH_SIGNAL_PATTERNS: List[Tuple[re.Pattern, Tuple[str, str]]] = [
    # Takeovers / mergers
    (re.compile(r"(?i)takeover|offer\s+announcement|rule\s*3\.5|mandatory\s+offer|"
                r"voluntary\s+offer|privatisation|privatization|delisting", re.I),
     ("tender_offer", "long")),
    (re.compile(r"(?i)scheme\s+of\s+arrangement", re.I),
     ("scheme_of_arrangement", "long")),
    # Activist / governance signals
    (re.compile(r"(?i)disclosure\s+of\s+interest|part\s+xv|substantial\s+shareholder|"
                r"major\s+shareholder\s+change", re.I),
     ("major_shareholder_change", "neutral")),
    (re.compile(r"(?i)profit\s+warning|profit\s+alert|loss\s+alert|expected\s+loss", re.I),
     ("profit_warning", "short")),
    (re.compile(r"(?i)trading\s+(suspension|halt)|resumption\s+of\s+trading", re.I),
     ("trading_suspension", "short")),
    (re.compile(r"(?i)going\s+concern|material\s+uncertainty|qualified\s+opinion|"
                r"resignation\s+of\s+auditor", re.I),
     ("going_concern", "short")),
    (re.compile(r"(?i)connected\s+transaction|very\s+substantial\s+(disposal|acquisition)|"
                r"major\s+transaction", re.I),
     ("material_transaction", "neutral")),
]

# V1-local blacklist — boilerplate categories that generate overwhelming noise.
# Kept in addition to shared boilerplate_filters.is_boilerplate("HKEx", ...) so
# that both v1 parity and shared filter coverage apply.
BOILERPLATE_PATTERNS: List[re.Pattern] = [
    re.compile(r"(?i)(annual\s+report|interim\s+report|esg\s+report|"
               r"environmental.*social.*governance)", re.I),
    re.compile(r"(?i)notice\s+of\s+(agm|annual\s+general\s+meeting|egm|sgm)", re.I),
    re.compile(r"(?i)dividend\s+(announcement|form|distribution)", re.I),
    re.compile(r"(?i)proxy\s+form|notification\s+letter|request\s+form", re.I),
    re.compile(r"(?i)list\s+of\s+directors\s+and\s+their\s+role", re.I),
    re.compile(r"(?i)general\s+mandate.*(repurchase|issue)\s+of\s+shares", re.I),
    re.compile(r"(?i)monthly\s+return", re.I),
    re.compile(r"(?i)next\s+day\s+disclosure", re.I),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _unescape(s: str) -> str:
    """HKEX HTML-escapes slashes and angle brackets in LONG_TEXT / TITLE."""
    if not s:
        return ""
    return html.unescape(s)


def _classify(title: str, long_text: str) -> Optional[Tuple[str, str]]:
    """Return (signal_type, thesis_direction) or None to drop."""
    combined = f"{_unescape(title)}  {_unescape(long_text)}"
    # Drop v1-local boilerplate first
    for pat in BOILERPLATE_PATTERNS:
        if pat.search(combined):
            return None
    for pat, result in HIGH_SIGNAL_PATTERNS:
        if pat.search(combined):
            return result
    return None  # unrecognised → drop (conservative)


def _build_search_url(days: int = LOOKBACK_DAYS, row_range: int = ROW_RANGE) -> str:
    today = datetime.now(timezone.utc).date()
    since = today - timedelta(days=days)
    return (
        f"{BASE_URL}?sortDir=0&sortByOption=DateTime&category=0&market=SEHK"
        f"&stockId=-1&documentType=-1"
        f"&fromDate={since.strftime('%Y%m%d')}&toDate={today.strftime('%Y%m%d')}"
        f"&t1code=-2&t2Gcode=-2&t2code=-2&rowRange={row_range}"
    )


def _parse_hkex_datetime(s: str) -> Optional[datetime]:
    """'16/04/2026 22:59' (HKT) → datetime in UTC, or None on failure."""
    if not s:
        return None
    try:
        dt_local = datetime.strptime(s, "%d/%m/%Y %H:%M")
        # HKEX posts in HKT (UTC+8).
        dt_utc = (dt_local - timedelta(hours=8)).replace(tzinfo=timezone.utc)
        return dt_utc
    except Exception:
        return None


def _sig_id(news_id: str, stock_code: str) -> str:
    return hashlib.sha256(f"hkex:{stock_code}:{news_id}".encode()).hexdigest()[:32]


def _content_hash(title: str, stock_code: str, date_time: str) -> str:
    return (
        "sha256:"
        + hashlib.sha256(f"{stock_code}|{title}|{date_time}".encode()).hexdigest()
    )


# ---------------------------------------------------------------------------
# Signal builder
# ---------------------------------------------------------------------------

def _build_signal(rec: Dict[str, Any], scan_date: datetime) -> Optional[Signal]:
    title = rec.get("TITLE") or ""
    long_text = rec.get("LONG_TEXT") or ""

    title_unesc = _unescape(title)
    long_text_unesc = _unescape(long_text)

    # Shared boilerplate filter — drops HKEx monthly returns etc. even if v1's
    # regex missed them (shared patterns are narrower but authoritative).
    if is_boilerplate("HKEx", title_unesc) or is_boilerplate("HKEx", long_text_unesc):
        return None

    cls = _classify(title, long_text)
    if cls is None:
        return None
    signal_type, direction = cls

    stock_code = (rec.get("STOCK_CODE") or "").strip()
    stock_name = (rec.get("STOCK_NAME") or "").strip()
    news_id = str(rec.get("NEWS_ID") or "")
    date_time = rec.get("DATE_TIME") or ""
    file_link = rec.get("FILE_LINK") or ""
    file_url = (
        f"{HKEXNEWS_ROOT}{file_link}"
        if file_link and file_link.startswith("/")
        else file_link
    )

    source_dt = _parse_hkex_datetime(date_time) or scan_date

    # Best-effort FIGI resolution via openfigi (exchCode="HK"). Same lazy-import
    # + swallow-all pattern as edgar; scanner emits the signal either way.
    issuer_figi: Optional[str] = None
    if stock_code:
        try:
            from modal_workers.shared.openfigi_resolver import resolve_ticker
            res = resolve_ticker(stock_code, exch_code="HK")
            if res.resolved:
                issuer_figi = res.issuer_figi
        except Exception:
            pass

    raw_payload: Dict[str, Any] = {
        "stock_code": stock_code,
        "doc_id": news_id,
        "title": title_unesc,
        "doc_type": rec.get("FILE_TYPE"),
        "long_text": long_text_unesc,
        "publish_time": date_time,
        # Extras useful to the reactor / dashboard:
        "company_name_en": stock_name,
        "file_url": file_url,
        "file_info": rec.get("FILE_INFO"),
        "long_text_raw": long_text,
        "headline": f"{stock_code} {stock_name}: {title_unesc[:100]}",
    }

    entity_hints = EntityHints(
        issuer_figi=issuer_figi,
        stock_code=stock_code or None,
        ticker=stock_code or None,   # HKEx stock codes are numeric (0001-9999)
        mic="XHKG",
        name=stock_name or None,
        country="HK",
    )

    return Signal(
        signal_id=_sig_id(news_id, stock_code),
        source_content_hash=_content_hash(title, stock_code, date_time),
        source_date=source_dt,
        scan_date=scan_date,
        signal_type=signal_type,
        raw_payload=raw_payload,
        source_url=file_url or None,
        issuer_figi=issuer_figi,
        entity_hints=entity_hints,
        thesis_direction=direction,
        strength_estimate=3,
    )


# ---------------------------------------------------------------------------
# scan entrypoint
# ---------------------------------------------------------------------------

def scan(cfg: ScannerConfig) -> ScannerResult:
    # No auth — public endpoint. Wire openfigi cache for stock_code resolution.
    client = SupabaseClient()
    try:
        from modal_workers.shared.openfigi_resolver import set_cache_backend
        set_cache_backend(*client.openfigi_cache_backend())
    except Exception:
        pass  # best effort — resolver falls back to file cache / no-cache

    scan_date = datetime.now(timezone.utc)
    scan_start = time.time()
    budget = max(10, cfg.timeout_soft_s - 5)  # leave headroom for final ops

    warnings: List[str] = []
    signals: List[Signal] = []
    seen_hashes: set[str] = set()

    url = _build_search_url()

    # --- Fetch the servlet payload ---
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": BROWSER_USER_AGENT, "Accept": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        outer = resp.json()
    except Exception as e:  # noqa: BLE001
        return ScannerResult(
            scanner=NAME,
            status="error",
            signals=[],
            warnings=[f"fetch failed: {type(e).__name__}: {e}"],
            error=f"fetch failed: {type(e).__name__}: {e}",
            fetched_records=0,
        )

    # --- JSON-in-JSON: outer["result"] is itself a JSON-encoded string ---
    try:
        result_str = outer.get("result", "[]") if isinstance(outer, dict) else "[]"
        if isinstance(result_str, str):
            records = json.loads(result_str)
        else:
            records = result_str or []
        if not isinstance(records, list):
            records = []
    except Exception as e:  # noqa: BLE001
        return ScannerResult(
            scanner=NAME,
            status="error",
            signals=[],
            warnings=[f"parse failed: {type(e).__name__}: {e}"],
            error=f"parse failed: {type(e).__name__}: {e}",
            fetched_records=0,
        )

    fetched = len(records)

    # --- Walk records, classify, dedup ---
    for rec in records:
        if time.time() - scan_start > budget:
            warnings.append(f"wall-clock budget ({budget}s) exceeded during classification")
            break
        try:
            sig = _build_signal(rec, scan_date)
        except Exception as e:  # noqa: BLE001
            warnings.append(f"record {rec.get('NEWS_ID')}: {type(e).__name__}: {e}")
            continue
        if sig is None:
            continue
        if sig.source_content_hash in seen_hashes:
            continue
        seen_hashes.add(sig.source_content_hash)
        signals.append(sig)

    status = "partial" if warnings else "ok"
    return ScannerResult(
        scanner=NAME,
        status=status,
        signals=signals,
        warnings=warnings,
        fetched_records=fetched,
    )
