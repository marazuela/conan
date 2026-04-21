"""
KIND Scanner - Korea corporate disclosures via OpenDART.

Promoted from stub to operational on 2026-04-16 (S2e).
(Note: the original stub docstring incorrectly referenced Borsa Istanbul /
KAP - that was a copy-paste error during S1 scaffolding. The correct
target is Korea's DART system; the filename/registry key 'kind' reflects
the KIND portal which is DART's consumer-facing view.)

Data source: OpenDART (Financial Supervisory Service of Korea)
  https://opendart.fss.or.kr/api/list.json
  20,000 req/day free quota. Requires crtfc_key env var (OPENDART_KEY).

KIND (kind.krx.co.kr) itself is client-side rendered and WAF-gated for
programmatic access; OpenDART is the programmatic equivalent covering
the same disclosure universe with official FSS filings.

Returns status=auth_required cleanly if OPENDART_KEY is missing -
same pattern as courtlistener_scanner.

Signal-type mapping (DART report codes, observed in list endpoint):
  A (main report / annual etc)          -> skip boilerplate
  B001 ~ B006 (tender offer filings)   -> merger_arb (long)
  C001 ~ C009 (substantial shareholdin) -> activist_governance (neutral)
  D (fund)                              -> skip
  E (spinoff/merger)                    -> merger_arb (unknown)
  F (audit report)                      -> skip boilerplate
  G (securities registration)           -> skip
  H (other fair disclosures)            -> classify by title keyword

Additionally we scan report_nm (report name / Korean title) and pattern
match for:
  "유상증자"     (rights issue/capital raise)     -> activist_governance (short)
  "합병"         (merger)                          -> merger_arb
  "공개매수"     (tender offer)                    -> merger_arb (long)
  "영업정지"     (business suspension)             -> activist_governance (short)
  "감사의견"     (audit opinion changes)           -> activist_governance (short)
  "소송"         (lawsuit)                         -> litigation (short)
  "횡령"         (embezzlement)                    -> activist_governance (short)
  "배임"         (breach of trust)                 -> activist_governance (short)
  "상장폐지"     (delisting)                       -> activist_governance (short)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent))
try:
    from http_client import HttpClient  # type: ignore
except Exception:
    HttpClient = None

NAME = "kind_scanner"
REPO = Path(__file__).parent.parent
OUT_FILE = REPO / "signals" / f"{NAME}_output.json"
OUT_FILE.parent.mkdir(parents=True, exist_ok=True)

DART_LIST_URL = "https://opendart.fss.or.kr/api/list.json"
LOOKBACK_DAYS = 3
PAGE_SIZE = 100

# Korean title keyword patterns. Ordered by specificity (most specific first).
HIGH_SIGNAL_PATTERNS = [
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

# Categories / disclosure types we drop wholesale
BOILERPLATE_PATTERNS = [
    re.compile(r"사업보고서|반기보고서|분기보고서"),  # annual/interim/quarterly report
    re.compile(r"감사보고서(?!.*거절)"),             # audit report (unless rejected)
    re.compile(r"주주총회소집"),                      # notice of shareholder meeting
    re.compile(r"배당.*공시|배당금지급"),             # dividend announcements
    re.compile(r"공시서류.*제출기한연장"),            # filing deadline extensions
    re.compile(r"증권발행실적보고서"),                # securities issuance report (routine)
    re.compile(r"자기주식.*취득결과"),                # buyback-completion reports (routine)
]


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sig_id(corp_code: str, rcept_no: str) -> str:
    return hashlib.sha256(f"dart:{corp_code}:{rcept_no}".encode()).hexdigest()[:32]


def _content_hash(corp_code: str, report_nm: str, rcept_dt: str) -> str:
    return hashlib.sha256(f"{corp_code}|{report_nm}|{rcept_dt}".encode()).hexdigest()[:16]


def _parse_rcept_dt(s: str) -> str:
    """'20260416' (KST date) -> ISO-8601 UTC start-of-day equivalent.

    DART only provides date-level precision for rcept_dt in the list endpoint.
    We approximate to 09:00 KST (typical market-open disclosure time) -> 00:00 UTC.
    """
    if not s or len(s) != 8:
        return ""
    try:
        dt_local = datetime.strptime(s, "%Y%m%d")
        # KST = UTC+9. A disclosure dated YYYYMMDD typically hits mid-morning KST.
        # Use 09:00 KST = 00:00 UTC same day.
        return dt_local.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        return ""


def _classify(report_nm: str) -> Optional[tuple]:
    if not report_nm:
        return None
    for pat in BOILERPLATE_PATTERNS:
        if pat.search(report_nm):
            return None
    for pat, result in HIGH_SIGNAL_PATTERNS:
        if pat.search(report_nm):
            return result
    return None


def _record_to_signal(rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    report_nm = (rec.get("report_nm") or "").strip()
    if not report_nm:
        return None
    cls = _classify(report_nm)
    if cls is None:
        return None
    signal_type, profile, direction = cls
    corp_code = (rec.get("corp_code") or "").strip()
    corp_name = (rec.get("corp_name") or "").strip()
    stock_code = (rec.get("stock_code") or "").strip()
    rcept_no = (rec.get("rcept_no") or "").strip()
    rcept_dt = rec.get("rcept_dt") or ""
    flr_nm = rec.get("flr_nm") or ""

    filing_url = (f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"
                  if rcept_no else None)

    return {
        "signal_id": _sig_id(corp_code, rcept_no),
        "source_content_hash": _content_hash(corp_code, report_nm, rcept_dt),
        "scanner_source": NAME,
        "upstream_scanner": NAME,
        "scoring_profile": profile,
        "signal_type": signal_type,
        "thesis_direction": direction,
        "ticker": stock_code or None,
        "mic": "XKRX",
        "figi": None,
        "issuer_figi": None,
        "company_name_en": corp_name,
        "corp_code": corp_code,
        "rcept_no": rcept_no,
        "filing_url": filing_url,
        "scan_date": _iso(),
        "source_date": _parse_rcept_dt(rcept_dt) or _iso(),
        "headline": f"{stock_code or corp_code} {corp_name}: {report_nm[:140]}",
        "summary": report_nm,
        "raw_data": {
            "corp_code": corp_code,
            "stock_code": stock_code,
            "rcept_no": rcept_no,
            "rcept_dt": rcept_dt,
            "flr_nm": flr_nm,
            "report_nm": report_nm,
        },
    }


def _fetch_page(client, token, bgn_de, end_de, page_no) -> tuple:
    params = {
        "crtfc_key": token,
        "bgn_de": bgn_de,
        "end_de": end_de,
        "page_no": page_no,
        "page_count": PAGE_SIZE,
    }
    try:
        r = client.get(DART_LIST_URL, params=params, timeout_s=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return None, f"fetch_page({page_no}): {type(e).__name__}: {e}"
    status = data.get("status")
    # DART status codes: 000 = success; 013 = no matching data; otherwise error.
    if status == "013":
        return {"list": [], "total_page": 0, "page_no": page_no}, ""
    if status != "000":
        return None, f"dart status={status} msg={data.get('message')}"
    return data, ""


def scan() -> Dict[str, Any]:
    token = os.environ.get("OPENDART_KEY")
    if not token:
        return {
            "scanner": NAME,
            "ran_at_utc": _iso(),
            "status": "auth_required",
            "signals": [],
            "warnings": ["OPENDART_KEY env var not set. Register free at "
                         "https://opendart.fss.or.kr/uss/umt/EgovMberInsertView.do"],
            "fetched_records": 0,
        }

    if HttpClient is None:
        return {
            "scanner": NAME,
            "ran_at_utc": _iso(),
            "status": "error",
            "signals": [],
            "error": "http_client module not importable",
        }

    client = HttpClient()
    today = datetime.now(timezone.utc).date()
    since = today - timedelta(days=LOOKBACK_DAYS)
    bgn_de = since.strftime("%Y%m%d")
    end_de = today.strftime("%Y%m%d")

    all_records: List[Dict[str, Any]] = []
    page_no = 1
    max_pages = 10  # cap at 1000 records per scan
    errors: List[str] = []
    while page_no <= max_pages:
        data, err = _fetch_page(client, token, bgn_de, end_de, page_no)
        if err:
            errors.append(err)
            break
        page_list = data.get("list") or []
        if not page_list:
            break
        all_records.extend(page_list)
        total_page = int(data.get("total_page") or 1)
        if page_no >= total_page:
            break
        page_no += 1

    if errors and not all_records:
        return {
            "scanner": NAME,
            "ran_at_utc": _iso(),
            "status": "error",
            "signals": [],
            "errors": errors,
            "fetched_records": 0,
        }

    signals: List[Dict[str, Any]] = []
    seen = set()
    skipped_boilerplate = 0
    skipped_unmatched = 0
    for rec in all_records:
        sig = _record_to_signal(rec)
        if sig is None:
            report_nm = rec.get("report_nm") or ""
            if any(p.search(report_nm) for p in BOILERPLATE_PATTERNS):
                skipped_boilerplate += 1
            else:
                skipped_unmatched += 1
            continue
        h = sig["source_content_hash"]
        if h in seen:
            continue
        seen.add(h)
        signals.append(sig)

    status = "ok" if not errors else "partial"
    return {
        "scanner": NAME,
        "ran_at_utc": _iso(),
        "status": status,
        "signals": signals,
        "fetched_records": len(all_records),
        "unique_signals": len(signals),
        "skipped_boilerplate": skipped_boilerplate,
        "skipped_unmatched": skipped_unmatched,
        "lookback_days": LOOKBACK_DAYS,
        "errors": errors,
    }


def main():
    result = scan()
    tmp = OUT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    os.replace(tmp, OUT_FILE)
    print(json.dumps({
        "signals": len(result["signals"]),
        "scanner": NAME,
        "status": result["status"],
        "fetched": result.get("fetched_records", 0),
    }))


if __name__ == "__main__":
    main()

# --- END OF FILE ---
