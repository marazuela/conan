"""bcfda.outcomes.resolve — regulatory-outcome resolution for the outcome labeler.

Phase 3 §5.1 / §5.4 / §5.5. Decide a resolved PDUFA's regulatory verdict from the
most-authoritative source available, derive the band-vs-reality ``hypothesis_outcome``,
and detect PDUFA-date pushes.

Source precedence (most authoritative first, §5.1):
  1. CRL Transparency (``bcfda.universe.openfda_crl_transparency``): a record whose
     digit-normalized application_number matches AND letter_year >= pdufa_year -> ``crl``.
     ⚠️ The transparency module's FETCH path needs the network; the labeler passes the
     already-parsed CRL records in, so this module is pure. If the module is not
     importable / no records are supplied, the CRL branch is skipped and the caller
     logs ``crl_source_unavailable`` (the approvals path works independently).
  2. Drugs@FDA (``extract_submission_rows``-shaped submissions): an ORIG submission
     ``submission_status='AP'`` dated >= pdufa_date -> ``approved``; a ``WD`` -> ``withdrawn``.
  3. PDUFA extension (§5.5): pdufa_date advanced vs the last-seen value, no terminal
     verdict -> ``extended`` (NON-terminal; overwrite-eligible by the merge upsert).

ALL regulatory_outcome values are LOWERCASE (the verified CHECK §0.2:
{approved, crl, withdrawn, extended}) — emit lowercase, never ``CRL``/``Approved``.

This module is PURE (no I/O): the caller fetches CRL records + Drugs@FDA submissions
and passes them in, so resolution is unit-testable against fixtures with no network.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger("bcfda.outcomes.resolve")

# Terminal vs non-terminal verdicts. 'extended' is the only non-terminal one —
# the bet is still live, just later; it stays overwrite-eligible (§5.5).
TERMINAL_OUTCOMES = frozenset({"approved", "crl", "withdrawn"})
NON_TERMINAL_OUTCOMES = frozenset({"extended"})
ALL_OUTCOMES = TERMINAL_OUTCOMES | NON_TERMINAL_OUTCOMES


# ---------------------------------------------------------------------------
# application-number digit normalization (match across "NDA 215344" list-form
# and bare digits — A0 §2.2 / §5.1).
# ---------------------------------------------------------------------------
# Real NDA/BLA application numbers are 6 digits. Below this, a "match" is almost
# certainly a stray digit from a surrogate (e.g. the '8' in 'EDGAR8K:...') — refuse
# it so a surrogate never spuriously digit-matches a CRL/Drugs@FDA record.
_MIN_APPNO_DIGITS = 4

# Phase-0 surrogate appnos look like 'EDGAR8K:<cik>:<slug>'. They are NOT real
# FDA numbers, so they must never normalize to matchable digits.
_SURROGATE_PREFIX_RE = re.compile(r"^\s*EDGAR8K[:\s]", re.IGNORECASE)


def normalize_appno_digits(appno: Any) -> str:
    """Return the bare digits of an application number (drops NDA/BLA prefix,
    spaces, surrogate junk). Handles both the CRL list-form (``["NDA 215344"]``)
    and a scalar (``"NDA215344"`` / ``"BLA 761385"`` / ``"215344"``).

    A Phase-0 surrogate (``EDGAR8K:...``) normalizes to ``""`` — even though it
    contains the literal '8', it is not a real FDA number and must never
    digit-match a CRL/Drugs@FDA record. Likewise a result with fewer than
    ``_MIN_APPNO_DIGITS`` digits is treated as no-match (``""``)."""
    if isinstance(appno, (list, tuple)):
        for item in appno:
            d = normalize_appno_digits(item)
            if d:
                return d
        return ""
    s = str(appno or "")
    if _SURROGATE_PREFIX_RE.match(s):
        return ""  # surrogate: never matchable
    digits = re.sub(r"\D", "", s)
    if len(digits) < _MIN_APPNO_DIGITS:
        return ""
    return digits


def _parse_date(value: Any) -> Optional[date]:
    if isinstance(value, date):
        return value
    if not value:
        return None
    s = str(value)
    # ISO YYYY-MM-DD
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        pass
    # compact YYYYMMDD (Drugs@FDA submission_status_date raw)
    try:
        return datetime.strptime(s[:8], "%Y%m%d").date()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 1. CRL Transparency match
# ---------------------------------------------------------------------------
def match_crl(
    crl_records: Optional[List[Dict[str, Any]]],
    application_number: str,
    pdufa_date: Any,
) -> Optional[Dict[str, Any]]:
    """Return the matching CRL record (or None) for an app.

    A match requires digit-normalized application_number equality AND
    ``letter_year >= pdufa_year`` (the CRL must be on/after the catalyst, not a
    prior cycle). ``crl_records`` is the parsed transparency ``results`` list; pass
    None/[] when the source is unavailable (-> no match, caller logs the gap)."""
    if not crl_records:
        return None
    target = normalize_appno_digits(application_number)
    if not target:
        return None
    pdufa = _parse_date(pdufa_date)
    pdufa_year = pdufa.year if pdufa else None

    for rec in crl_records:
        rec_digits = normalize_appno_digits(rec.get("application_number"))
        if rec_digits != target:
            continue
        if pdufa_year is not None:
            ly = rec.get("letter_year")
            try:
                if ly is not None and int(str(ly)[:4]) < pdufa_year:
                    continue
            except (TypeError, ValueError):
                pass  # unparseable letter_year => don't exclude on the year gate
        return rec
    return None


# ---------------------------------------------------------------------------
# 2. Drugs@FDA submission status
# ---------------------------------------------------------------------------
def resolve_drugsfda_status(
    submissions: Optional[List[Dict[str, Any]]],
    pdufa_date: Any,
) -> Optional[str]:
    """Map an app's ORIG submission status to ``approved`` / ``withdrawn`` / None.

    ``submissions`` is the ``extract_submission_rows``-shaped list (each row has
    ``submission_type``, ``submission_status``, ``submission_status_date``). An ORIG
    ``AP`` dated >= pdufa_date -> ``approved``; an ORIG ``WD`` -> ``withdrawn``.
    Returns None when no terminal ORIG status is present yet."""
    if not submissions:
        return None
    pdufa = _parse_date(pdufa_date)

    approved = False
    withdrawn = False
    for s in submissions:
        if str(s.get("submission_type", "")).upper() != "ORIG":
            continue
        status = str(s.get("submission_status", "")).upper()
        sdate = _parse_date(s.get("submission_status_date"))
        if status == "AP":
            # An AP on/after PDUFA is the decision; an AP before PDUFA would be a
            # prior cycle's approval (rare for an ORIG) — require >= pdufa when known.
            if pdufa is None or (sdate is not None and sdate >= pdufa) or sdate is None:
                approved = True
        elif status == "WD":
            withdrawn = True
    if approved:
        return "approved"
    if withdrawn:
        return "withdrawn"
    return None


# ---------------------------------------------------------------------------
# 3. PDUFA extension (§5.5)
# ---------------------------------------------------------------------------
def detect_extension(current_pdufa: Any, last_seen_pdufa: Any) -> bool:
    """True when the current PDUFA date is LATER than the last-seen one (a push).

    The caller keeps the last-seen PDUFA per app (in bc_pipeline_runs.log, no new
    column — §5.5). A push with no terminal verdict -> ``extended``."""
    cur = _parse_date(current_pdufa)
    prev = _parse_date(last_seen_pdufa)
    if cur is None or prev is None:
        return False
    return cur > prev


# ---------------------------------------------------------------------------
# Top-level resolution (precedence-ordered)
# ---------------------------------------------------------------------------
def resolve_regulatory_outcome(
    *,
    application_number: str,
    pdufa_date: Any,
    crl_records: Optional[List[Dict[str, Any]]] = None,
    submissions: Optional[List[Dict[str, Any]]] = None,
    last_seen_pdufa: Any = None,
    crl_source_available: bool = True,
) -> Dict[str, Any]:
    """Resolve a single app's regulatory outcome by source precedence (§5.1).

    Returns ``{"outcome": <crl|approved|withdrawn|extended|None>, "source": str,
    "is_terminal": bool, "log": <token|None>}``. ``outcome=None`` means still
    unresolved (no row written; a prior partial row may still accrue prices). The
    value, when set, is ALWAYS lowercase (CHECK-conformant)."""
    # 1. CRL Transparency (most authoritative). Gate on source availability.
    if crl_source_available:
        crl = match_crl(crl_records, application_number, pdufa_date)
        if crl is not None:
            return {"outcome": "crl", "source": "crl_transparency", "is_terminal": True, "log": None}
    else:
        # source not importable / not supplied — approvals path still works.
        log_token = "crl_source_unavailable"
        status = resolve_drugsfda_status(submissions, pdufa_date)
        if status is not None:
            return {"outcome": status, "source": "drugsfda", "is_terminal": True, "log": log_token}
        if detect_extension(pdufa_date, last_seen_pdufa):
            return {"outcome": "extended", "source": "pdufa_push", "is_terminal": False, "log": log_token}
        return {"outcome": None, "source": "none", "is_terminal": False, "log": log_token}

    # 2. Drugs@FDA approval / withdrawal.
    status = resolve_drugsfda_status(submissions, pdufa_date)
    if status is not None:
        return {"outcome": status, "source": "drugsfda", "is_terminal": True, "log": None}

    # 3. PDUFA extension (non-terminal).
    if detect_extension(pdufa_date, last_seen_pdufa):
        return {"outcome": "extended", "source": "pdufa_push", "is_terminal": False, "log": None}

    return {"outcome": None, "source": "none", "is_terminal": False, "log": None}


# ---------------------------------------------------------------------------
# hypothesis_outcome (§5.4) — free text, no CHECK. Logging, not gating.
# ---------------------------------------------------------------------------
_BAND_HIGH = frozenset({"elevated", "high"})
_BAND_LOW = frozenset({"low", "moderate"})


def hypothesis_outcome(band: Optional[str], regulatory_outcome: Optional[str]) -> Optional[str]:
    """Pair the SHOWN pre-PDUFA band with the TERMINAL verdict (§5.4).

    Returns one of: band_correct_high_risk / band_correct_low_risk /
    band_overstated_risk / band_understated_risk / indeterminate. Returns None
    (omit from the upsert) until BOTH a band and a terminal verdict exist."""
    if not band or not regulatory_outcome:
        return None
    ro = str(regulatory_outcome).lower()
    if ro not in TERMINAL_OUTCOMES:
        # 'extended' (or unknown) is non-terminal -> indeterminate, but only once
        # a verdict string exists; a bare extended pairs to indeterminate.
        if ro in NON_TERMINAL_OUTCOMES:
            return "indeterminate"
        return None
    b = str(band).lower()
    if b in _BAND_HIGH and ro == "crl":
        return "band_correct_high_risk"
    if b in _BAND_LOW and ro == "approved":
        return "band_correct_low_risk"
    if b in _BAND_HIGH and ro == "approved":
        return "band_overstated_risk"
    if b in _BAND_LOW and ro == "crl":
        return "band_understated_risk"  # the costly miss: model calm, drug CRL'd
    # withdrawn or an unmapped band combination
    return "indeterminate"
