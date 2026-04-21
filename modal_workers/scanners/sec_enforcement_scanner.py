"""
SEC Enforcement scanner — Modal port of tools/sec_enforcement_scanner.py.

Preserved from v1 (byte-equivalent where relevant):
  - Dual RSS feeds: litigation releases + administrative proceedings.
  - Title-level classification (cease_and_desist / administrative_proceeding / litigation_release).
  - Corporate-entity heuristic (skip individual-only cases).
  - Ticker-hint extraction from parenthesised title uppercase (e.g. "Acme Corp. (ACME)").
  - Per-release dedup on source_content_hash.

Deviations from v1:
  - No OUT_FILE; signals returned via ScannerResult list for run_scanner plumbing.
  - User-Agent via SEC_USER_AGENT env var (MissingAuthError if unset — same contract as edgar).
  - source_content_hash now carries the spec.md §3.4 "sha256:<64hex>" prefix for convergence
    classification parity with edgar.

IO contract:
  scan(cfg: ScannerConfig) -> ScannerResult
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

import requests

from modal_workers.shared.scanner_base import MissingAuthError, Signal, ScannerResult
from modal_workers.shared.supabase_client import EntityHints, ScannerConfig

NAME = "sec_enforcement_scanner"

FEED_LITREL = "https://www.sec.gov/enforcement-litigation/litigation-releases/rss"
FEED_ADMIN = "https://www.sec.gov/enforcement-litigation/administrative-proceedings/rss"

TICKER_HINT_RE = re.compile(r'\(\s*"?([A-Z]{2,5})"?\s*\)')
CORP_HINTS = re.compile(
    r'\b(Inc\.?|Corp\.?|Corporation|Company|Co\.?|LLC|LLP|LP|Ltd\.?|'
    r'Holdings|Group|Partners|Capital|Management|Fund|Trust|PLC|AG|SA|NV)\b',
    re.IGNORECASE,
)

REQUEST_TIMEOUT = 15


# ---------------------------------------------------------------------------
# Feed fetch + parse
# ---------------------------------------------------------------------------

def _parse_pub_date(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _parse_feed(xml_text: str) -> List[Dict[str, str]]:
    if not xml_text:
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    items: List[Dict[str, str]] = []
    for item in root.findall(".//item"):
        def txt(tag: str) -> str:
            el = item.find(tag)
            return (el.text or "").strip() if el is not None and el.text else ""
        dc_creator = item.find("{http://purl.org/dc/elements/1.1/}creator")
        items.append({
            "title": txt("title"),
            "link": txt("link"),
            "description": txt("description"),
            "pub_date": txt("pubDate"),
            "release_id": (dc_creator.text or "").strip() if dc_creator is not None and dc_creator.text else "",
        })
    return items


def _fetch(url: str, user_agent: str) -> str:
    resp = requests.get(url, headers={"User-Agent": user_agent}, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.text


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def _classify(source: str, title: str) -> Tuple[str, str]:
    t = (title or "").lower()
    if source == "admin":
        if "cease-and-desist" in t or "cease and desist" in t:
            return "cease_and_desist", "short"
        return "administrative_proceeding", "short"
    return "litigation_release", "short"


def _procedural_stage(source: str, signal_type: str) -> tuple[str, str]:
    if source == "admin":
        if signal_type == "cease_and_desist":
            return "cease_and_desist", "≤1m"
        return "administrative_proceeding", "1-3m"
    return "litigation_release", "1-3m"


def _is_tradeable(title: str) -> bool:
    if not title:
        return False
    if TICKER_HINT_RE.search(title):
        return True
    if CORP_HINTS.search(title):
        return True
    return False


# ---------------------------------------------------------------------------
# Signal builder
# ---------------------------------------------------------------------------

def _build_signal(source: str, item: Dict[str, str], scan_date: datetime) -> Optional[Signal]:
    title = item.get("title") or ""
    if not title or not _is_tradeable(title):
        return None

    pub_dt = _parse_pub_date(item.get("pub_date", ""))
    source_date = pub_dt or scan_date

    signal_type, direction = _classify(source, title)
    procedural_stage, timeline_bucket = _procedural_stage(source, signal_type)
    release_id = item.get("release_id") or ""
    link = item.get("link") or ""

    ticker_hint: Optional[str] = None
    m = TICKER_HINT_RE.search(title)
    if m:
        ticker_hint = m.group(1)

    source_content_hash = (
        f"sha256:{hashlib.sha256(f'{title}|{source_date.isoformat()}|{source}'.encode()).hexdigest()}"
    )
    signal_id = hashlib.sha256(
        f"{source}:{release_id}:{source_date.isoformat()}".encode()
    ).hexdigest()[:32]

    raw_payload: Dict[str, Any] = {
        "source_feed": source,
        "release_id": release_id,
        "release_url": link,
        "title": title,
        "summary": item.get("description") or title,
        "company_name_en": title,
        "ticker_hint": ticker_hint,
        "ticker_hint_present": bool(ticker_hint),
        "case_family": "sec_admin" if source == "admin" else "sec_litigation",
        "procedural_stage": procedural_stage,
        "procedural_stage_confidence": "high",
        "resolution_timeline_bucket": timeline_bucket,
        "pub_date_raw": item.get("pub_date", ""),
        "headline": f"{release_id or 'SEC'}: {title}",
    }

    entity_hints = EntityHints(
        ticker=ticker_hint,
        mic=None,
        name=title,
        country="US",
    )

    return Signal(
        signal_id=signal_id,
        source_content_hash=source_content_hash,
        source_date=source_date,
        scan_date=scan_date,
        signal_type=signal_type,
        raw_payload=raw_payload,
        source_url=link or None,
        entity_hints=entity_hints,
        thesis_direction=direction,
        strength_estimate=3,
    )


# ---------------------------------------------------------------------------
# scan entrypoint
# ---------------------------------------------------------------------------

def scan(cfg: ScannerConfig) -> ScannerResult:
    user_agent = os.environ.get("SEC_USER_AGENT")
    if not user_agent:
        raise MissingAuthError(
            "SEC_USER_AGENT env var missing — SEC requires a valid contact email "
            "in the User-Agent header. Set via Modal secret `scanner-secrets`.")

    scan_date = datetime.now(timezone.utc)
    feeds = [("litrel", FEED_LITREL), ("admin", FEED_ADMIN)]

    warnings: List[str] = []
    signals: List[Signal] = []
    seen: set[str] = set()
    fetched = 0

    for src, url in feeds:
        try:
            xml_text = _fetch(url, user_agent)
        except Exception as e:  # noqa: BLE001
            warnings.append(f"{src}: {type(e).__name__}: {e}")
            continue

        items = _parse_feed(xml_text)
        fetched += len(items)
        for it in items:
            sig = _build_signal(src, it, scan_date)
            if sig is None:
                continue
            if sig.source_content_hash in seen:
                continue
            seen.add(sig.source_content_hash)
            signals.append(sig)

    status = "partial" if warnings else "ok"
    if warnings and not signals:
        status = "error"

    return ScannerResult(
        scanner=NAME,
        status=status,
        signals=signals,
        warnings=warnings,
        fetched_records=fetched,
    )
