"""
CourtListener Scanner — US federal district + appellate dockets.

Promoted from stub to operational on 2026-04-16.
Requires COURTLISTENER_TOKEN env var. Returns status=auth_required cleanly
if token is missing. Signal envelope matches the unified schema.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent))
try:
    from http_client import HttpClient  # type: ignore
except Exception:
    HttpClient = None

NAME = "courtlistener_scanner"
REPO = Path(__file__).parent.parent
OUT_FILE = REPO / "signals" / f"{NAME}_output.json"
OUT_FILE.parent.mkdir(parents=True, exist_ok=True)

BASE_URL = "https://www.courtlistener.com/api/rest/v4"

NOS_SECURITIES = {"850"}
NOS_CONTRACT_MA = {"190"}
NOS_PATENT = {"830", "835"}
NOS_ANTITRUST = {"410"}
TARGET_NOS = NOS_SECURITIES | NOS_CONTRACT_MA | NOS_PATENT | NOS_ANTITRUST
LOOKBACK_DAYS = 7

TICKER_HINT_RE = re.compile(r'\(\s*"?([A-Z]{2,5})"?\s*\)')


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sig_id(payload: Dict[str, Any]) -> str:
    key = f"{payload.get('docket_id','?')}:{payload.get('filing_date','?')}:{payload.get('signal_type','?')}"
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def _content_hash(payload: Dict[str, Any]) -> str:
    key = f"{payload.get('case_name','')}|{payload.get('filing_date','')}|{payload.get('court','')}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


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


def _fetch_recent_dockets(client, token, days=LOOKBACK_DAYS):
    headers = {"Authorization": f"Token {token}"}
    since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    all_dockets = []
    for nos in sorted(TARGET_NOS):
        url = f"{BASE_URL}/dockets/"
        params = {
            "nature_of_suit": nos,
            "date_filed__gte": since,
            "order_by": "-date_filed",
            "page_size": 50,
        }
        try:
            resp = client.get_json(url, params=params, headers=headers, timeout_s=15)
        except Exception as e:
            print(f"  [warn] fetch NOS {nos} failed: {e}", file=sys.stderr)
            continue
        if not resp:
            continue
        for d in resp.get("results") or []:
            d["_nos_queried"] = nos
            all_dockets.append(d)
    return all_dockets


def _docket_to_signal(d):
    case_name = d.get("case_name") or d.get("case_name_short") or ""
    if not case_name:
        return None
    nos = d.get("_nos_queried") or d.get("nature_of_suit") or ""
    court = d.get("court") or d.get("court_id") or ""
    filing_date = d.get("date_filed") or ""
    docket_id = d.get("id") or d.get("docket_id")
    ticker_hint = _extract_ticker_hint(case_name)
    signal_type = _classify_signal_type(str(nos), case_name)

    body = {
        "signal_id": None,
        "source_content_hash": None,
        "scanner_source": NAME,
        "upstream_scanner": NAME,
        "scoring_profile": "litigation",
        "signal_type": signal_type,
        "thesis_direction": "short" if signal_type in ("federal_civil_filed", "mtd_denied", "class_certified") else "neutral",
        "ticker": ticker_hint,
        "figi": None,
        "issuer_figi": None,
        "case_name": case_name,
        "court": str(court),
        "filing_date": filing_date,
        "docket_id": docket_id,
        "nos": str(nos),
        "docket_url": f"https://www.courtlistener.com/docket/{docket_id}/" if docket_id else None,
        "scan_date": _iso(),
        "source_date": (filing_date + "T00:00:00Z") if filing_date else _iso(),
        "headline": f"{case_name} — {signal_type.replace('_',' ')}",
        "summary": f"NOS {nos} filed {filing_date} in {court}",
        "raw_data": {
            "nature_of_suit": str(nos),
            "court_id": str(court),
            "ticker_hint_source": "case_name_paren" if ticker_hint else None,
        },
    }
    body["signal_id"] = _sig_id(body)
    body["source_content_hash"] = _content_hash(body)
    return body


def scan():
    token = os.environ.get("COURTLISTENER_TOKEN")
    if not token:
        return {
            "scanner": NAME,
            "ran_at_utc": _iso(),
            "status": "auth_required",
            "signals": [],
            "warnings": ["COURTLISTENER_TOKEN env var not set. Get a free token at "
                         "https://www.courtlistener.com/help/api/rest/authentication/"],
            "fetched_dockets": 0,
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
    try:
        dockets = _fetch_recent_dockets(client, token)
    except Exception as e:
        return {
            "scanner": NAME,
            "ran_at_utc": _iso(),
            "status": "error",
            "signals": [],
            "error": f"fetch failed: {e}",
        }

    signals = []
    seen = set()
    for d in dockets:
        sig = _docket_to_signal(d)
        if not sig:
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
        "fetched_dockets": len(dockets),
        "unique_signals": len(signals),
        "lookback_days": LOOKBACK_DAYS,
        "target_nos": sorted(TARGET_NOS),
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
        "fetched_dockets": result.get("fetched_dockets", 0),
    }))


if __name__ == "__main__":
    main()

# --- END OF FILE ---
