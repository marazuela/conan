"""
BSE/NSE Scanner - India corporate announcements.

Promoted from stub to operational on 2026-04-16 (S2d).

Data source: NSE's public corporate-announcements API.
  https://www.nseindia.com/api/corporate-announcements?index=equities
      &from_date=DD-MM-YYYY&to_date=DD-MM-YYYY

The endpoint requires a cookie warmup on the NSE home page (anti-bot),
then returns clean JSON. ~500 records/day typical. We use a 3-day
lookback to match HKEX cadence and keep volume sane.

BSE's api.bseindia.com endpoint returns an HTML WAF interstitial for
anonymous requests in this sandbox - NSE alone is used here. NSE
is India's primary exchange (~90% of equity volume) so coverage loss
from skipping BSE is minor.

Signal-type mapping (based on NSE 'desc' field):
  Acquisition                                           -> merger_arb (long)
  Amalgamation/Merger                                   -> merger_arb (unknown)
  Scheme of Arrangement                                 -> merger_arb (long)
  Disclosure under SEBI Takeover Regulations            -> activist_governance (neutral)
  Open Offer / Delisting / Buyback Offer                -> merger_arb / activist_governance
  Change in Auditors / Resignation of Independent dir.  -> activist_governance (short)
  Pendency of Litigation(s)                             -> litigation (short)
  Disclosure of material issue                          -> activist_governance (short)
  Suspension of Trading                                 -> activist_governance (short)
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

NAME = "bse_nse_scanner"
REPO = Path(__file__).parent.parent
OUT_FILE = REPO / "signals" / f"{NAME}_output.json"
OUT_FILE.parent.mkdir(parents=True, exist_ok=True)

NSE_HOME = "https://www.nseindia.com/"
NSE_API = "https://www.nseindia.com/api/corporate-announcements"
LOOKBACK_DAYS = 3

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

HIGH_SIGNAL_PATTERNS = [
    (re.compile(r"(?i)^\s*acquisition\s*$", re.I),
     ("acquisition", "merger_arb", "long")),
    (re.compile(r"(?i)amalgamation|\bmerger\b", re.I),
     ("amalgamation_merger", "merger_arb", "unknown")),
    (re.compile(r"(?i)scheme\s+of\s+arrangement", re.I),
     ("scheme_of_arrangement", "merger_arb", "long")),
    (re.compile(r"(?i)open\s+offer|delisting", re.I),
     ("open_offer", "merger_arb", "long")),
    (re.compile(r"(?i)buy[-\s]?back", re.I),
     ("buyback", "activist_governance", "long")),
    (re.compile(r"(?i)sebi\s+takeover\s+regulations?", re.I),
     ("takeover_disclosure", "activist_governance", "neutral")),
    (re.compile(r"(?i)substantial\s+acquisition|change\s+in\s+promoter", re.I),
     ("major_shareholder_change", "activist_governance", "neutral")),
    (re.compile(r"(?i)change\s+in\s+auditors?|resignation\s+of\s+auditor", re.I),
     ("auditor_change", "activist_governance", "short")),
    (re.compile(r"(?i)resignation\s+of\s+independent\s+director", re.I),
     ("independent_director_resignation", "activist_governance", "short")),
    (re.compile(r"(?i)^\s*resignation\s*$", re.I),
     ("board_resignation", "activist_governance", "neutral")),
    (re.compile(r"(?i)disclosure\s+of\s+material\s+issue", re.I),
     ("material_issue", "activist_governance", "short")),
    (re.compile(r"(?i)suspension\s+of\s+trading", re.I),
     ("trading_suspension", "activist_governance", "short")),
    (re.compile(r"(?i)pendency\s+of\s+litigation|outcome.*impacting", re.I),
     ("pending_litigation", "litigation", "short")),
    (re.compile(r"(?i)profit\s+warning|loss\s+alert|impairment", re.I),
     ("profit_warning", "activist_governance", "short")),
    (re.compile(r"(?i)shut[-\s]?down|plant.*closure|operations.*halted", re.I),
     ("operational_shock", "activist_governance", "short")),
]

BOILERPLATE_PATTERNS = [
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


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sig_id(symbol: str, seq_id: str, an_dt: str) -> str:
    return hashlib.sha256(f"nse:{symbol}:{seq_id}:{an_dt}".encode()).hexdigest()[:32]


def _content_hash(symbol: str, desc: str, an_dt: str) -> str:
    return hashlib.sha256(f"{symbol}|{desc}|{an_dt}".encode()).hexdigest()[:16]


def _classify(desc: str, attchmnt_text: str) -> Optional[tuple]:
    combined = f"{desc or ''}  {attchmnt_text or ''}"
    for pat in BOILERPLATE_PATTERNS:
        if pat.search(desc or ""):
            return None
    for pat, result in HIGH_SIGNAL_PATTERNS:
        if pat.search(combined):
            return result
    return None


def _parse_nse_dt(s: str) -> str:
    if not s:
        return ""
    try:
        dt_local = datetime.strptime(s, "%d-%b-%Y %H:%M:%S")
        dt_utc = dt_local - timedelta(hours=5, minutes=30)
        return dt_utc.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        return ""


def _record_to_signal(rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    symbol = (rec.get("symbol") or "").strip()
    desc = (rec.get("desc") or "").strip()
    attch_text = (rec.get("attchmntText") or "").strip()
    if not symbol or not desc:
        return None
    cls = _classify(desc, attch_text)
    if cls is None:
        return None
    signal_type, profile, direction = cls
    an_dt = rec.get("an_dt") or ""
    sort_dt = rec.get("sort_date") or an_dt
    seq_id = str(rec.get("seq_id") or "")
    company = (rec.get("sm_name") or "").strip()
    isin = (rec.get("sm_isin") or "").strip()
    attch_file = rec.get("attchmntFile") or ""

    return {
        "signal_id": _sig_id(symbol, seq_id, an_dt),
        "source_content_hash": _content_hash(symbol, desc, an_dt),
        "scanner_source": NAME,
        "upstream_scanner": NAME,
        "scoring_profile": profile,
        "signal_type": signal_type,
        "thesis_direction": direction,
        "ticker": symbol,
        "mic": "XNSE",
        "figi": None,
        "issuer_figi": None,
        "isin": isin,
        "company_name_en": company,
        "seq_id": seq_id,
        "filing_url": attch_file or None,
        "scan_date": _iso(),
        "source_date": _parse_nse_dt(an_dt) or _iso(),
        "headline": f"{symbol} {company}: {desc[:120]}",
        "summary": attch_text[:2000] if attch_text else desc,
        "raw_data": {
            "symbol": symbol,
            "isin": isin,
            "an_dt_ist": an_dt,
            "sort_date_ist": sort_dt,
            "desc": desc,
            "industry": rec.get("smIndustry"),
            "file_size": rec.get("fileSize"),
        },
    }


def _fetch_nse(client, days: int = LOOKBACK_DAYS):
    try:
        client.get(NSE_HOME, timeout_s=15, headers=NSE_HEADERS_HOME)
    except Exception as e:
        return [], f"home warmup failed: {type(e).__name__}: {e}"

    today = datetime.now(timezone.utc).date()
    since = today - timedelta(days=days)
    url = (
        f"{NSE_API}?index=equities"
        f"&from_date={since.strftime('%d-%m-%Y')}"
        f"&to_date={today.strftime('%d-%m-%Y')}"
    )
    try:
        r = client.get(url, timeout_s=20, headers=NSE_HEADERS_API)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return [], f"api fetch failed: {type(e).__name__}: {e}"
    if not isinstance(data, list):
        return [], f"unexpected payload shape: {type(data).__name__}"
    return data, ""


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
    records, err = _fetch_nse(client, LOOKBACK_DAYS)
    if err:
        return {
            "scanner": NAME,
            "ran_at_utc": _iso(),
            "status": "error",
            "signals": [],
            "error": err,
        }

    signals: List[Dict[str, Any]] = []
    seen = set()
    skipped_boilerplate = 0
    skipped_unmatched = 0
    for rec in records:
        sig = _record_to_signal(rec)
        if sig is None:
            desc = (rec.get("desc") or "")
            if any(p.search(desc) for p in BOILERPLATE_PATTERNS):
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
