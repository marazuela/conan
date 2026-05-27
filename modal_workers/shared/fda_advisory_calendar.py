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

import difflib
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
# v2: payloads now carry drug_candidates + agenda_excerpt (body-text extraction).
# Bumping the key forces a refresh so the new fields populate immediately rather
# than waiting out the 12h TTL on a v1 (candidate-less) cached payload.
CACHE_KEY = "adcom_calendar_v2.json"
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

# FDA AdComm Federal Register notices spell the committee name out in full and
# rarely include the acronym, so acronym-only detection returns None. Map the
# common full names to their acronym for a usable audit trail.
_COMMITTEE_FULLNAMES = (
    ("ONCOLOGIC DRUGS ADVISORY", "ODAC"),
    ("CARDIOVASCULAR AND RENAL DRUGS ADVISORY", "CRDAC"),
    ("PSYCHOPHARMACOLOGIC DRUGS ADVISORY", "PDAC"),
    ("ARTHRITIS ADVISORY", "AAC"),
    ("ANTIMICROBIAL DRUGS ADVISORY", "AMDAC"),
    ("ANTI-INFECTIVE DRUGS ADVISORY", "AIDAC"),
    ("DRUG SAFETY AND RISK MANAGEMENT ADVISORY", "DSaRM"),
    ("GASTROINTESTINAL DRUGS ADVISORY", "GIDAC"),
    ("ENDOCRINOLOGIC AND METABOLIC DRUGS ADVISORY", "EMDAC"),
    ("PERIPHERAL AND CENTRAL NERVOUS SYSTEM DRUGS ADVISORY", "PCNS"),
    ("VACCINES AND RELATED BIOLOGICAL PRODUCTS ADVISORY", "VRBPAC"),
    ("PULMONARY-ALLERGY DRUGS ADVISORY", "PADAC"),
    ("OBSTETRICS, REPRODUCTIVE AND UROLOGIC DRUGS ADVISORY", "ORUDAC"),
    ("ANESTHETIC AND ANALGESIC DRUG PRODUCTS ADVISORY", "AADPAC"),
    ("BLOOD PRODUCTS ADVISORY", "BPAC"),
    ("CELLULAR, TISSUE, AND GENE THERAPIES ADVISORY", "CTGTAC"),
    ("PHARMACY COMPOUNDING ADVISORY", "PCAC"),
    ("DERMATOLOGIC AND OPHTHALMIC DRUGS ADVISORY", "DODAC"),
    ("MEDICAL IMAGING DRUGS ADVISORY", "MIDAC"),
    ("GENETIC METABOLIC DISEASES ADVISORY", "GeMDAC"),
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
    # Trimmed body slice carrying the agenda sentence ("...will discuss NDA
    # NNNNNN, for <drug>, submitted by <company>..."). The drug name is in
    # the document BODY, never the title/abstract, so matching needs this.
    agenda_excerpt: str = ""


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
    for full, acronym in _COMMITTEE_FULLNAMES:
        if full in upper:
            return acronym
    return None


# WHO/USAN International Nonproprietary Name stems — a lowercase word ending in
# one of these is very likely a drug substance, not English prose.
_INN_STEMS = (
    "mab", "nib", "tinib", "ciclib", "sen", "gene", "cel", "tide", "stat",
    "prazole", "parib", "lisib", "degib", "ertib", "afenib", "zomib", "limus",
    "anib", "citinib", "fenacin", "gliptin", "vir", "feron", "kinra", "ept",
    "pride", "sartan", "olol", "azepam",
)

# Words that look brand-like (CamelCase / ALL-CAPS) but are FDA-notice
# boilerplate or committee names — never treat these as a drug.
_BRAND_STOPWORDS = frozenset(
    w.upper() for w in (
        list(_KNOWN_COMMITTEES) + [
            "MEETING", "NOTICE", "ADVISORY", "COMMITTEE", "COMMITTEES",
            "ADMINISTRATION", "DRUG", "DRUGS", "FOOD", "HUMAN", "SERVICES",
            "DEPARTMENT", "AGENCY", "PUBLIC", "BIOLOGICAL", "PRODUCTS",
            "VACCINES", "RELATED", "FORTHCOMING", "GENERAL", "OPEN", "SESSION",
            "CLOSED", "EASTERN", "TIME", "FEDERAL", "REGISTER", "HEALTH",
            "CENTER", "OFFICE", "DIVISION", "PANEL", "TOPICS", "ESTABLISHMENT",
            "AMENDMENT", "AMENDED", "DOCKET", "WEBCAST", "VIRTUAL", "HYBRID",
            "JANUARY", "FEBRUARY", "MARCH", "APRIL", "JUNE", "JULY", "AUGUST",
            "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER", "MONDAY",
            "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY",
        ]
    )
)

# Lowercased fragments that must never act as a standalone match key — they
# are too generic and would mis-assign a meeting to the wrong drug.
_KEY_STOPWORDS = frozenset(
    w.lower() for w in _BRAND_STOPWORDS
) | frozenset((
    "tablets", "capsules", "injection", "solution", "oral", "extended",
    "release", "therapy", "treatment", "indication", "application", "review",
    "disease", "patients", "adult", "adults", "pediatric", "metastatic",
))

# Salt / formulation suffixes stripped from a generic name before tokenizing.
_SALT_SUFFIXES = (
    "hydrochloride", "dihydrochloride", "hcl", "sodium", "potassium",
    "calcium", "sulfate", "sulphate", "acetate", "mesylate", "maleate",
    "citrate", "phosphate", "tartrate", "fumarate", "succinate", "besylate",
    "bromide", "for injection", "for oral suspension", "for oral solution",
    "extended release", "delayed release",
)

_BRAND_TOKEN_RE = re.compile(r"\b([A-Z][A-Za-z0-9][A-Za-z0-9\-]{2,}|[A-Z]{4,})\b")
_PAREN_RE = re.compile(r"\(([^)]{3,})\)")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9\-]{2,}")


def _extract_drug_candidates(text: str) -> List[str]:
    """Heuristically pull likely drug names (brand, generic, INN) out of a
    Federal Register notice's title/abstract/dates/agenda blob.

    Aggressive recall: union of three cheap heuristics, filtered by the
    brand/key stopword sets so committee names and boilerplate don't leak in.
    """
    if not text:
        return []
    out: set[str] = set()

    # 1. Parenthetical generics: "...for Brandname (genericumab)...".
    for m in _PAREN_RE.finditer(text):
        inner = m.group(1).strip().lower()
        for tok in _WORD_RE.findall(inner):
            t = tok.lower()
            if len(t) >= 4 and t not in _KEY_STOPWORDS and t.upper() not in _BRAND_STOPWORDS:
                out.add(t)

    # 2. Brand-like CamelCase / ALL-CAPS tokens.
    for m in _BRAND_TOKEN_RE.finditer(text):
        tok = m.group(1)
        if tok.upper() in _BRAND_STOPWORDS:
            continue
        t = tok.lower()
        if len(t) >= 4 and t not in _KEY_STOPWORDS:
            out.add(t)

    # 3. INN-stem lowercase tokens (catches generics in plain prose).
    for tok in _WORD_RE.findall(text):
        t = tok.lower()
        if len(t) < 5 or t in _KEY_STOPWORDS:
            continue
        if any(t.endswith(stem) for stem in _INN_STEMS):
            out.add(t)

    return sorted(out)


def _strip_salts(name: str) -> str:
    s = name.strip().lower()
    for suf in _SALT_SUFFIXES:
        if s.endswith(" " + suf):
            s = s[: -(len(suf) + 1)].strip()
    return s


def _drug_match_keys(drug_name: str) -> set[str]:
    """Decompose a watchlist drug_name into match keys.

    "FILSPARI (sparsentan)" -> {"filspari", "sparsentan"}
    "drug-a / drug-b"       -> {"drug-a", "drug-b", ...}
    Plus individual word tokens >= 4 chars (combo / multi-word names),
    minus the generic stopword set.
    """
    if not drug_name:
        return set()
    raw = drug_name.strip()
    if not raw or raw == "(auto-discovered)":
        return set()

    pieces: List[str] = []
    paren = _PAREN_RE.search(raw)
    if paren:
        pieces.append(raw[: paren.start()].strip())   # brand
        pieces.append(paren.group(1).strip())          # generic
    else:
        pieces.append(raw)

    keys: set[str] = set()
    for piece in pieces:
        for sub in re.split(r"\s*(?:/|\+|,| and )\s*", piece):
            sub = _strip_salts(sub)
            if not sub:
                continue
            collapsed = sub.replace(" ", "")
            if len(collapsed) >= 4 and collapsed not in _KEY_STOPWORDS:
                keys.add(collapsed)
            for tok in _WORD_RE.findall(sub):
                t = tok.lower()
                if len(t) >= 4 and t not in _KEY_STOPWORDS:
                    keys.add(t)
    return keys


def _match_basis(keys: set[str], haystack: str,
                 candidates: List[str]) -> Optional[str]:
    """Return a short audit string describing how (if at all) any key matched
    the meeting, else None. Precedence: whole-word prose > candidate > fuzzy."""
    cands = [c.lower() for c in (candidates or []) if c]

    for key in keys:
        if re.search(r"\b" + re.escape(key) + r"\b", haystack):
            return f"prose:{key}"
    for key in keys:
        for c in cands:
            if key == c or key in c or c in key:
                return f"candidate:{key}"
    for key in keys:
        for c in cands:
            if difflib.SequenceMatcher(None, key, c).ratio() >= 0.85:
                return f"fuzzy:{key}~{c}"
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


_AGENDA_ANCHORS = (
    "the committee will discuss",
    "committee will discuss",
    "will meet in open session",
    "will discuss",
    "new drug application",
    "biologics license application",
    "to discuss",
)
_BODY_TIMEOUT = 12
_AGENDA_WINDOW = 1400


def _fetch_body_text(url: str, user_agent: str) -> str:
    """Best-effort fetch of a Federal Register notice's plain-text body.
    Never raises — a flaky text endpoint must not break the scanner."""
    if not url:
        return ""
    try:
        r = requests.get(url, headers={"User-Agent": user_agent},
                          timeout=_BODY_TIMEOUT)
        r.raise_for_status()
        return r.text or ""
    except requests.exceptions.RequestException as e:
        logger.warning(f"Federal Register body fetch failed ({url}): {e}")
        return ""


def _agenda_excerpt(body: str) -> str:
    """Slice the agenda sentence(s) out of a notice body. The product/NDA is
    stated here (e.g. '...will discuss NDA 220359, for camizestrant tablets,
    submitted by AstraZeneca...'), never in the title or abstract."""
    if not body:
        return ""
    low = body.lower()
    pos = -1
    for anchor in _AGENDA_ANCHORS:
        pos = low.find(anchor)
        if pos != -1:
            break
    if pos == -1:
        return ""
    start = max(0, pos - 80)
    return re.sub(r"\s+", " ", body[start:start + _AGENDA_WINDOW]).strip()


def fetch_advisory_committee_meetings(
    *,
    lookback_days: int = 30,
    lookahead_days: int = 90,
    client: Optional[SupabaseClient] = None,
    user_agent: str = "InvestmentResearch research@example.com",
) -> List[Meeting]:
    """Fetch FDA advisory-committee meeting notices from the Federal Register.

    For each notice the plain-text body is fetched (best-effort) and the
    agenda slice extracted, since the product/NDA is stated in the body, not
    the title/abstract. Returns Meeting objects with drug_candidates and
    agenda_excerpt populated; callers match against their own watchlist drugs.

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
        # raw_text_url is the plain-text body endpoint — agenda + sponsor live
        # there, not in title/abstract which are FDA-notice boilerplate.
        "fields[]": ["title", "abstract", "dates",
                     "publication_date", "html_url", "type",
                     "document_number", "raw_text_url"],
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
        # The dates string is preferred over the boilerplate abstract because
        # most FDA AdComm notice abstracts don't mention the meeting date.
        dates_field = doc.get("dates") or ""
        # The drug/NDA is in the document BODY, not title/abstract. Pull a
        # trimmed agenda slice so date parsing, committee detection, drug
        # extraction, and watchlist matching all see the substance.
        body = _fetch_body_text(doc.get("raw_text_url") or "", user_agent)
        agenda = _agenda_excerpt(body)
        text_blob = f"{title} {dates_field} {abstract} {agenda}"
        meeting_date = _parse_meeting_date(text_blob)
        committee = _detect_committee(text_blob)
        meetings.append(Meeting(
            publication_date=doc.get("publication_date") or "",
            meeting_date=meeting_date,
            title=title,
            abstract=abstract,
            committee=committee,
            source_url=doc.get("html_url") or "",
            drug_candidates=_extract_drug_candidates(text_blob),
            agenda_excerpt=agenda,
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
        keys = _drug_match_keys(drug)
        if not keys:
            continue

        best: Optional[Meeting] = None
        best_basis: Optional[str] = None
        for m in meetings:
            if not m.meeting_date:
                continue
            md = _safe_iso_to_date(m.meeting_date)
            if md is None or md < today:
                continue
            haystack = f"{m.title} {m.abstract} {m.agenda_excerpt}".lower()
            basis = _match_basis(keys, haystack, m.drug_candidates)
            if basis is None:
                continue
            if best is None or (m.meeting_date < best.meeting_date):
                best = m
                best_basis = basis

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
            f"({best.committee or 'committee'}, {best_basis}) "
            f"per Federal Register {best.source_url}")
        updated.append(ticker)
    return updated
