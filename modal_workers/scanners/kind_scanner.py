"""
KIND scanner -- Modal port of tools/kind_scanner.py.

Data source: OpenDART (Financial Supervisory Service of Korea)
  https://opendart.fss.or.kr/api/list.json
  20,000 req/day free quota. Requires crtfc_key env var (OPENDART_KEY).

KIND (kind.krx.co.kr) itself is client-side rendered and WAF-gated for
programmatic access; OpenDART is the programmatic equivalent covering
the same disclosure universe with official FSS filings. Registry key
`kind_scanner` / filename `kind_scanner.py` reflects the KIND portal name.

Preserved from v1 (byte-equivalent):
  - All 14 HIGH_SIGNAL_PATTERNS (Korean-language regex, ordered by specificity).
  - BOILERPLATE_PATTERNS (supplemental v1 drops; augments the shared
    `is_boilerplate("KIND", headline)` filter).
  - LOOKBACK_DAYS=3, PAGE_SIZE=100, max_pages=10 (1000 records per scan).
  - DART status handling: 000 ok, 013 no-data (empty list), else error.
  - raw_payload keys: corp_code, stock_code, corp_name, report_nm, rcept_no,
    rcept_dt, rm, flr_nm.

Deviations from v1:
  - No OUT_FILE / no __main__ block; signals returned via ScannerResult.
  - source_content_hash carries spec.md `sha256:<64hex>` prefix (v1 used a
    16-char truncated hex). Keys off rcept_no when present, else
    `corp_code|rcept_dt|report_nm` (v1 parity on the fallback).
  - Korean stock codes (6-digit) go into EntityHints.ticker AND stock_code
    so the entity cascade can resolve either way; MIC set to "XKRX".
  - Best-effort OpenFIGI resolution on the stock_code (some Korean codes
    don't resolve; scanner emits signal regardless).
  - rcept_dt (YYYYMMDD, KST date) parsed to 00:00 KST -> 15:00 UTC prior
    day equivalent (KST = UTC+9). v1 stamped it as 00:00 UTC same day;
    v2 corrects this so source_date reflects the actual KST midnight.
  - Boilerplate filter: combines shared `is_boilerplate("KIND", ...)` with
    v1's local BOILERPLATE_PATTERNS for full coverage.
  - requests directly (v1 used tools/http_client.HttpClient which isn't
    available in the Modal image).
  - Wall-clock budget guard on paginated fetch; bails with status="partial"
    if cfg.timeout_soft_s is exhausted mid-pagination.

IO contract:
  scan(cfg: ScannerConfig) -> ScannerResult
    - raises MissingAuthError if OPENDART_KEY env unset.
    - Uses cfg.timeout_soft_s as wall-clock budget for pagination.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

from modal_workers.shared.boilerplate_filters import is_boilerplate
from modal_workers.shared.scanner_base import MissingAuthError, Signal, ScannerResult
from modal_workers.shared.supabase_client import EntityHints, ScannerConfig, SupabaseClient

NAME = "kind_scanner"

# ---------------------------------------------------------------------------
# Constants (verbatim from v1)
# ---------------------------------------------------------------------------

DART_LIST_URL = "https://opendart.fss.or.kr/api/list.json"
LOOKBACK_DAYS = 3
PAGE_SIZE = 100
MAX_PAGES = 10  # cap at 1000 records per scan (v1 parity)
REQUEST_TIMEOUT = 15  # per-request seconds

KST = timezone(timedelta(hours=9))

# Korean title keyword patterns. Ordered by specificity (most specific first).
# BYTE-EQUIVALENT to v1 HIGH_SIGNAL_PATTERNS.
HIGH_SIGNAL_PATTERNS: List[Tuple[re.Pattern, Tuple[str, str, str]]] = [
    # Tender offers / takeovers (highest value)
    (re.compile(r"공개매수"),
     ("tender_offer", "merger_arb", "long")),
    (re.compile(r"합병(?!계약)"),  # "merger" but not "merger contract" which is routine
     ("merger_announcement", "merger_arb", "unknown")),
    (re.compile(r"분할합병|합병계약"),
     ("merger_contract", "merger_arb", "unknown")),
    (re.compile(r"경영권|지배주주.*변경"),
     ("control_change", "merger_arb", "long")),

    # Ownership disclosures
    (re.compile(r"주식등의대량보유|5%룰"),
     ("large_holding", "activist_governance", "neutral")),
    (re.compile(r"지분공시|임원.*주식소유"),
     ("ownership_disclosure", "activist_governance", "neutral")),

    # Governance red flags
    (re.compile(r"횡령|배임"),
     ("fraud_allegation", "activist_governance", "short")),
    (re.compile(r"감사의견.*(?:거절|한정|부적정)"),
     ("adverse_audit_opinion", "activist_governance", "short")),
    (re.compile(r"상장폐지|매매거래정지"),
     ("delisting_or_halt", "activist_governance", "short")),
    (re.compile(r"영업정지|영업중단"),
     ("operations_suspended", "activist_governance", "short")),

    # Capital / dilution
    (re.compile(r"유상증자(?:결정)?"),
     ("rights_issue", "activist_governance", "short")),
    (re.compile(r"전환사채.*발행|신주인수권부사채"),
     ("convertible_issuance", "activist_governance", "short")),

    # Litigation
    (re.compile(r"소송.*제기|소송.*판결"),
     ("litigation_filed", "litigation", "short")),

    # Profit warnings
    (re.compile(r"매출액.*감소|영업(?:손실|이익).*감소"),
     ("profit_warning", "activist_governance", "short")),
]

# Supplemental drop list from v1 (augments shared is_boilerplate("KIND", ...)).
# Shared list already covers "감사보고서 제출", "사업보고서 (일반)", "주주총회소집결의".
LOCAL_BOILERPLATE_PATTERNS: List[re.Pattern] = [
    re.compile(r"사업보고서|반기보고서|분기보고서"),  # annual/interim/quarterly report
    re.compile(r"감사보고서(?!.*거절)"),             # audit report (unless rejected)
    re.compile(r"주주총회소집"),                      # notice of shareholder meeting
    re.compile(r"배당.*공시|배당금지급"),             # dividend announcements
    re.compile(r"공시서류.*제출기한연장"),            # filing deadline extensions
    re.compile(r"증권발행실적보고서"),                # securities issuance report (routine)
    re.compile(r"자기주식.*취득결과"),                # buyback-completion reports (routine)
]


# Direction for thesis_direction (v1 emitted this in third tuple slot).
# v1 used "unknown" for merger_announcement / merger_contract -- map to
# "neutral" for the v2 Signal contract (Literal["long","short","neutral"]).
def _normalize_direction(d: str) -> str:
    return "neutral" if d == "unknown" else d


# ---------------------------------------------------------------------------
# Classification (verbatim from v1)
# ---------------------------------------------------------------------------

def _is_boilerplate_full(report_nm: str) -> bool:
    """Combined boilerplate check: shared KIND filter + v1 local patterns."""
    if is_boilerplate("KIND", report_nm):
        return True
    for pat in LOCAL_BOILERPLATE_PATTERNS:
        if pat.search(report_nm):
            return True
    return False


def _classify(report_nm: str) -> Optional[Tuple[str, str, str]]:
    if not report_nm:
        return None
    if _is_boilerplate_full(report_nm):
        return None
    for pat, result in HIGH_SIGNAL_PATTERNS:
        if pat.search(report_nm):
            return result
    return None


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------

def _parse_rcept_dt(s: str) -> Optional[datetime]:
    """'20260416' (KST date) -> UTC datetime at 00:00 KST (= 15:00 UTC prior day).

    DART only provides date-level precision for rcept_dt in the list endpoint.
    We interpret YYYYMMDD as midnight KST and convert to UTC.
    """
    if not s or len(s) != 8:
        return None
    try:
        dt_kst = datetime.strptime(s, "%Y%m%d").replace(tzinfo=KST)
        return dt_kst.astimezone(timezone.utc)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Fetch (v1 pagination preserved)
# ---------------------------------------------------------------------------

def _fetch_page(token: str, bgn_de: str, end_de: str, page_no: int) -> Tuple[Optional[Dict[str, Any]], str]:
    params = {
        "crtfc_key": token,
        "bgn_de": bgn_de,
        "end_de": end_de,
        "page_no": page_no,
        "page_count": PAGE_SIZE,
    }
    try:
        r = requests.get(DART_LIST_URL, params=params, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except requests.exceptions.RequestException as e:
        return None, f"fetch_page({page_no}): {type(e).__name__}: {e}"
    except ValueError as e:
        return None, f"fetch_page({page_no}): json decode: {e}"
    status = data.get("status")
    # DART status codes: 000 = success; 013 = no matching data; otherwise error.
    if status == "013":
        return {"list": [], "total_page": 0, "page_no": page_no}, ""
    if status != "000":
        return None, f"dart status={status} msg={data.get('message')}"
    return data, ""


# ---------------------------------------------------------------------------
# Signal builder
# ---------------------------------------------------------------------------

def _record_to_signal(rec: Dict[str, Any], scan_date: datetime) -> Optional[Signal]:
    report_nm = (rec.get("report_nm") or "").strip()
    if not report_nm:
        return None
    cls = _classify(report_nm)
    if cls is None:
        return None
    signal_type, _profile_hint, direction_raw = cls
    direction = _normalize_direction(direction_raw)

    corp_code = (rec.get("corp_code") or "").strip()
    corp_name = (rec.get("corp_name") or "").strip()
    stock_code = (rec.get("stock_code") or "").strip()
    rcept_no = (rec.get("rcept_no") or "").strip()
    rcept_dt = rec.get("rcept_dt") or ""
    rm = rec.get("rm") or ""
    flr_nm = rec.get("flr_nm") or ""

    filing_url = (
        f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"
        if rcept_no else None
    )

    # source_content_hash: prefer rcept_no (DART's unique filing id); fall back
    # to corp_code|rcept_dt|report_nm (v1 parity on the fallback).
    if rcept_no:
        hash_input = f"dart|rcept:{rcept_no}"
    else:
        hash_input = f"{corp_code}|{rcept_dt}|{report_nm}"
    source_content_hash = f"sha256:{hashlib.sha256(hash_input.encode('utf-8')).hexdigest()}"

    # signal_id: v1 keyed off corp_code + rcept_no, 32-char truncated.
    signal_id = hashlib.sha256(
        f"dart:{corp_code}:{rcept_no or hash_input}".encode("utf-8")
    ).hexdigest()[:32]

    source_date = _parse_rcept_dt(rcept_dt) or scan_date

    # Best-effort OpenFIGI resolution on the 6-digit stock code. Some Korean
    # issuers aren't in OpenFIGI; reactor/entity-resolver cascade handles misses.
    issuer_figi: Optional[str] = None
    if stock_code and len(stock_code) == 6:
        try:
            from modal_workers.shared.openfigi_resolver import resolve_ticker
            res = resolve_ticker(stock_code, exch_code="KS")
            if res.resolved:
                issuer_figi = res.issuer_figi
        except Exception:
            pass

    raw_payload: Dict[str, Any] = {
        "corp_code": corp_code,
        "stock_code": stock_code,
        "corp_name": corp_name,
        "report_nm": report_nm,
        "rcept_no": rcept_no,
        "rcept_dt": rcept_dt,
        "rm": rm,
        "flr_nm": flr_nm,
        "filing_url": filing_url,
        "headline": f"{stock_code or corp_code} {corp_name}: {report_nm[:140]}",
        "summary": report_nm,
    }

    entity_hints = EntityHints(
        issuer_figi=issuer_figi,
        ticker=stock_code or None,
        stock_code=stock_code or None,
        mic="XKRX",
        name=corp_name or None,
        country="KR",
    )

    return Signal(
        signal_id=signal_id,
        source_content_hash=source_content_hash,
        source_date=source_date,
        scan_date=scan_date,
        signal_type=signal_type,
        raw_payload=raw_payload,
        source_url=filing_url,
        issuer_figi=issuer_figi,
        entity_hints=entity_hints,
        thesis_direction=direction,
        strength_estimate=3,
    )


# ---------------------------------------------------------------------------
# scan entrypoint
# ---------------------------------------------------------------------------

def scan(cfg: ScannerConfig) -> ScannerResult:
    token = os.environ.get("OPENDART_KEY")
    if not token:
        raise MissingAuthError(
            "OPENDART_KEY env var missing -- OpenDART requires a free crtfc_key. "
            "Register at https://opendart.fss.or.kr/uss/umt/EgovMberInsertView.do "
            "and set via Modal secret `scanner-secrets`."
        )

    client = SupabaseClient()

    # Route OpenFIGI cache reads/writes through Supabase Storage (matches edgar).
    from modal_workers.shared.openfigi_resolver import set_cache_backend
    set_cache_backend(*client.openfigi_cache_backend())

    scan_date = datetime.now(timezone.utc)
    today = scan_date.date()
    since = today - timedelta(days=LOOKBACK_DAYS)
    bgn_de = since.strftime("%Y%m%d")
    end_de = today.strftime("%Y%m%d")

    budget = max(10, cfg.timeout_soft_s - 5)  # leave headroom for final ops
    scan_start = time.time()
    warnings: List[str] = []
    fetch_errors: List[str] = []

    all_records: List[Dict[str, Any]] = []
    page_no = 1
    pagination_incomplete = False
    while page_no <= MAX_PAGES:
        if time.time() - scan_start > budget:
            warnings.append(
                f"wall-clock budget ({budget}s) exceeded at page {page_no}; "
                "pagination incomplete"
            )
            pagination_incomplete = True
            break
        data, err = _fetch_page(token, bgn_de, end_de, page_no)
        if err:
            fetch_errors.append(err)
            break
        page_list = data.get("list") or []
        if not page_list:
            break
        all_records.extend(page_list)
        total_page = int(data.get("total_page") or 1)
        if page_no >= total_page:
            break
        page_no += 1
    else:
        # Exited via MAX_PAGES bound without breaking; flag as incomplete if
        # the registry had more pages to offer.
        pagination_incomplete = True

    # If we hit the cap mid-fetch but DART still had more pages, emit warning.
    if page_no > MAX_PAGES:
        warnings.append(f"hit MAX_PAGES cap ({MAX_PAGES}); may have truncated results")
        pagination_incomplete = True

    if fetch_errors:
        warnings.extend(fetch_errors)

    # Hard failure: no records AND every page errored.
    if fetch_errors and not all_records:
        return ScannerResult(
            scanner=NAME,
            status="error",
            signals=[],
            warnings=warnings,
            fetched_records=0,
            error="; ".join(fetch_errors),
        )

    # Build signals with per-run dedup on source_content_hash.
    signals: List[Signal] = []
    seen: set[str] = set()
    for rec in all_records:
        sig = _record_to_signal(rec, scan_date)
        if sig is None:
            continue
        if sig.source_content_hash in seen:
            continue
        seen.add(sig.source_content_hash)
        signals.append(sig)

    status = "partial" if (warnings or pagination_incomplete) else "ok"

    return ScannerResult(
        scanner=NAME,
        status=status,
        signals=signals,
        warnings=warnings,
        fetched_records=len(all_records),
    )
