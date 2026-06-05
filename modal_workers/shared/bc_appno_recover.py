"""bc_appno_recover — recover a REAL NDA/BLA application number for a discovered
PDUFA candidate by joining to openFDA Drugs@FDA.

Context (Phase 0 / approach 1). The 8-K extractor (`bc_pdufa_extract`) yields a
real **PDUFA date** but an 8-K almost never states the FDA application number, so
the enumerator otherwise synthesizes a surrogate ``EDGAR8K:<cik>:<slug>`` and
marks the feature row ``feature_quality='low'``. This module attempts to replace
that surrogate with the **real** number (``NDA######`` / ``BLA######``) plus the
authoritative ``appl_type`` and ``review_priority``, by looking the sponsor/drug
up in Drugs@FDA and taking its **ORIG** (original) submission.

Why this is best-effort (and mostly recovers *resolved/older* apps, not pending):
Drugs@FDA is a **post-decision** registry — an application appears only once it
has a submission on file (typically after FDA receipt; an approval/AP, tentative
TA, or complete-response CR status). A brand-new *pending* NDA/BLA whose only
public footprint is the company's PDUFA-date 8-K usually is **not yet in
Drugs@FDA**, so recovery legitimately misses it and the row stays surrogate. That
is the correct, honest behavior — we never fabricate a number. Recovery mainly
helps when the company has prior approved products under the same NDA/BLA, or for
the resolved half of the benchmark truth set.

Design constraints honored:
  - **Read-only.** Only GETs openFDA; writes nothing.
  - **Idempotent.** Pure function of (sponsor, drug); same inputs → same output.
  - **Rate-limit-safe.** One narrow query per sponsor (optionally one per
    brand_name), routed through ``openfda_client.openfda_get`` so the
    ``OPENFDA_API_KEY`` (120k/day) is applied and the unauth 1,000/day shared-IP
    cap (memory ``openfda_rate_limit_gap``) is respected. Callers cache per-CIK so
    one sponsor is queried once per run.
  - **Surrogate provenance preserved.** A successful recovery returns a real
    number → caller flips ``feature_quality`` to ``'standard'``; a miss keeps the
    ``EDGAR8K:`` surrogate at ``'low'``.

Public surface
--------------
``recover_real_appno(drug, sponsor_name, *, session=None, openfda_get=None) -> Optional[RecoveredAppno]``
    The one network entry point. Returns a ``RecoveredAppno`` or ``None`` (no
    confident match). ``openfda_get`` is injectable so tests pass a fake (no HTTP).

``pick_orig_application(records, *, drug=None) -> Optional[RecoveredAppno]``
    Pure (no I/O): given a list of Drugs@FDA records (the ``results`` array),
    choose the best one and extract its ORIG submission identity. Unit-testable
    against fixture payloads.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("bc_appno_recover")

# Drugs@FDA application numbers look like NDA######, BLA######, ANDA######.
# We accept only NDA / BLA originals for the BC universe (sNDA/sBLA are
# supplements; a fresh PDUFA-date 8-K is an original-application catalyst).
_APPNO_RE = re.compile(r"^(NDA|BLA)\d+$", re.IGNORECASE)


@dataclass
class RecoveredAppno:
    """A real Drugs@FDA application identity recovered for a candidate."""

    application_number: str          # e.g. "NDA215000" / "BLA761234"
    appl_type: str                   # "NDA" | "BLA" (from the number prefix)
    review_priority: Optional[str] = None   # "PRIORITY" | "STANDARD" | None
    sponsor_name: Optional[str] = None       # the matched record's sponsor
    matched_brand: Optional[str] = None      # which brand_name matched the drug
    match_basis: str = "sponsor"             # "brand" | "sponsor" | "sole"


# ---------------------------------------------------------------------------
# Pure selection / extraction
# ---------------------------------------------------------------------------

def _appl_type_from_number(appno: str) -> Optional[str]:
    m = _APPNO_RE.match(appno or "")
    if not m:
        return None
    return m.group(1).upper()


def _norm(s: Optional[str]) -> str:
    """Lowercase + strip non-alphanumerics for tolerant name matching
    ('VK-2735' ~ 'VK2735', 'Opdivo®' ~ 'opdivo')."""
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _orig_submission(app: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return the ORIG (submission_number == '1', submission_type == 'ORIG')
    submission for an application, or None. Drugs@FDA always carries exactly one
    ORIG per application; we pick it for the authoritative review_priority."""
    origs = [
        s for s in (app.get("submissions") or [])
        if str(s.get("submission_type", "")).upper() == "ORIG"
    ]
    if not origs:
        return None
    # Prefer submission_number '1'; else the lexicographically-first ORIG.
    for s in origs:
        if str(s.get("submission_number")) == "1":
            return s
    return sorted(origs, key=lambda s: str(s.get("submission_number") or ""))[0]


def _brands(app: Dict[str, Any]) -> List[str]:
    return [
        p.get("brand_name") for p in (app.get("products") or [])
        if p.get("brand_name")
    ]


def _record_to_recovered(app: Dict[str, Any], *, match_basis: str,
                         matched_brand: Optional[str]) -> Optional[RecoveredAppno]:
    appno = str(app.get("application_number") or "").upper()
    appl_type = _appl_type_from_number(appno)
    if not appl_type:
        return None  # ANDA / malformed → not a BC original
    orig = _orig_submission(app)
    priority = None
    if orig:
        rp = orig.get("review_priority")
        if isinstance(rp, str) and rp.strip().upper() in ("PRIORITY", "STANDARD"):
            priority = rp.strip().upper()
    return RecoveredAppno(
        application_number=appno,
        appl_type=appl_type,
        review_priority=priority,
        sponsor_name=app.get("sponsor_name"),
        matched_brand=matched_brand,
        match_basis=match_basis,
    )


def pick_orig_application(
    records: List[Dict[str, Any]],
    *,
    drug: Optional[str] = None,
) -> Optional[RecoveredAppno]:
    """Choose the best Drugs@FDA record for our candidate and extract its ORIG
    NDA/BLA identity. Pure — no I/O.

    Selection priority (conservative; we only recover when reasonably confident):
      1. **brand match** — a record whose product ``brand_name`` matches ``drug``
         (normalized). Most reliable: ties the number to the specific drug.
      2. **sole NDA/BLA** — exactly one NDA/BLA record returned and no brand info
         to disambiguate → take it.
    If neither holds (multiple records, none brand-matching), return None — we do
    NOT guess which of several applications is the catalyst (a wrong real number
    is worse than an honest surrogate)."""
    if not records:
        return None

    nda_bla = [
        r for r in records
        if _appl_type_from_number(str(r.get("application_number") or "").upper())
    ]
    if not nda_bla:
        return None

    # 1. brand match
    if drug:
        dn = _norm(drug)
        if dn:
            for r in nda_bla:
                for b in _brands(r):
                    nb = _norm(b)
                    # substring either way: "opdivo" in "opdivoqvantig" and vice-versa
                    if nb and (nb == dn or nb in dn or dn in nb):
                        rec = _record_to_recovered(r, match_basis="brand", matched_brand=b)
                        if rec:
                            return rec

    # 2. sole NDA/BLA application
    if len(nda_bla) == 1:
        return _record_to_recovered(nda_bla[0], match_basis="sole", matched_brand=None)

    # Ambiguous → no confident recovery.
    return None


# ---------------------------------------------------------------------------
# Network entry point (read-only)
# ---------------------------------------------------------------------------

# Default openFDA fetcher — imported lazily so this module stays import-light and
# tests can inject a fake without the `requests`/openfda_client dependency.
def _default_openfda_get(path: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    from modal_workers.shared.openfda_client import openfda_get
    return openfda_get(path, params)


def recover_real_appno(
    drug: Optional[str],
    sponsor_name: Optional[str],
    *,
    openfda_get: Optional[Callable[[str, Dict[str, Any]], Optional[Dict[str, Any]]]] = None,
    page_limit: int = 20,
) -> Optional[RecoveredAppno]:
    """Recover a real NDA/BLA number for (drug, sponsor) via Drugs@FDA, or None.

    Strategy (≤2 narrow GETs per call, brand first since it's the strongest key):
      A. If ``drug`` is present, query ``products.brand_name:"<drug>"`` and try a
         brand match. (Cheap, high-precision; misses fresh drugs not yet in
         Drugs@FDA — that's fine.)
      B. Else / if A misses, query ``sponsor_name:"<sponsor>"`` and take a sole
         NDA/BLA (or a brand match within the sponsor's records).

    All reads route through ``openfda_get`` (auth + retry + 404→None). Returns the
    first confident match or None. Never raises on a 404/empty — recovery is
    advisory; the caller keeps the surrogate on a miss.
    """
    get = openfda_get or _default_openfda_get

    # A. brand-name query (strongest)
    if drug and drug.strip():
        try:
            body = get(
                "/drug/drugsfda.json",
                {"search": f'products.brand_name:"{drug.strip()}"', "limit": page_limit},
            )
        except Exception as e:  # noqa: BLE001 — recovery is advisory, never fatal
            logger.info("drugsfda brand query failed for %r: %s", drug, e)
            body = None
        if body:
            rec = pick_orig_application(body.get("results") or [], drug=drug)
            if rec:
                return rec

    # B. sponsor-name query (fallback)
    if sponsor_name and sponsor_name.strip():
        sponsor_q = _clean_sponsor(sponsor_name)
        try:
            body = get(
                "/drug/drugsfda.json",
                {"search": f'sponsor_name:"{sponsor_q}"', "limit": page_limit},
            )
        except Exception as e:  # noqa: BLE001
            logger.info("drugsfda sponsor query failed for %r: %s", sponsor_q, e)
            body = None
        if body:
            rec = pick_orig_application(body.get("results") or [], drug=drug)
            if rec:
                return rec

    return None


# Sponsor names on 8-K display lines carry corporate suffixes Drugs@FDA may not
# store identically ("Exelixis, Inc." vs "EXELIXIS"). Drop the trailing
# Inc./Corp./Ltd./plc and punctuation to widen the (already exact-phrase) match a
# little without going fuzzy.
_SPONSOR_SUFFIX_RE = re.compile(
    r"[,\.]?\s+(?:inc|incorporated|corp|corporation|co|company|ltd|limited|plc|"
    r"llc|lp|sa|ag|nv|holdings|pharmaceuticals?|therapeutics?|biosciences?)\b.*$",
    re.IGNORECASE,
)


def _clean_sponsor(name: str) -> str:
    stripped = _SPONSOR_SUFFIX_RE.sub("", name).strip()
    # Keep at least one token; if stripping nuked everything, fall back to raw.
    return stripped or name.strip()
