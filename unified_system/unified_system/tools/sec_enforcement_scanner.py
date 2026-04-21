"""
SEC Enforcement Scanner — SEC litigation releases + administrative proceedings.

Promoted from stub to operational on 2026-04-16.

Data sources (both RSS, no auth):
  - https://www.sec.gov/enforcement-litigation/litigation-releases/rss
  - https://www.sec.gov/enforcement-litigation/administrative-proceedings/rss

Emits into the unified signal schema. Default profile: litigation.
Pairs with courtlistener_scanner for same-direction convergence —
both target US federal / SEC enforcement events.

Note: SEC RSS entries carry company names in the title but no ticker.
run_post_scan + openfigi_resolver will resolve ticker→figi downstream
when a ticker hint is present. For company-name-only cases, the signal
will land with figi=None and be picked up by the party_resolver in
future sessions (Q-007).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).parent))
try:
    from http_client import HttpClient  # type: ignore
except Exception:
    HttpClient = None

NAME = "sec_enforcement_scanner"
REPO = Path(__file__).parent.parent
OUT_FILE = REPO / "signals" / f"{NAME}_output.json"
OUT_FILE.parent.mkdir(parents=True, exist_ok=True)

FEED_LITREL = "https://www.sec.gov/enforcement-litigation/litigation-releases/rss"
FEED_ADMIN = "https://www.sec.gov/enforcement-litigation/administrative-proceedings/rss"

# Extract ticker hints from title: "Acme Corp. (ACME)" or "ACME Holdings"
TICKER_HINT_RE = re.compile(r'\(\s*"?([A-Z]{2,5})"?\s*\)')

# Company-type keywords that suggest an operating company (vs. pure individual
# defendant). Helps us skip solo-trader cases that aren't tradeable.
CORP_HINTS = re.compile(
    r'\b(Inc\.?|Corp\.?|Corporation|Company|Co\.?|LLC|LLP|LP|Ltd\.?|'
    r'Holdings|Group|Partners|Capital|Management|Fund|Trust|PLC|AG|SA|NV)\b',
    re.IGNORECASE,
)


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sig_id(src: str, release_id: str, pubdate: str) -> str:
    key = f"{src}:{release_id}:{pubdate}"
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def _content_hash(title: str, pubdate: str) -> str:
    return hashlib.sha256(f"{title}|{pubdate}".encode()).hexdigest()[:16]


def _parse_pub_date(s: str) -> str:
    """RSS pubDate → ISO-8601 UTC."""
    if not s:
        return ""
    try:
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        return ""


def _parse_feed(xml_text: str) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    if not xml_text:
        return items
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return items
    # RSS 2.0 — items under channel
    for item in root.findall(".//item"):
        def txt(tag, ns=None):
            el = item.find(tag)
            return (el.text or "").strip() if el is not None and el.text else ""
        dc_creator = item.find("{http://purl.org/dc/elements/1.1/}creator")
        items.append({
            "title": txt("title"),
            "link": txt("link").strip(),
            "description": txt("description"),
            "pub_date": txt("pubDate"),
            "release_id": (dc_creator.text or "").strip() if dc_creator is not None and dc_creator.text else "",
        })
    return items


def _classify(source: str, title: str) -> tuple:
    """Return (signal_type, thesis_direction) for a release.

    Heuristics:
      - admin proceedings with 'cease-and-desist' → cease_and_desist
      - admin proceedings with 'administrative' → administrative_proceeding
      - litigation releases → litigation_release
      - direction is typically 'short' (enforcement = negative for target)
        unless the title clearly describes a settlement favorable to the
        company (rare in these feeds).
    """
    t = (title or "").lower()
    if source == "admin":
        if "cease-and-desist" in t or "cease and desist" in t:
            return "cease_and_desist", "short"
        return "administrative_proceeding", "short"
    # litigation releases
    return "litigation_release", "short"


def _is_tradeable(title: str) -> bool:
    """Heuristic: skip releases that appear to target only individuals."""
    if not title:
        return False
    # If it explicitly has a ticker hint, it's tradeable.
    if TICKER_HINT_RE.search(title):
        return True
    # Corporate-entity keywords suggest a company is named.
    if CORP_HINTS.search(title):
        return True
    # Otherwise likely individual-only (e.g., "John Doe and Jane Roe").
    return False


def _item_to_signal(source: str, item: Dict[str, str]) -> Optional[Dict[str, Any]]:
    title = item.get("title") or ""
    if not title:
        return None
    if not _is_tradeable(title):
        return None  # skip pure-individual cases
    pub_date_iso = _parse_pub_date(item.get("pub_date", ""))
    signal_type, direction = _classify(source, title)
    release_id = item.get("release_id") or ""
    link = item.get("link") or ""
    ticker_hint = None
    m = TICKER_HINT_RE.search(title)
    if m:
        ticker_hint = m.group(1)
    sid = _sig_id(source, release_id, pub_date_iso)
    chash = _content_hash(title, pub_date_iso)
    return {
        "signal_id": sid,
        "source_content_hash": chash,
        "scanner_source": NAME,
        "upstream_scanner": NAME,
        "scoring_profile": "litigation",
        "signal_type": signal_type,
        "thesis_direction": direction,
        "ticker": ticker_hint,
        "figi": None,
        "issuer_figi": None,
        "company_name_en": title,
        "release_id": release_id,
        "source_feed": source,
        "release_url": link,
        "scan_date": _iso(),
        "source_date": pub_date_iso or _iso(),
        "headline": f"{release_id or 'SEC'}: {title}",
        "summary": item.get("description") or title,
        "raw_data": {
            "source_feed": source,
            "release_id": release_id,
            "pub_date_raw": item.get("pub_date", ""),
            "ticker_hint_source": "title_paren" if ticker_hint else None,
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
    feeds = [
        ("litrel", FEED_LITREL),
        ("admin", FEED_ADMIN),
    ]
    all_items: List[tuple] = []
    fetch_stats: Dict[str, Any] = {}
    errors: List[str] = []
    for src, url in feeds:
        try:
            r = client.get(url, timeout_s=15)
            r.raise_for_status()
            items = _parse_feed(r.text)
            fetch_stats[src] = len(items)
            for it in items:
                all_items.append((src, it))
        except Exception as e:
            errors.append(f"{src}: {type(e).__name__}: {e}")
            fetch_stats[src] = 0

    signals: List[Dict[str, Any]] = []
    seen = set()
    skipped_individual = 0
    for src, it in all_items:
        sig = _item_to_signal(src, it)
        if sig is None:
            skipped_individual += 1
            continue
        h = sig["source_content_hash"]
        if h in seen:
            continue
        seen.add(h)
        signals.append(sig)

    status = "ok" if not errors else ("partial" if signals else "error")
    return {
        "scanner": NAME,
        "ran_at_utc": _iso(),
        "status": status,
        "signals": signals,
        "fetched_items": fetch_stats,
        "skipped_individual_only": skipped_individual,
        "unique_signals": len(signals),
        "errors": errors,
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
        "fetched": result.get("fetched_items", {}),
    }))


if __name__ == "__main__":
    main()

# --- END OF FILE ---
