"""bc_pdufa_extract — pure (no-I/O) PDUFA-date + designation parser for the BC monitor.

Lifted and hardened from ``modal_workers/scanners/fda_pdufa_pipeline.py::_parse_filing_for_pdufa``
(and its ``_extract_drug_name`` helper) so the extraction logic is unit-testable against
fixtures with zero network. The scanner version fetches the filing body itself; this module
takes already-fetched text and is therefore deterministic and side-effect-free.

Phase 0 / approach-1 hardening over the scanner regex (per the spike spec §1):

  1. **Context-anchored date.** The date must appear within ~200 chars of a PDUFA token
     (``PDUFA`` / ``goal date`` / ``action date`` / ``target action date``). This is the
     central guard against grabbing an unrelated date elsewhere in the filing (audit F-114
     style false positives). A standalone date with no nearby PDUFA keyword is rejected.

  2. **Three date formats, normalized to ISO.** ``Month DD, YYYY`` (``%B %d, %Y``, comma
     optional), ``MM/DD/YYYY`` (and ``M/D/YYYY``), and ISO ``YYYY-MM-DD``. The scanner only
     handled the long ``%B %d, %Y`` form; numeric + ISO are added here because 8-K exhibits
     and tabular disclosures use them.

  3. **Designation keyword scan (new).** Best-effort booleans from the same filing body:
     ``Breakthrough Therapy`` -> ``has_bt``, ``Fast Track`` -> ``has_ft``,
     ``Accelerated Approval`` -> ``has_aa``. **Absence yields ``None`` (unknown), never
     ``False``** — matching ``feature_assembly._designations`` semantics where a missing
     designation is "unknown," not "negative." An 8-K announcing a PDUFA date frequently
     restates these designations, but many omit them, so the signal is intentionally soft.

Public surface
--------------
``extract_pdufa(text) -> PdufaExtract``
    The one entry point. ``text`` is whitespace-collapsed plain filing text (the shape
    ``edgar_efts.fetch_filing_text`` returns). Returns a ``PdufaExtract`` dataclass; every
    field may be None / unknown.

``PdufaExtract``
    ``pdufa_date_iso`` (str|None), ``drug_name`` (str|None),
    ``has_bt`` / ``has_ft`` / ``has_aa`` (Optional[bool] — None == unknown),
    ``appl_type_hint`` ('BLA'|'NDA'|None — keyword-derived, best-effort).

The module imports nothing from the codebase (pure stdlib ``re``/``datetime``) so it can be
unit-tested in isolation and reused by the enumerator without dragging in Supabase/requests.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple

# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------


@dataclass
class PdufaExtract:
    """Result of parsing a filing body for PDUFA payload.

    All fields are optional. Designation booleans use Optional[bool] so the
    caller can distinguish "the filing stated this designation" (True) from
    "the filing did not mention it" (None == unknown). We never emit False —
    a non-mention is not evidence of absence.
    """

    pdufa_date_iso: Optional[str] = None
    drug_name: Optional[str] = None
    has_bt: Optional[bool] = None
    has_ft: Optional[bool] = None
    has_aa: Optional[bool] = None
    appl_type_hint: Optional[str] = None  # 'BLA' | 'NDA' | None


# ---------------------------------------------------------------------------
# Date extraction — context-anchored
# ---------------------------------------------------------------------------

# PDUFA "anchor" tokens. The extracted date must sit within _ANCHOR_WINDOW
# characters of one of these (measured from the start of the anchor match to
# the start of the date match) for the date to be accepted.
_ANCHOR_RE = re.compile(
    r"(?:\bPDUFA\b|\bgoal\s+date\b|\baction\s+date\b|\btarget\s+action\b)",
    re.IGNORECASE,
)
_ANCHOR_WINDOW = 200  # chars between a PDUFA anchor and the date

# Three accepted date surface forms. Each capturing group 1 is the raw date string.
#   _DATE_LONG    : "January 5, 2026" / "Jan 5 2026" (comma optional, abbrev ok)
#   _DATE_NUMERIC : "01/05/2026" / "1/5/2026"
#   _DATE_ISO     : "2026-01-05"
_MONTHS = (
    r"(?:January|February|March|April|May|June|July|August|September|October|"
    r"November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)"
)
_DATE_LONG = re.compile(rf"\b({_MONTHS}\.?\s+\d{{1,2}},?\s+\d{{4}})\b", re.IGNORECASE)
_DATE_NUMERIC = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{4})\b")
_DATE_ISO = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")

# Long-form parse attempts. We strip a trailing "." from abbreviations
# ("Sept." -> "Sept") and a stray comma before parsing.
_LONG_FORMATS = ("%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y")
_NUMERIC_FORMATS = ("%m/%d/%Y",)


def _normalize_long(raw: str) -> Optional[str]:
    cleaned = raw.replace(".", "").strip()
    # "Sept" is a common abbreviation strptime doesn't know — map to "Sep".
    cleaned = re.sub(r"\bSept\b", "Sep", cleaned, flags=re.IGNORECASE)
    for fmt in _LONG_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _normalize_numeric(raw: str) -> Optional[str]:
    for fmt in _NUMERIC_FORMATS:
        try:
            return datetime.strptime(raw.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _normalize_iso(raw: str) -> Optional[str]:
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return None


def _all_date_matches(text: str) -> List[Tuple[int, str]]:
    """Return [(start_index, iso_date)] for every parseable date in `text`,
    across all three surface forms. Order is by appearance (start_index)."""
    out: List[Tuple[int, str]] = []
    for m in _DATE_LONG.finditer(text):
        iso = _normalize_long(m.group(1))
        if iso:
            out.append((m.start(1), iso))
    for m in _DATE_NUMERIC.finditer(text):
        iso = _normalize_numeric(m.group(1))
        if iso:
            out.append((m.start(1), iso))
    for m in _DATE_ISO.finditer(text):
        iso = _normalize_iso(m.group(1))
        if iso:
            out.append((m.start(1), iso))
    out.sort(key=lambda t: t[0])
    return out


def extract_pdufa_date(text: str) -> Optional[str]:
    """Return the ISO PDUFA date anchored to a PDUFA keyword, or None.

    Algorithm: find every PDUFA anchor span and every parseable date span; accept
    the FIRST date whose start index lies within [anchor_start, anchor_start +
    _ANCHOR_WINDOW] of some anchor (i.e. the date follows a PDUFA token within
    ~200 chars). Looking forward-only from the anchor mirrors how IR copy reads
    ("PDUFA goal date of January 5, 2026"), and avoids matching a date that merely
    precedes an unrelated later 'PDUFA' mention.
    """
    if not text:
        return None
    anchors = [m.start() for m in _ANCHOR_RE.finditer(text)]
    if not anchors:
        return None
    dates = _all_date_matches(text)
    if not dates:
        return None
    for date_start, iso in dates:
        for a_start in anchors:
            # date must appear at or after the anchor, within the window
            if a_start <= date_start <= a_start + _ANCHOR_WINDOW:
                return iso
    return None


# ---------------------------------------------------------------------------
# Drug name extraction — lifted verbatim from fda_pdufa_pipeline._extract_drug_name
# (INN-suffix pass, then code-form fallback). Kept byte-identical so behavior
# matches the scanner the spike is replacing.
# ---------------------------------------------------------------------------

_INN_SUFFIXES = (
    # Monoclonal antibodies
    "mab", "zumab", "ximab", "umab", "lumab",
    # Kinase / pathway inhibitors
    "tinib", "afenib", "rafenib", "lisib", "ciclib", "sertib",
    # Statins
    "vastatin",
    # Antivirals (specific class stems only)
    "navir", "tegravir", "ciclovir", "fovir", "buvir", "asvir", "pravir", "cabir",
    # GLP-1 / peptide therapeutics
    "glutide", "lutide", "tide",
    # Antifungals / antiparasitics
    "conazole", "prazole",
    # Cardiovascular
    "sartan", "olol", "dipine",
    # Diabetes
    "formin", "gliflozin", "gliptin",
    # CNS
    "azepam", "azolam", "melteon", "stigmine",
    # Cytokines / immunomodulators
    "kira", "leukin", "cept",
    # Oligonucleotides / RNA therapeutics
    "rsen", "drisen", "siran", "mersen",
    # Cortisol receptor antagonists
    "corilant",
    # Renin inhibitors
    "kiren",
)
_DRUG_NAME_RE = re.compile(
    r"\b([A-Za-z]{3,20}(?:" + "|".join(_INN_SUFFIXES) + r"))\b",
)
_DRUG_CODE_RE = re.compile(r"\b([A-Z]{2,5}-?\d{2,5})\b")
_DRUG_NAME_BLOCKLIST = {
    "report", "import", "support", "account", "amount", "submit",
    "permit", "consult", "result", "default", "agreement",
}

# Exhibit / filing residue the code-form regex grabs from 8-K bodies ("EX-99",
# "EX-99.1") plus generic filler. These are NOT drug names — they're the
# best-known false positives of the code-form fallback (memory
# `fda_adcom_capture.md`: pdufa_watchlist polluted with "EX-99"). We reject them
# so the caller gets None (unknown drug) rather than a junk slug.
_CODE_JUNK_RE = re.compile(r"^EX[-_]?\d", re.IGNORECASE)
_DRUG_CODE_BLOCKLIST = {"concept", "ex-99", "ex99"}


def extract_drug_name(text: str) -> Optional[str]:
    """Return the most likely drug name from a filing body, or None.

    Two-pass: INN-suffix match wins; else a code form (VK2735 / AXS-05). Exhibit
    residue ("EX-99") and generic filler are rejected (-> None) so downstream
    surrogate-appno slugs don't get polluted. Confined to the first 20 KB of text
    since drug names appear in the lede.
    """
    head = text[:20_000]
    for m in _DRUG_NAME_RE.finditer(head):
        candidate = m.group(1)
        if candidate.lower() in _DRUG_NAME_BLOCKLIST:
            continue
        if candidate.lower() in _DRUG_CODE_BLOCKLIST:
            continue
        return candidate
    m = _DRUG_CODE_RE.search(head)
    if m:
        candidate = m.group(1)
        if _CODE_JUNK_RE.match(candidate) or candidate.lower() in _DRUG_CODE_BLOCKLIST:
            return None
        return candidate
    return None


# ---------------------------------------------------------------------------
# Designation extraction — NEW for the BC path. Best-effort booleans.
# ---------------------------------------------------------------------------

# Each designation: a regex; a match -> True, no match -> None (unknown).
# Patterns are tolerant of common phrasings ("Breakthrough Therapy designation",
# "granted Breakthrough Therapy", "BTD"). We keep them tight enough that an
# unrelated word doesn't trip them.
_BT_RE = re.compile(
    r"\bBreakthrough\s+Therapy\b|\bBreakthrough\s+Therapy\s+Designation\b|\bBTD\b",
    re.IGNORECASE,
)
_FT_RE = re.compile(
    r"\bFast\s+Track\b",
    re.IGNORECASE,
)
_AA_RE = re.compile(
    # "Accelerated Approval" — the regulatory pathway. Guard against the bare
    # word "accelerated" by requiring "approval"/"pathway" adjacency.
    r"\bAccelerated\s+Approval\b",
    re.IGNORECASE,
)

# appl_type hint: BLA if biologics-license keywords present, else NDA if an NDA
# keyword is present. Neither -> None (the enumerator defaults to NDA when it
# must synthesize, but the parser itself reports only what the text supports).
_BLA_RE = re.compile(r"\bbiologics?\s+license\s+application\b|\bBLA\b", re.IGNORECASE)
_NDA_RE = re.compile(r"\bnew\s+drug\s+application\b|\bNDA\b", re.IGNORECASE)


def _designation_flag(text: str, pattern: re.Pattern) -> Optional[bool]:
    """True when the pattern matches; None (unknown) when it does not.
    Never returns False — non-mention is not evidence of absence."""
    return True if pattern.search(text) else None


def extract_designations(text: str) -> Tuple[Optional[bool], Optional[bool], Optional[bool]]:
    """Return (has_bt, has_ft, has_aa) as Optional[bool]; None == unknown."""
    if not text:
        return (None, None, None)
    return (
        _designation_flag(text, _BT_RE),
        _designation_flag(text, _FT_RE),
        _designation_flag(text, _AA_RE),
    )


def extract_appl_type_hint(text: str) -> Optional[str]:
    """Best-effort 'BLA'|'NDA'|None from filing keywords. BLA takes precedence
    when both appear (a BLA filing often references the NDA framework)."""
    if not text:
        return None
    if _BLA_RE.search(text):
        return "BLA"
    if _NDA_RE.search(text):
        return "NDA"
    return None


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


def extract_pdufa(text: Optional[str]) -> PdufaExtract:
    """Parse a whitespace-collapsed filing body into a PdufaExtract.

    Pure: no network, no clock dependence beyond date normalization. Safe to
    call on None / empty (returns an all-None PdufaExtract).
    """
    if not text:
        return PdufaExtract()
    bt, ft, aa = extract_designations(text)
    return PdufaExtract(
        pdufa_date_iso=extract_pdufa_date(text),
        drug_name=extract_drug_name(text),
        has_bt=bt,
        has_ft=ft,
        has_aa=aa,
        appl_type_hint=extract_appl_type_hint(text),
    )
