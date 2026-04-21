"""
HKEX Scanner — Hong Kong Exchanges announcements.

Promoted from stub to operational on 2026-04-16.

Data source: https://www1.hkexnews.hk/search/titleSearchServlet.do
Returns JSON-in-JSON (outer has a "result" key whose value is a
JSON-encoded string). ~50 records per page, 7-day lookback yields
~4600 records; we paginate with rowRange up to 200 to cap volume.

Signal-type mapping:
  Takeover / Offer announcements (Rule 3.5, Codes on Takeovers) → merger_arb
  Scheme of arrangement                                         → merger_arb
  Privatisation / delisting                                     → merger_arb
  Disclosure of Interests (Part XV), major shareholder changes  → activist_governance
  Profit warning / profit alert / negative                      → activist_governance
  Trading suspension / resumption                               → activist_governance
  Going concern / material uncertainty                          → activist_governance
  Resignation of auditor / qualified opinion                    → activist_governance

Skips boilerplate: annual reports, ESG reports, notice of AGM, circulars
for routine share mandates, dividend announcements, proxy forms.
"""

from __future__ import annotations

import hashlib
import html
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

NAME = "hkex_scanner"
REPO = Path(__file__).parent.parent
OUT_FILE = REPO / "signals" / f"{NAME}_output.json"
OUT_FILE.parent.mkdir(parents=True, exist_ok=True)

BASE_URL = "https://www1.hkexnews.hk/search/titleSearchServlet.do"
HKEXNEWS_ROOT = "https://www1.hkexnews.hk"

LOOKBACK_DAYS = 3  # short lookback — HKEX produces ~2000/day; dedup + cadence keep it sane
ROW_RANGE = 200    # max records per request

# Mapping from normalized category → (signal_type, profile, direction)
# Note LONG_TEXT comes wrapped as "Major Category - [Sub Category]".
# We inspect the sub-category token for the high-signal patterns.
HIGH_SIGNAL_PATTERNS = [
    # Takeovers / mergers
    (re.compile(r"(?i)takeover|offer\s+announcement|rule\s*3\.5|mandatory\s+offer|"
                r"voluntary\s+offer|privatisation|privatization|delisting", re.I),
     ("tender_offer", "merger_arb", "long")),
    (re.compile(r"(?i)scheme\s+of\s+arrangement", re.I),
     ("scheme_of_arrangement", "merger_arb", "long")),
    # Activist / governance signals
    (re.compile(r"(?i)disclosure\s+of\s+interest|part\s+xv|substantial\s+shareholder|"
                r"major\s+shareholder\s+change", re.I),
     ("major_shareholder_change", "activist_governance", "neutral")),
    (re.compile(r"(?i)profit\s+warning|profit\s+alert|loss\s+alert|expected\s+loss", re.I),
     ("profit_warning", "activist_governance", "short")),
    (re.compile(r"(?i)trading\s+(suspension|halt)|resumption\s+of\s+trading", re.I),
     ("trading_suspension", "activist_governance", "short")),
    (re.compile(r"(?i)going\s+concern|material\s+uncertainty|qualified\s+opinion|"
                r"resignation\s+of\s+auditor", re.I),
     ("going_concern", "activist_governance", "short")),
    (re.compile(r"(?i)connected\s+transaction|very\s+substantial\s+(disposal|acquisition)|"
                r"major\s+transaction", re.I),
     ("material_transaction", "activist_governance", "neutral")),
]

# Blacklist — boilerplate categories that generate overwhelming noise
BOILERPLATE_PATTERNS = [
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


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sig_id(news_id: str, stock_code: str) -> str:
    return hashlib.sha256(f"hkex:{stock_code}:{news_id}".encode()).hexdigest()[:32]


def _content_hash(title: str, stock_code: str, date_time: str) -> str:
    return hashlib.sha256(f"{stock_code}|{title}|{date_time}".encode()).hexdigest()[:16]


def _unescape(s: str) -> str:
    """HKEX HTML-escapes slashes and angle brackets in LONG_TEXT/TITLE."""
    if not s:
        return ""
    # Their payload uses &#x2f; for '/', &#x3b; for ';', &lt;br/&gt; etc.
    return html.unescape(s)


def _classify(title: str, long_text: str) -> Optional[tuple]:
    """Return (signal_type, profile, direction) or None to drop."""
    combined = f"{_unescape(title)}  {_unescape(long_text)}"
    # Drop boilerplate first
    for pat in BOILERPLATE_PATTERNS:
        if pat.search(combined):
            return None
    for pat, result in HIGH_SIGNAL_PATTERNS:
        if pat.search(combined):
            return result
    return None  # unrecognized → drop (conservative)


def _build_search_url(days: int = LOOKBACK_DAYS, row_range: int = ROW_RANGE) -> str:
    today = datetime.now(timezone.utc).date()
    since = today - timedelta(days=days)
    return (
        f"{BASE_URL}?sortDir=0&sortByOption=DateTime&category=0&market=SEHK"
        f"&stockId=-1&documentType=-1"
        f"&fromDate={since.strftime('%Y%m%d')}&toDate={today.strftime('%Y%m%d')}"
        f"&t1code=-2&t2Gcode=-2&t2code=-2&rowRange={row_range}"
    )


def _parse_hkex_datetime(s: str) -> str:
    """'16/04/2026 22:59' (HKT) → ISO-8601 UTC."""
    if not s:
        return ""
    try:
        # HKEX posts in HKT (UTC+8)
        dt_local = datetime.strptime(s, "%d/%m/%Y %H:%M")
        dt_utc = dt_local - timedelta(hours=8)
        return dt_utc.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        return ""


def _record_to_signal(rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    title = rec.get("TITLE") or ""
    long_text = rec.get("LONG_TEXT") or ""
    cls = _classify(title, long_text)
    if cls is None:
        return None
    signal_type, profile, direction = cls
    stock_code = (rec.get("STOCK_CODE") or "").strip()
    stock_name = (rec.get("STOCK_NAME") or "").strip()
    news_id = str(rec.get("NEWS_ID") or "")
    date_time = rec.get("DATE_TIME") or ""
    file_link = rec.get("FILE_LINK") or ""
    url = f"{HKEXNEWS_ROOT}{file_link}" if file_link and file_link.startswith("/") else file_link

    return {
        "signal_id": _sig_id(news_id, stock_code),
        "source_content_hash": _content_hash(title, stock_code, date_time),
        "scanner_source": NAME,
        "upstream_scanner": NAME,
        "scoring_profile": profile,
        "signal_type": signal_type,
        "thesis_direction": direction,
        "ticker": stock_code,         # HKEX stock codes are 4-5 digit numerics
        "mic": "XHKG",
        "figi": None,
        "issuer_figi": None,
        "company_name_en": stock_name,
        "news_id": news_id,
        "filing_url": url,
        "scan_date": _iso(),
        "source_date": _parse_hkex_datetime(date_time) or _iso(),
        "headline": f"{stock_code} {stock_name}: {_unescape(title)[:100]}",
        "summary": _unescape(long_text),
        "raw_data": {
            "stock_code": stock_code,
            "news_id": news_id,
            "date_time_hkt": date_time,
            "file_type": rec.get("FILE_TYPE"),
            "file_info": rec.get("FILE_INFO"),
            "long_text_raw": long_text,
        },
    }


def scan() -> Dict[str, Any]:
    if HttpClient is None:
        return {
            "scanner": NAME,
            "ran_at_utc": _iso(),
            "status": "error",
            "signals": [],
            "error": "http_client module not importable",
        }
    client = HttpClient()
    url = _build_search_url()
    try:
        r = client.get(url, timeout_s=25)
        r.raise_for_status()
        outer = r.json()
    except Exception as e:
        return {
            "scanner": NAME,
            "ran_at_utc": _iso(),
            "status": "error",
            "signals": [],
            "error": f"fetch failed: {type(e).__name__}: {e}",
        }

    try:
        # Inner result is a JSON string
        result_str = outer.get("result", "[]")
        records = json.loads(result_str) if isinstance(result_str, str) else (result_str or [])
    except Exception as e:
        return {
            "scanner": NAME,
            "ran_at_utc": _iso(),
            "status": "error",
            "signals": [],
            "error": f"parse failed: {e}",
        }

    signals: List[Dict[str, Any]] = []
    seen = set()
    skipped_boilerplate = 0
    skipped_unmatched = 0
    for rec in records:
        sig = _record_to_signal(rec)
        if sig is None:
            # Distinguish boilerplate (matched a blacklist) from unmatched (no pattern hit).
            # Re-run classifier just to count them; cheap.
            combined = f"{_unescape(rec.get('TITLE',''))}  {_unescape(rec.get('LONG_TEXT',''))}"
            if any(p.search(combined) for p in BOILERPLATE_PATTERNS):
                skipped_boilerplate += 1
            else:
                skipped_unmatched += 1
            continue
        h = sig["source_content_hash"]
        if h in seen:
            continue
        seen.add(h)
        signals.append(sig)

    return {
        "scanner": NAME,
        "ran_at_utc": _iso(),
        "status": "ok",
        "signals": signals,
        "fetched_records": len(records),
        "unique_signals": len(signals),
        "skipped_boilerplate": skipped_boilerplate,
        "skipped_unmatched": skipped_unmatched,
        "lookback_days": LOOKBACK_DAYS,
    }


def main():
    result = scan()
    tmp = OUT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, indent=2))
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
