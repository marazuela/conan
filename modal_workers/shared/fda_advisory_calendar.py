"""
FDA Advisory Committee meeting fetcher backed by the Federal Register API.

Pulls FDA NOTICE-type documents matching "advisory committee meeting" within
a configurable window, parses meeting dates + drug-name candidates from the
title/abstract, and exposes a hydration helper that updates a PDUFA-watchlist
entry's `adcom_date` when its drug name appears in any notice.

Federal Register API:
  https://www.federalregister.gov/api/v1/documents.json
  ?conditions[type][]=NOTICE
  &conditions[agencies][]=food-and-drug-administration
  &conditions[term]=advisory committee meeting
  &per_page=100
  &order=newest

No auth required. Cached via SupabaseClient.read_cache/write_cache under
scanner-caches/fda/adcom_calendar.json with a 12h TTL.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests

from modal_workers.shared.supabase_client import SupabaseClient, SupabaseError

logger = logging.getLogger("fda_advisory_calendar")

FEDERAL_REGISTER_URL = "https://www.federalregister.gov/api/v1/documents.json"
REQUEST_TIMEOUT = 15
CACHE_KEY = "adcom_calendar.json"
CACHE_TTL_S = 12 * 3600  # 12h

# Meeting-date patterns we accept inside the notice title or abstract.
# Federal Register prose is fairly consistent: "is announcing a meeting on
# January 12, 2026" / "meeting will be held on March 4-5, 2026".
_MEETING_DATE_PATTERNS = [
    re.compile(
        r"(?:meeting\s+(?:on|will\s+be\s+held\s+on|scheduled\s+for))\s+"
        r"(\w+\s+\d{1,2}(?:[-–]\d{1,2})?,?\s*\d{4})",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:on|date:)\s+(\w+\s+\d{1,2}(?:[-–]\d{1,2})?,?\s*\d{4})",
        re.IGNORECASE,
    ),
]

# Common Advisory Committee abbreviations used in PDUFA tracking.
_KNOWN_COMMITTEES = (
    "ODAC", "CRDAC", "PDAC", "AMDAC", "DSaRM", "GIDAC", "ADCOM",
    "EMDAC", "PCNS", "Anti-Infective Drugs",
)


@dataclass
class Meeting:
    publication_date: str          # ISO YYYY-MM-DD; document publication
    meeting_date: Optional[str]    # ISO YYYY-MM-DD; parsed from text
    title: str
    abstract: str
    committee: Optional[str]
    source_url: str
    drug_candidates: List[str] = field(default_factory=list)


def _parse_meeting_date(text: str) -> Optional[str]:
    """Return YYYY-MM-DD for the first plausible meeting date in `text`."""
    for pattern in _MEETING_DATE_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        raw = m.group(1).strip()
        # Normalize "March 4-5, 2026" → "March 4, 2026" (use first day).
        raw = re.sub(r"\s+\d{1,2}\s*[-–]\s*\d{1,2}", lambda x: x.group(0).split("-")[0], raw)
        for fmt in ("%B %d, %Y", "%B %d %Y"):
            try:
                return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
    return None


def _detect_committee(text: str) -> Optional[str]:
    upper = text.upper()
    for c in _KNOWN_COMMITTEES:
        if c.upper() in upper:
            return c
    return None


def _read_cache(client: Optional[SupabaseClient]) -> Optional[List[dict]]:
    if client is None:
        return None
    try:
        raw = client.read_cache("fda", CACHE_KEY)
    except SupabaseError:
        return None
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    if time.time() - float(payload.get("cached_at", 0)) > CACHE_TTL_S:
        return None
    return payload.get("meetings") or []


def _write_cache(client: Optional[SupabaseClient], meetings: List[dict]) -> None:
    if client is None:
        return
    try:
        client.write_cache(
            "fda", CACHE_KEY,
            json.dumps({"cached_at": time.time(), "meetings": meetings}).encode("utf-8"),
            content_type="application/json",
        )
    except SupabaseError:
        pass


def fetch_advisory_committee_meetings(
    *,
    lookback_days: int = 30,
    lookahead_days: int = 90,
    client: Optional[SupabaseClient] = None,
    user_agent: str = "InvestmentResearch research@example.com",
) -> List[Meeting]:
    """Fetch FDA advisory-committee meeting notices from the Federal Register.

    Returns a list of Meeting objects (publication_date, meeting_date, title,
    abstract, committee, source_url, drug_candidates). The drug_candidates
    field is empty here — callers match against their own watchlist drugs.

    Read-through cache via Supabase Storage (12h TTL).
    """
    cached = _read_cache(client)
    if cached is not None:
        return [Meeting(**m) for m in cached]

    today = datetime.now(timezone.utc).date()
    lookback_date = (today - timedelta(days=lookback_days)).isoformat()
    params = {
        "conditions[type][]": "NOTICE",
        "conditions[agencies][]": "food-and-drug-administration",
        "conditions[term]": "advisory committee meeting",
        "conditions[publication_date][gte]": lookback_date,
        "per_page": 100,
        "order": "newest",
        # `dates` is NOT in the default response shape — the API only
        # returns it when we ask for it. The actual meeting date lives in
        # the DATES section of the document (e.g. "The meeting will be
        # held on May 28, 2026..."); the abstract is usually generic
        # boilerplate. Without `dates`, _parse_meeting_date matches
        # nothing on most modern FDA AdComm notices.
        "fields[]": ["title", "abstract", "dates",
                     "publication_date", "html_url", "type",
                     "document_number"],
    }
    try:
        resp = requests.get(FEDERAL_REGISTER_URL, params=params,
                            headers={"User-Agent": user_agent},
                            timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException as e:
        logger.warning(f"Federal Register fetch failed: {e}")
        return []

    meetings: List[Meeting] = []
    for doc in data.get("results", []) or []:
        title = doc.get("title") or ""
        abstract = doc.get("abstract") or ""
        # `dates` carries the actual meeting date in structured form
        # ("The meeting will be held on May 28, 2026, from 8:30 a.m.").
        # _parse_meeting_date is regex-based and works on the prose, so
        # we just concatenate the dates string into the parse target.
        # The dates string is preferred over the boilerplate abstract
        # because most FDA AdComm notice abstracts don't mention the date.
        dates_field = doc.get("dates") or ""
        text_blob = f"{title} {dates_field} {abstract}"
        meeting_date = _parse_meeting_date(text_blob)
        committee = _detect_committee(text_blob)
        meetings.append(Meeting(
            publication_date=doc.get("publication_date") or "",
            meeting_date=meeting_date,
            title=title,
            abstract=abstract,
            committee=committee,
            source_url=doc.get("html_url") or "",
        ))

    # Optional client-side filter: drop notices whose meeting_date is more
    # than `lookahead_days` in the future (rare but happens for batch reissues).
    cutoff = today + timedelta(days=lookahead_days)
    filtered = [
        m for m in meetings
        if m.meeting_date is None
        or _safe_iso_to_date(m.meeting_date) is None
        or _safe_iso_to_date(m.meeting_date) <= cutoff
    ]

    _write_cache(client, [m.__dict__ for m in filtered])
    return filtered


def _safe_iso_to_date(s: str):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def hydrate_watchlist_adcom_dates(watchlist: List[Dict[str, Any]],
                                  meetings: List[Meeting]) -> List[str]:
    """Update entry['adcom_date'] when an entry's drug_name appears in a notice's
    title or abstract and the meeting_date is in the future.

    Returns the list of tickers updated. Mutates watchlist in place.
    Skips meetings older than today, blank drug names, and the
    auto-discovered placeholder.
    """
    today = datetime.now(timezone.utc).date()
    updated: List[str] = []

    for entry in watchlist:
        drug = (entry.get("drug_name") or "").strip()
        if not drug or drug == "(auto-discovered)":
            continue
        ticker = entry.get("ticker", "")
        drug_lower = drug.lower()

        best: Optional[Meeting] = None
        for m in meetings:
            if not m.meeting_date:
                continue
            md = _safe_iso_to_date(m.meeting_date)
            if md is None or md < today:
                continue
            haystack = f"{m.title} {m.abstract}".lower()
            if drug_lower not in haystack:
                continue
            if best is None or (m.meeting_date < best.meeting_date):
                best = m

        if best is None:
            continue

        existing = entry.get("adcom_date")
        # Prefer the earliest future AdCom — a later notice should not overwrite
        # an existing earlier one, since the soonest catalyst is what matters.
        if existing and existing <= best.meeting_date:
            continue
        entry["adcom_date"] = best.meeting_date
        entry["notes"] = (entry.get("notes", "") +
            f" | AdCom auto-detected {best.meeting_date} "
            f"({best.committee or 'committee'}) per Federal Register {best.source_url}")
        updated.append(ticker)
    return updated
