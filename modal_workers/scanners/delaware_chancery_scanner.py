"""
Delaware Chancery Docket Scanner.

Surfaces corporate-law events from the Delaware Court of Chancery — the venue
where most US corporate litigation happens (appraisal proceedings, Revlon
claims, § 220 books-and-records demands, fiduciary-duty injunctions, derivative
suits). D-016 redesign: CourtConnect docket-search is the primary target, with
the opinions page as a secondary surface; RSS is removed.

MVP (this implementation):
  - PRIMARY: opinions page (`courts.delaware.gov/opinions/index.aspx?ag=...`).
    Simple HTML table; one row per released opinion; reliably reachable without
    session/cookie management.
  - SECONDARY: CourtConnect docket-search stub — flagged as blocked on
    CAPTCHA/session-cookie handling (Q-002 per lit_delaware_chancery.md:46).
    Degrade gracefully: return partial with a warning if we cannot walk the
    docket search, but never fail the opinions coverage.

Signal types (strategy_spec lines 33-38):
  - chancery_appraisal_filed
  - chancery_books_and_records_demand
  - chancery_revlon_claim_filed
  - chancery_motion_to_expedite_granted
  - chancery_injunction_granted_blocking_deal
  - chancery_opinion_released (generic fallback)

Dedup key: (case_number, signal_type, source_date) — Delaware case numbers are
uniquely formatted (e.g., "2026-0123-AGB").

Scoring profile: `litigation`. dim_estimator returns None for the profile, so
signals land unscored and route through signal_resolver for AI dim assignment.
The litigation auto-cap (party_resolution_confidence < 3 → archive) catches
weak party-resolution rows.

Per-host UA (D-015): Delaware hosts get a browser-style UA to survive WAF
gating. The general SEC_USER_AGENT is NOT propagated to courts.delaware.gov.

IO contract:
  scan(cfg: ScannerConfig) -> ScannerResult
    - No auth required (public web).
    - Uses cfg.timeout_soft_s as wall-clock budget (~90s daily cadence).
    - Always attempts opinions; CourtConnect is best-effort only.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple

import requests

from modal_workers.shared.scanner_base import ScannerResult, Signal
from modal_workers.shared.supabase_client import (
    EntityHints,
    ScannerConfig,
    SupabaseClient,
)

NAME = "delaware_chancery_scanner"

REQUEST_TIMEOUT = 15
DEFAULT_WALL_CLOCK_S = 90

# D-015 per-host UA rule — Delaware courts site gets a browser-style UA (it
# blocks non-browser traffic more aggressively than SEC). The operational
# User-Agent used elsewhere in Conan is not propagated.
DELAWARE_BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

OPINIONS_URL = (
    "https://courts.delaware.gov/opinions/index.aspx"
    "?ag=court%20of%20chancery"
)

# Delaware case-number pattern: YYYY-NNNN-XXX (year, sequence, judge initials).
CASE_NUMBER_RE = re.compile(r"\b(\d{4}-\d{3,5}-[A-Z]{2,4})\b")

# Caption-driven signal classifier. Tuples of (regex, signal_type), first match
# wins. Generic `chancery_opinion_released` is the fallback.
#
# Order matters: more-specific patterns before less-specific. `books-and-records`
# must beat the generic `§ 220` check, for example.
_CAPTION_CLASSIFIERS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"\bappraisal\b", re.IGNORECASE), "chancery_appraisal_filed"),
    (re.compile(r"\brevlon\b", re.IGNORECASE), "chancery_revlon_claim_filed"),
    (re.compile(r"books\s+and\s+records", re.IGNORECASE), "chancery_books_and_records_demand"),
    (re.compile(r"\bsection\s+220\b|\b§\s*220\b|\bDGCL\s+220\b", re.IGNORECASE),
     "chancery_books_and_records_demand"),
    (re.compile(r"motion\s+to\s+expedite", re.IGNORECASE),
     "chancery_motion_to_expedite_granted"),
    (re.compile(r"preliminary\s+injunction|injunction\s+(granted|issued)|enjoin(ing|ed)?\s+.*merger",
                re.IGNORECASE),
     "chancery_injunction_granted_blocking_deal"),
]


@dataclass
class _Opinion:
    case_number: str
    case_caption: str
    release_date: str     # YYYY-MM-DD
    opinion_url: str      # deep link to PDF
    matter_type: str      # appraisal | books_and_records | revlon | injunction | other
    raw_row: str          # kept for audit + raw_payload
    opinion_title: str = ""  # court-assigned opinion title (e.g. "Letter Report
                             # Denying Inspection of Books and Records"). Distinct
                             # from case_caption ("Party v. Party") — both are
                             # classified for signal_type matching.


# ---------------------------------------------------------------------------
# HTML parsing — the opinions index page is a table; we pull each row and
# extract case_number / caption / release_date / PDF link.
# ---------------------------------------------------------------------------

class _OpinionsTableParser(HTMLParser):
    """Extract opinion rows from the Chancery opinions HTML table.

    The page's structure is a single <table> with rows — each row has columns
    for date, case name, case number, and a PDF link. We collect rows as dicts
    of {col_texts[], links[]} and let the caller interpret.
    """

    def __init__(self):
        super().__init__()
        self._in_table = False
        self._in_row = False
        self._in_cell = False
        self._in_a = False
        self._current_row: Optional[Dict[str, Any]] = None
        self._current_cell_text: List[str] = []
        self._current_a_href: Optional[str] = None
        self.rows: List[Dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]):
        tag = tag.lower()
        attrs_dict = dict(attrs)
        if tag == "table":
            self._in_table = True
        elif self._in_table and tag == "tr":
            self._in_row = True
            self._current_row = {"cells": [], "links": []}
        elif self._in_row and tag in ("td", "th"):
            self._in_cell = True
            self._current_cell_text = []
        elif self._in_row and tag == "a":
            self._in_a = True
            href = attrs_dict.get("href")
            if href:
                self._current_a_href = href
                self._current_row["links"].append(href)

    def handle_endtag(self, tag: str):
        tag = tag.lower()
        if tag == "table":
            self._in_table = False
        elif tag == "tr" and self._in_row:
            self._in_row = False
            if self._current_row and self._current_row["cells"]:
                self.rows.append(self._current_row)
            self._current_row = None
        elif tag in ("td", "th") and self._in_cell:
            text = " ".join(self._current_cell_text).strip()
            text = re.sub(r"\s+", " ", text)
            if self._current_row is not None:
                self._current_row["cells"].append(text)
            self._in_cell = False
            self._current_cell_text = []
        elif tag == "a":
            self._in_a = False
            self._current_a_href = None

    def handle_data(self, data: str):
        if self._in_cell:
            self._current_cell_text.append(data)


_CAPTION_SHAPE_RE = re.compile(
    r"\bv\.\s|\s+v\s+|\bin\s+re\b|"
    r"\b(Inc\.?|Corp\.?|LLC|Ltd\.?|LP|L\.P\.?|Company|Co\.?|Holdings?|"
    r"Partners|Trust|Stockholders?)\b",
    re.IGNORECASE,
)


def _looks_like_caption(text: str) -> bool:
    """Heuristic: does this cell text look like a Chancery case caption?

    A caption typically contains 'v.', 'In re', or a corporate entity suffix.
    Opinion titles ('Letter Report Denying...', 'Memorandum Opinion') do NOT
    match. This lets us prefer the real caption column over a long opinion-
    title column when both are present.
    """
    return bool(text) and bool(_CAPTION_SHAPE_RE.search(text))


_DATE_PATTERNS = [
    re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$"),
    re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$"),
]


# Court-table boilerplate cells we explicitly skip when locating the opinion_title.
# Covers "Court of Chancery" / "Supreme Court" (column 3 in the live layout),
# "Civil" / "Criminal" case-type tags (column 4), and typical Delaware judge
# abbreviations (column 5) which are short and end in "J.", "V.C.", "C.J.", etc.
_IS_METADATA_CELL = re.compile(
    r"^(?:"
    r"Court\s+of\s+Chancery|"
    r"Supreme\s+Court(?:\s+of\s+Delaware)?|"
    r"Superior\s+Court|"
    r"Court\s+of\s+Common\s+Pleas|"
    r"Family\s+Court|"
    r"Civil|Criminal|Juvenile|"
    r"[A-Z][a-z]+(?:\s+[A-Z][a-z]*)*\s+(?:J\.|V\.C\.|C\.J\.|Chancellor|Judge)"
    r")\.?$",
    re.IGNORECASE,
)


def _normalize_date(cell: str) -> str:
    """Return YYYY-MM-DD from a date cell, or '' if not a date."""
    s = cell.strip()
    for pat in _DATE_PATTERNS:
        m = pat.match(s)
        if not m:
            continue
        if pat.pattern.startswith("^(\\d{4})"):
            y, mm, dd = m.group(1), m.group(2).zfill(2), m.group(3).zfill(2)
        else:
            mm, dd, y = m.group(1).zfill(2), m.group(2).zfill(2), m.group(3)
        return f"{y}-{mm}-{dd}"
    return ""


def _parse_opinions_html(html: str) -> List[_Opinion]:
    """Parse opinions page HTML → list of _Opinion records.

    The courts.delaware.gov opinions index table has a firm column structure:
      cell[0]: case_caption        ("Siyuan Ma v. iShopShops, Inc.")
      cell[1]: release_date        ("04/17/2026")
      cell[2]: case_number         ("C.A. No. 2025-1499-CDW")
      cell[3]: court name
      cell[4]: case type           ("Civil")
      cell[5]: judge
      cell[6]: opinion_title       ("Letter Report Denying Inspection...")

    We extract by role rather than position so the parser remains robust if
    the site re-orders columns: caption is the first cell matching
    `_looks_like_caption`; date is the first cell matching `_DATE_PATTERNS`;
    case_number is extracted via `CASE_NUMBER_RE` from any cell; opinion_title
    is the last non-trivial cell that's distinct from all of those.
    """
    parser = _OpinionsTableParser()
    parser.feed(html)
    opinions: List[_Opinion] = []

    for row in parser.rows:
        cells = [c for c in row["cells"] if c]
        if not cells:
            continue

        # Case number anywhere in the row (often wrapped in "C.A. No. ...").
        case_number = ""
        case_number_cell_idx: Optional[int] = None
        for i, cell in enumerate(cells):
            m = CASE_NUMBER_RE.search(cell)
            if m:
                case_number = m.group(1)
                case_number_cell_idx = i
                break
        if not case_number:
            continue

        # Date cell.
        release_date = ""
        date_cell_idx: Optional[int] = None
        for i, cell in enumerate(cells):
            d = _normalize_date(cell)
            if d:
                release_date = d
                date_cell_idx = i
                break

        # Caption: first cell that looks like a caption.
        caption = ""
        caption_cell_idx: Optional[int] = None
        for i, cell in enumerate(cells):
            if _looks_like_caption(cell):
                caption = cell
                caption_cell_idx = i
                break
        # Fallback: longest non-date, non-case-number cell (legacy behavior).
        if not caption:
            candidates = [
                (i, c) for i, c in enumerate(cells)
                if i not in (case_number_cell_idx, date_cell_idx)
            ]
            if candidates:
                caption_cell_idx, caption = max(candidates, key=lambda p: len(p[1]))

        # Opinion title: walk cells right-to-left for the first cell that's
        # distinct from caption/date/case-number and isn't a known boilerplate
        # metadata cell (court name, case-type tag, judge name). The Chancery
        # opinions table fixes opinion_title at the last column, but we find
        # it by exclusion so the parser survives if the site re-orders.
        opinion_title = ""
        reserved = {caption_cell_idx, date_cell_idx, case_number_cell_idx}
        for i in range(len(cells) - 1, -1, -1):
            if i in reserved:
                continue
            c = cells[i]
            if _normalize_date(c) or CASE_NUMBER_RE.search(c):
                continue
            if _IS_METADATA_CELL.match(c):
                continue
            opinion_title = c
            break

        # Opinion URL.
        opinion_url = ""
        for link in row["links"]:
            if link.lower().endswith(".pdf") or "opinions" in link.lower() \
                    or "download.aspx" in link.lower():
                opinion_url = link if link.startswith("http") \
                    else "https://courts.delaware.gov" + (
                    link if link.startswith("/") else "/" + link)
                break

        # Classify on caption + opinion_title combined — the caption is often
        # just "Party v. Party" with no matter-type keywords, but the opinion
        # title reliably names the subject matter ("Books and Records",
        # "Appraisal", etc.).
        classify_text = f"{caption} {opinion_title}".strip()
        matter_type, _ = _classify_caption(classify_text)

        opinions.append(_Opinion(
            case_number=case_number,
            case_caption=caption,
            release_date=release_date,
            opinion_url=opinion_url,
            matter_type=matter_type,
            raw_row=" | ".join(cells),
            opinion_title=opinion_title,
        ))
    return opinions


def _classify_caption(caption: str) -> Tuple[str, str]:
    """Return (matter_type, signal_type).

    matter_type follows the strategy spec's raw_data.matter_type convention
    (appraisal / books_and_records / revlon / 220 / other). signal_type is the
    emitted Signal.signal_type.
    """
    if not caption:
        return ("other", "chancery_opinion_released")
    for pat, sig_type in _CAPTION_CLASSIFIERS:
        if pat.search(caption):
            matter = {
                "chancery_appraisal_filed": "appraisal",
                "chancery_books_and_records_demand": "books_and_records",
                "chancery_revlon_claim_filed": "revlon",
                "chancery_motion_to_expedite_granted": "motion_to_expedite",
                "chancery_injunction_granted_blocking_deal": "injunction",
            }.get(sig_type, "other")
            return (matter, sig_type)
    return ("other", "chancery_opinion_released")


# ---------------------------------------------------------------------------
# Party resolution — extract the corporate party from Chancery case captions.
# ---------------------------------------------------------------------------

# Chancery caption idioms we strip to isolate the corporate party (strategy spec
# line 48 — F-12 flags captions as idiosyncratic).
_CAPTION_CLEANUPS = [
    re.compile(r"^in\s+re\s+", re.IGNORECASE),
    re.compile(r"\bstockholders?\s+litigation\b", re.IGNORECASE),
    re.compile(r"\bderivative\s+litigation\b", re.IGNORECASE),
    re.compile(r"\bsecurities\s+litigation\b", re.IGNORECASE),
    re.compile(r"\bappraisal\s+(proceedings?|of)\b", re.IGNORECASE),
]


def _extract_party_name(caption: str) -> str:
    """Heuristically extract a corporate party name from a Chancery caption.

    'In re XYZ Corp. Stockholders Litigation' → 'XYZ Corp.'
    'Smith v. ABC, Inc.' → 'ABC, Inc.'
    'XYZ Corp. v. Smith' → 'XYZ Corp.'
    """
    if not caption:
        return ""
    s = caption.strip()
    for pat in _CAPTION_CLEANUPS:
        s = pat.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip(" ,")
    # Split at 'v.' and prefer whichever side looks like a corporate name (has
    # an entity suffix like Inc, Corp, LLC).
    parts = re.split(r"\s+v\.?\s+", s, maxsplit=1, flags=re.IGNORECASE)
    entity_re = re.compile(r"\b(Inc\.?|Corp\.?|LLC|Ltd\.?|LP|Company|Co\.?|Holdings?)\b",
                           re.IGNORECASE)
    if len(parts) == 2:
        left, right = parts
        left_hit = entity_re.search(left)
        right_hit = entity_re.search(right)
        if right_hit and not left_hit:
            return right.strip()
        if left_hit and not right_hit:
            return left.strip()
        # Both or neither: prefer the one that looks more like a company.
        return (right if right_hit else left).strip()
    return s


# ---------------------------------------------------------------------------
# Dedup / hash helpers
# ---------------------------------------------------------------------------

def _content_hash(case_number: str, signal_type: str, source_date: str) -> str:
    key = f"chancery|{case_number}|{signal_type}|{source_date}"
    return "sha256:" + hashlib.sha256(key.encode()).hexdigest()


def _signal_id(case_number: str, signal_type: str, source_date: str) -> str:
    key = f"chancery|{case_number}|{signal_type}|{source_date}"
    return "chancery_" + hashlib.sha256(key.encode()).hexdigest()[:24]


# ---------------------------------------------------------------------------
# HTTP fetch with graceful degradation
# ---------------------------------------------------------------------------

def _fetch_opinions_html() -> Tuple[Optional[str], Optional[str]]:
    """Return (html, error_message). error_message is non-None on failure."""
    try:
        resp = requests.get(
            OPINIONS_URL,
            headers={"User-Agent": DELAWARE_BROWSER_UA, "Accept": "text/html"},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            return None, f"opinions http {resp.status_code}"
        return resp.text, None
    except requests.exceptions.RequestException as e:
        return None, f"opinions {type(e).__name__}: {e}"


# CourtConnect is a stubbed secondary surface per D-016. The docket-search has
# a two-hop disclaimer flow and returns frameset HTML; building it is Q-002 in
# the strategy spec. The stub returns (None, warning) so the opinions coverage
# still lands.
def _fetch_courtconnect_filings(*, date_from: str, date_to: str
                                 ) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """D-016 secondary surface — CourtConnect docket-search.

    Not yet implemented (Q-002). Returns (None, "deferred") so scan() proceeds
    with opinions-only coverage and emits an explicit warning rather than
    masking the gap.
    """
    return None, "courtconnect docket-search deferred (Q-002 session/frameset handling)"


# ---------------------------------------------------------------------------
# scan entrypoint
# ---------------------------------------------------------------------------

def scan(cfg: ScannerConfig) -> ScannerResult:
    client = SupabaseClient()

    budget_s = max(20, (cfg.timeout_soft_s or DEFAULT_WALL_CLOCK_S) - 5)
    scan_start = time.time()
    scan_date = datetime.now(timezone.utc)
    lookback_days = int(cfg.config.get("opinions_lookback_days", 14))
    cutoff_date = (scan_date - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

    warnings: List[str] = []
    signals: List[Signal] = []
    fetched = 0
    filtered_by_date = 0

    # ---- 1. Primary surface: opinions page ------------------------------------
    html, err = _fetch_opinions_html()
    if err:
        warnings.append(err)
    elif html:
        try:
            opinions = _parse_opinions_html(html)
            fetched = len(opinions)
        except Exception as e:  # noqa: BLE001
            warnings.append(f"opinions parse: {type(e).__name__}: {e}")
            opinions = []
        for op in opinions:
            if time.time() - scan_start > budget_s * 0.8:
                warnings.append("opinions: soft budget reached")
                break
            if op.release_date and op.release_date < cutoff_date:
                filtered_by_date += 1
                continue
            signal = _build_signal(op, client=client, scan_date=scan_date)
            if signal is not None:
                signals.append(signal)

    # ---- 2. Secondary surface: CourtConnect docket-search (stubbed) ----------
    dfrom = cutoff_date
    dto = scan_date.strftime("%Y-%m-%d")
    cc_rows, cc_err = _fetch_courtconnect_filings(date_from=dfrom, date_to=dto)
    if cc_err:
        warnings.append(cc_err)
    # No emission from CourtConnect yet.

    status: str
    if not html and cc_rows is None:
        status = "error"  # both surfaces failed
    elif warnings:
        status = "partial"
    else:
        status = "ok"

    return ScannerResult(
        scanner=NAME,
        status=status,
        signals=signals,
        warnings=warnings,
        fetched_records=fetched,
        run_metrics={
            "opinions_parsed": fetched,
            "opinions_filtered_by_date": filtered_by_date,
            "signals_emitted": len(signals),
            "courtconnect_deferred": cc_err is not None,
        },
    )


def _build_signal(op: _Opinion, *, client: SupabaseClient,
                  scan_date: datetime) -> Optional[Signal]:
    """Convert a parsed _Opinion into a Signal or None if unemittable."""
    if not op.case_number:
        return None
    # Classify on caption + opinion_title combined (captions are often just
    # "Party v. Party" with no matter keywords).
    classify_text = f"{op.case_caption} {op.opinion_title}".strip()
    matter_type, signal_type = _classify_caption(classify_text)

    # source_date — use release_date when known, else scan_date.
    try:
        source_date = datetime.strptime(
            op.release_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        source_date = scan_date

    party_name = _extract_party_name(op.case_caption)

    src_hash = _content_hash(op.case_number, signal_type,
                             op.release_date or scan_date.strftime("%Y-%m-%d"))
    sig_id = _signal_id(op.case_number, signal_type,
                        op.release_date or scan_date.strftime("%Y-%m-%d"))

    raw_payload: Dict[str, Any] = {
        "court": "Delaware Court of Chancery",
        "signal_category": "delaware_chancery",
        "chancery_case_number": op.case_number,
        "case_caption": op.case_caption,
        "opinion_title": op.opinion_title,
        "matter_type": matter_type,
        "opinion_url": op.opinion_url,
        "release_date": op.release_date,
        "party_raw_name": party_name,
        "raw_row": op.raw_row,
        # M&A context is filled in downstream by signal_resolver if the caption
        # references a ticker or by legal_enricher via caption → issuer lookup.
        "m_and_a_context": None,
    }

    entity_hints = EntityHints(
        issuer_figi=None,
        ticker=None,
        mic=None,
        cik=None,
        name=party_name or None,
        country="US",
    )

    return Signal(
        signal_id=sig_id,
        source_content_hash=src_hash,
        source_date=source_date,
        scan_date=scan_date,
        signal_type=signal_type,
        raw_payload=raw_payload,
        source_url=op.opinion_url or OPINIONS_URL,
        issuer_figi=None,
        entity_hints=entity_hints,
        # Chancery opinions are often mixed-direction (e.g., appraisal might be
        # long if the court signals a price bump, short if it sides with the
        # deal). Leave direction unset — signal_resolver determines during
        # thesis review.
        thesis_direction=None,
        strength_estimate=None,
    )
