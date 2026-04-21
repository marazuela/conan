"""
BSE/NSE scanner — Modal port of tools/bse_nse_scanner.py.

Data source: NSE India's public corporate-announcements API.
    https://www.nseindia.com/api/corporate-announcements?index=equities
        &from_date=DD-MM-YYYY&to_date=DD-MM-YYYY

The endpoint requires a cookie warmup on the NSE home page (anti-bot);
first GET the home URL with NSE_HEADERS_HOME to populate cookies, then
GET the API with NSE_HEADERS_API using the same Session. Typical yield
is ~100 tradeable signals over the 3-day lookback.

BSE (api.bseindia.com) returns a WAF interstitial for anonymous clients
and is intentionally skipped — NSE covers ~90% of equity volume in India.

Preserved from v1 (byte-equivalent where relevant):
  - NSE_HEADERS_HOME and NSE_HEADERS_API dicts.
  - HIGH_SIGNAL_PATTERNS classification table (14 regexes).
  - Local BOILERPLATE_PATTERNS (reg-30 press releases, shareholding
    patterns, loss-of-share-certificate, AGM/EGM notices, etc.).
  - IST (UTC+5:30) → UTC source_date normalisation via
    datetime.strptime(..., "%d-%b-%Y %H:%M:%S").
  - raw_payload mirrors v1 fields: symbol, desc, subject, attchmntText,
    exchdisstime, smIndustry.
  - 3-day lookback window.

Deviations from v1:
  - No OUT_FILE; signals returned via ScannerResult for run_scanner.
  - requests.Session replaces v1's HttpClient wrapper; cookie warmup
    uses the same session so anti-bot cookies flow into the API call.
  - source_content_hash now carries spec.md §3.4 "sha256:<64hex>" prefix.
  - Shared is_boilerplate("BSE_NSE", headline) layered on top of v1-local
    drop patterns.
  - v1 thesis direction "unknown" is normalised to "neutral" (matches the
    kind_scanner convention; downstream rubric distinguishes explicit neutral
    from missing direction for the contradiction-detection path).
  - OpenFIGI cache wired through SupabaseClient.openfigi_cache_backend()
    so NSE ticker → issuer_figi lookups persist across Modal invocations.
  - Cookie warmup retry: if the API call returns non-200, the session
    is re-warmed once; a second failure returns status='error'.

IO contract:
    scan(cfg: ScannerConfig) -> ScannerResult
"""

from __future__ import annotations

import hashlib
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

from modal_workers.shared.boilerplate_filters import is_boilerplate
from modal_workers.shared.scanner_base import Signal, ScannerResult
from modal_workers.shared.supabase_client import EntityHints, ScannerConfig, SupabaseClient

NAME = "bse_nse_scanner"

NSE_HOME = "https://www.nseindia.com/"
NSE_API = "https://www.nseindia.com/api/corporate-announcements"
LOOKBACK_DAYS = 3
REQUEST_TIMEOUT_HOME = 15  # seconds
REQUEST_TIMEOUT_API = 20

# ---------------------------------------------------------------------------
# Headers (verbatim from v1)
# ---------------------------------------------------------------------------

NSE_HEADERS_HOME = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
    "Accept-Language": "en-US,en;q=0.9",
}
NSE_HEADERS_API = {
    **NSE_HEADERS_HOME,
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-announcements",
    "X-Requested-With": "XMLHttpRequest",
}

# ---------------------------------------------------------------------------
# Classification (verbatim patterns from v1)
# ---------------------------------------------------------------------------

# Each entry: (regex, (signal_type, thesis_direction_raw))
# v1 used "unknown" for ambiguous amalgamation/merger; we translate "unknown"
# and "neutral" both to None in _normalise_direction so the Signal dataclass
# (which only accepts long/short/neutral) passes validation and the downstream
# rubric treats them as unspecified.
HIGH_SIGNAL_PATTERNS: List[Tuple[re.Pattern, Tuple[str, str]]] = [
    (re.compile(r"(?i)^\s*acquisition\s*$", re.I),
     ("acquisition", "long")),
    (re.compile(r"(?i)amalgamation|\bmerger\b", re.I),
     ("amalgamation_merger", "unknown")),
    (re.compile(r"(?i)scheme\s+of\s+arrangement", re.I),
     ("scheme_of_arrangement", "long")),
    (re.compile(r"(?i)open\s+offer|delisting", re.I),
     ("open_offer", "long")),
    (re.compile(r"(?i)buy[-\s]?back", re.I),
     ("buyback", "long")),
    (re.compile(r"(?i)sebi\s+takeover\s+regulations?", re.I),
     ("takeover_disclosure", "neutral")),
    (re.compile(r"(?i)substantial\s+acquisition|change\s+in\s+promoter", re.I),
     ("major_shareholder_change", "neutral")),
    (re.compile(r"(?i)change\s+in\s+auditors?|resignation\s+of\s+auditor", re.I),
     ("auditor_change", "short")),
    (re.compile(r"(?i)resignation\s+of\s+independent\s+director", re.I),
     ("independent_director_resignation", "short")),
    (re.compile(r"(?i)^\s*resignation\s*$", re.I),
     ("board_resignation", "neutral")),
    (re.compile(r"(?i)disclosure\s+of\s+material\s+issue", re.I),
     ("material_issue", "short")),
    (re.compile(r"(?i)suspension\s+of\s+trading", re.I),
     ("trading_suspension", "short")),
    (re.compile(r"(?i)pendency\s+of\s+litigation|outcome.*impacting", re.I),
     ("pending_litigation", "short")),
    (re.compile(r"(?i)profit\s+warning|loss\s+alert|impairment", re.I),
     ("profit_warning", "short")),
    (re.compile(r"(?i)shut[-\s]?down|plant.*closure|operations.*halted", re.I),
     ("operational_shock", "short")),
]

# V1-local drop list — applied to the `desc` field only (v1 behaviour). Kept
# alongside the shared boilerplate_filters.is_boilerplate("BSE_NSE", ...)
# call so both v1 parity and the shared regex set apply.
BOILERPLATE_PATTERNS: List[re.Pattern] = [
    re.compile(r"(?i)certificate\s+under\s+sebi.*depositor", re.I),
    re.compile(r"(?i)investor\s+(meet|presentation|con.*call)", re.I),
    re.compile(r"(?i)analysts?\s*/\s*institutional\s+investor", re.I),
    re.compile(r"(?i)press\s+release\b", re.I),
    re.compile(r"(?i)^\s*general\s+updates?\s*$", re.I),
    re.compile(r"(?i)^\s*updates?\s*$", re.I),
    re.compile(r"(?i)loss\s+of\s+share\s+certificate", re.I),
    re.compile(r"(?i)monitoring\s+agency\s+report", re.I),
    re.compile(r"(?i)shareholders\s+meeting", re.I),
    re.compile(r"(?i)^\s*capacity\s+addition\s*$", re.I),
    re.compile(r"(?i)notice\s+of\s+(agm|egm)", re.I),
    re.compile(r"(?i)dividend\s+(announcement|intimation)", re.I),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalise_direction(raw: str) -> Optional[str]:
    """Map v1 direction strings to the Signal Literal["long","short","neutral"].

    v1 used "unknown" for amalgamation/merger-style ambiguous cases. In v2 we
    surface that explicitly as "neutral" so downstream rubric dimensions can
    differentiate "scanner saw no directional bias" from "scanner didn't run"."""
    if raw in ("long", "short", "neutral"):
        return raw
    if raw == "unknown":
        return "neutral"
    return None


def _classify(desc: str, attch_text: str) -> Optional[Tuple[str, str]]:
    """Return (signal_type, raw_direction) or None to drop."""
    # v1: boilerplate check runs against `desc` only (not attchmntText), so
    # preserve that narrow scope to avoid over-dropping on verbose attachment
    # descriptions that happen to match generic patterns.
    for pat in BOILERPLATE_PATTERNS:
        if pat.search(desc or ""):
            return None
    combined = f"{desc or ''}  {attch_text or ''}"
    for pat, result in HIGH_SIGNAL_PATTERNS:
        if pat.search(combined):
            return result
    return None


def _parse_nse_datetime(s: str) -> Optional[datetime]:
    """'16-Apr-2026 22:59:03' (IST, UTC+5:30) → datetime in UTC, or None."""
    if not s:
        return None
    try:
        dt_local = datetime.strptime(s, "%d-%b-%Y %H:%M:%S")
        dt_utc = (dt_local - timedelta(hours=5, minutes=30)).replace(tzinfo=timezone.utc)
        return dt_utc
    except Exception:
        return None


def _sig_id(symbol: str, seq_id: str, an_dt: str) -> str:
    return hashlib.sha256(f"nse:{symbol}:{seq_id}:{an_dt}".encode()).hexdigest()[:32]


def _content_hash(symbol: str, desc: str, an_dt: str) -> str:
    return (
        "sha256:"
        + hashlib.sha256(f"{symbol}|{desc}|{an_dt}".encode()).hexdigest()
    )


# ---------------------------------------------------------------------------
# Fetch: cookie warmup + API call
# ---------------------------------------------------------------------------

def _warm_session(session: requests.Session) -> None:
    """GET the NSE home page to populate anti-bot cookies on the session.

    Raises on non-200 so callers can decide whether to retry or bail.
    """
    r = session.get(NSE_HOME, headers=NSE_HEADERS_HOME, timeout=REQUEST_TIMEOUT_HOME)
    r.raise_for_status()


def _build_api_url(days: int = LOOKBACK_DAYS) -> str:
    today = datetime.now(timezone.utc).date()
    since = today - timedelta(days=days)
    return (
        f"{NSE_API}?index=equities"
        f"&from_date={since.strftime('%d-%m-%Y')}"
        f"&to_date={today.strftime('%d-%m-%Y')}"
    )


def _fetch_records(session: requests.Session, url: str) -> Tuple[Optional[List[Dict[str, Any]]], str]:
    """Return (records, status_code_or_err). records is None on non-200."""
    try:
        r = session.get(url, headers=NSE_HEADERS_API, timeout=REQUEST_TIMEOUT_API)
    except Exception as e:  # noqa: BLE001
        return None, f"exception:{type(e).__name__}:{e}"
    if r.status_code != 200:
        return None, f"http:{r.status_code}"
    try:
        data = r.json()
    except ValueError as e:
        return None, f"json:{e}"
    if not isinstance(data, list):
        return None, f"shape:{type(data).__name__}"
    return data, "ok"


# ---------------------------------------------------------------------------
# Signal builder
# ---------------------------------------------------------------------------

def _build_signal(rec: Dict[str, Any], scan_date: datetime) -> Optional[Signal]:
    symbol = (rec.get("symbol") or "").strip()
    desc = (rec.get("desc") or "").strip()
    attch_text = (rec.get("attchmntText") or "").strip()
    subject = (rec.get("sm_name") or rec.get("subject") or "").strip()
    if not symbol or not desc:
        return None

    # Shared boilerplate (reg-30 press releases, shareholding patterns, etc.).
    # v1-local check inside _classify runs against desc only; the shared filter
    # checks the built headline (symbol + desc) to catch different wordings.
    headline_probe = f"{desc}"
    if is_boilerplate("BSE_NSE", headline_probe):
        return None

    cls = _classify(desc, attch_text)
    if cls is None:
        return None
    signal_type, raw_direction = cls
    direction = _normalise_direction(raw_direction)

    an_dt = rec.get("an_dt") or rec.get("exchdisstime") or ""
    seq_id = str(rec.get("seq_id") or "")
    company = (rec.get("sm_name") or "").strip()
    isin = (rec.get("sm_isin") or "").strip()
    attch_file = rec.get("attchmntFile") or ""
    industry = rec.get("smIndustry")

    source_dt = _parse_nse_datetime(an_dt) or scan_date

    # Best-effort FIGI resolution. v1 didn't do this (it emitted isin-only),
    # but the reactor's entity-resolver cascade benefits from having the FIGI
    # when available. Lazy import + swallow-all pattern matches edgar/hkex.
    issuer_figi: Optional[str] = None
    if symbol:
        try:
            from modal_workers.shared.openfigi_resolver import resolve_ticker
            res = resolve_ticker(symbol, exch_code="IN")
            if res.resolved:
                issuer_figi = res.issuer_figi
        except Exception:
            pass

    # raw_payload mirrors v1 field set plus the spec-required extras used by
    # the reactor + dashboard.
    raw_payload: Dict[str, Any] = {
        "symbol": symbol,
        "desc": desc,
        "subject": subject,
        "attchmntText": attch_text,
        "exchdisstime": an_dt,
        "smIndustry": industry,
        # Extras useful downstream:
        "isin": isin,
        "seq_id": seq_id,
        "attchmntFile": attch_file,
        "company_name_en": company,
        "file_size": rec.get("fileSize"),
        "headline": f"{symbol} {company}: {desc[:120]}",
        "summary": attch_text[:2000] if attch_text else desc,
    }

    entity_hints = EntityHints(
        issuer_figi=issuer_figi,
        ticker=symbol,
        mic="XNSE",
        isin=isin or None,
        name=company or None,
        country="IN",
    )

    return Signal(
        signal_id=_sig_id(symbol, seq_id, an_dt),
        source_content_hash=_content_hash(symbol, desc, an_dt),
        source_date=source_dt,
        scan_date=scan_date,
        signal_type=signal_type,
        raw_payload=raw_payload,
        source_url=attch_file or None,
        issuer_figi=issuer_figi,
        entity_hints=entity_hints,
        thesis_direction=direction,
        strength_estimate=3,
    )


# ---------------------------------------------------------------------------
# scan entrypoint
# ---------------------------------------------------------------------------

def scan(cfg: ScannerConfig) -> ScannerResult:
    # No auth — public endpoint gated only by cookie warmup. Wire openfigi
    # cache for NSE ticker resolution.
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

    session = requests.Session()
    url = _build_api_url(LOOKBACK_DAYS)

    # --- Cookie warmup (attempt #1) ---
    try:
        _warm_session(session)
    except Exception as e:  # noqa: BLE001
        return ScannerResult(
            scanner=NAME,
            status="error",
            signals=[],
            warnings=[f"home warmup failed: {type(e).__name__}: {e}"],
            error=f"home warmup failed: {type(e).__name__}: {e}",
            fetched_records=0,
        )

    # --- API call (attempt #1) ---
    records, status = _fetch_records(session, url)
    if records is None:
        # Re-warm once and retry — NSE sometimes drops cookies on the first API hit.
        warnings.append(f"api attempt #1 failed ({status}); re-warming session")
        try:
            session.cookies.clear()
            _warm_session(session)
        except Exception as e:  # noqa: BLE001
            return ScannerResult(
                scanner=NAME,
                status="error",
                signals=[],
                warnings=warnings + [f"re-warmup failed: {type(e).__name__}: {e}"],
                error=f"re-warmup failed: {type(e).__name__}: {e}",
                fetched_records=0,
            )
        records, status = _fetch_records(session, url)
        if records is None:
            return ScannerResult(
                scanner=NAME,
                status="error",
                signals=[],
                warnings=warnings + [f"api attempt #2 failed ({status})"],
                error=f"api attempt #2 failed ({status})",
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
            warnings.append(f"record {rec.get('seq_id')}: {type(e).__name__}: {e}")
            continue
        if sig is None:
            continue
        if sig.source_content_hash in seen_hashes:
            continue
        seen_hashes.add(sig.source_content_hash)
        signals.append(sig)

    status_final = "partial" if warnings else "ok"
    return ScannerResult(
        scanner=NAME,
        status=status_final,
        signals=signals,
        warnings=warnings,
        fetched_records=fetched,
    )
