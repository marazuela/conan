"""Compute binary-catalyst pre-gate inputs (designations + sponsor history).

These hydrate the fda_assets pre-gate columns (priority_review,
breakthrough_designation, sponsor_prior_nda_count, first_time_sponsor) that the
reactor pre-gate (supabase/functions/reactor/bc-pregate.ts) reads at dispatch.
Sourced here on a dedicated budget instead of the scanner's per-scan openFDA
enrichment, which was budget-starved and populated <2% of signals -> every input
read false and the gate scored every asset 0.

Sources (deliberately NOT openFDA for the designations):
  - priority_review / breakthrough_designation: our own 8-K `extracted_facts`
    (fact_type='designation'). openFDA `drugsfda` does not carry these, and a
    pending pre-PDUFA application has no application_number in Drugs@FDA to query.
  - first_time_sponsor: openFDA `drugsfda` sponsor_name -> count of distinct prior
    *approved* NDA/BLA application numbers. 0 => first-time (a genuine first-timer's
    in-flight application is not yet in Drugs@FDA, so it reads 0).

`parse_designation_flags` is HTTP-free and unit-tested directly. The openFDA path
goes through `openfda_get()` (shared retry/auth).
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, Optional

from modal_workers.shared.openfda_client import openfda_get

# Designation facts are PR/8-K sourced and high-confidence; ignore low-confidence
# noise so a single shaky extraction can't flip the gate.
DESIGNATION_MIN_CONFIDENCE = 0.85

# "Breakthrough Therapy" / "Priority Review" are FDA-specific terms (the EU uses
# PRIME / accelerated assessment), so matching the phrase inherently scopes to FDA.
_BREAKTHROUGH_RE = re.compile(
    r"breakthrough\s+therapy(?:\s+designation)?|breakthrough\s+designation", re.I)
_PRIORITY_REVIEW_RE = re.compile(r"priority\s+review", re.I)
# Crude negation guard over the run-up to the match ("did not receive", "denied",
# "rescinded"). These PR-sourced facts are overwhelmingly affirmative, so this only
# needs to catch the rare explicit negative.
_NEGATION_RE = re.compile(
    r"\b(?:not|no|never|denied|declin\w*|rescind\w*|revok\w*|without|lost|failed\s+to)\b",
    re.I)


def _affirms(text: str, term_re: "re.Pattern[str]") -> bool:
    """True when `term_re` matches and is not locally negated."""
    m = term_re.search(text or "")
    if not m:
        return False
    window = text[max(0, m.start() - 40):m.start()]
    return _NEGATION_RE.search(window) is None


def parse_designation_flags(facts: Iterable[Dict[str, Any]]) -> Dict[str, bool]:
    """Reduce designation facts to gate booleans.

    `facts`: iterable of dicts with `fact_text` (str) and `confidence` (numeric).
    Returns {"priority_review": bool, "breakthrough_designation": bool}; a flag is
    True iff at least one >=DESIGNATION_MIN_CONFIDENCE fact affirms it.
    """
    priority_review = False
    breakthrough = False
    for f in facts:
        try:
            conf = float(f.get("confidence") or 0)
        except (TypeError, ValueError):
            conf = 0.0
        if conf < DESIGNATION_MIN_CONFIDENCE:
            continue
        text = f.get("fact_text") or ""
        if not breakthrough and _affirms(text, _BREAKTHROUGH_RE):
            breakthrough = True
        if not priority_review and _affirms(text, _PRIORITY_REVIEW_RE):
            priority_review = True
        if priority_review and breakthrough:
            break
    return {"priority_review": priority_review,
            "breakthrough_designation": breakthrough}


def count_sponsor_prior_nda(sponsor_name: Optional[str]) -> Optional[int]:
    """Distinct prior approved NDA/BLA application numbers for the sponsor (openFDA).

    Returns:
      - int >= 0 on a resolved lookup (404 / no Drugs@FDA hits => 0),
      - None when the lookup cannot be established (network/parse failure) so the
        caller leaves first_time_sponsor unknown rather than guessing first-time.
    """
    if not sponsor_name or not sponsor_name.strip():
        return None
    sponsor_clean = sponsor_name.replace('"', "").strip()
    if not sponsor_clean:
        return None
    try:
        body = openfda_get(
            "drug/drugsfda.json",
            params={"search": f'sponsor_name:"{sponsor_clean}"', "limit": 100},
        )
    except Exception:  # noqa: BLE001 — any HTTP/parse error => "unknown", not zero
        return None
    if not body:
        # openfda_get returns None on a 404, which Drugs@FDA uses for "no hits".
        return 0
    results = body.get("results") or []
    distinct = {
        r.get("application_number")
        for r in results
        if isinstance(r, dict) and r.get("application_number")
    }
    return len(distinct)


def first_time_sponsor(prior_nda_count: Optional[int]) -> Optional[bool]:
    """sponsor_prior_nda_count == 0 on a confirmed lookup; None stays unknown."""
    if prior_nda_count is None:
        return None
    return prior_nda_count == 0
