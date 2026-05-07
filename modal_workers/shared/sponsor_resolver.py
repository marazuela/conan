"""sponsor_resolver — sponsor_name → ticker resolution for FDA assets (D-110b).

Two-pass resolver per export ``bc_ticker_resolution_required`` postmortem:

  Pass 1 — ``CURATED_MAP``: high-confidence mapping for known pharma sponsors
           (sourced from migration ``20260430010000_seed_binary_catalyst_sponsor_tickers.sql``
           plus an explicit ``PRIVATE_DISCARD`` list for issuers without a
           public ticker that should not enter the tradeable universe).

  Pass 2 — ``match_sponsor_to_ticker``: Jaccard-style fuzzy match against the
           ``entities`` table. Lifted verbatim from
           ``modal_workers/scripts/curate_eval_harness.py`` (which now imports
           from this module). Score ≥3 OR single-distinctive-token whole-word
           match accepts.

Public surface:

  ``resolve_sponsor(sponsor_name, client) -> SponsorResolution``
        Single-call resolver. Returns dataclass with ticker / mic / entity_id /
        match_method / confidence. Never raises on resolution failure — returns
        ``match_method='unresolved'``.

  ``CURATED_MAP``      — explicit dict, callable for tests + offline use.
  ``PRIVATE_DISCARD``  — set of normalized names that resolve to no ticker.
  ``match_sponsor_to_ticker(name, client)``  — legacy fuzzy-match entry point.

Match-method semantics:
  - ``curated``         confidence 1.00 — direct lookup in CURATED_MAP.
  - ``private_discard`` confidence 1.00 — known private/non-public issuer; do not score.
  - ``jaccard``         confidence 0.60–0.85 from match score.
  - ``unresolved``      confidence 0.00 — caller flags for review.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set


# ---------------------------------------------------------------------------
# Pass 1: curated map
# ---------------------------------------------------------------------------

# Mirrors ``20260430010000_seed_binary_catalyst_sponsor_tickers.sql``. Keep the
# Python side in sync when the migration changes — both are sources of truth
# for different stages (migration backfills entities; this dict resolves at
# ingest before an entities row exists).
CURATED_MAP: Dict[str, Dict[str, str]] = {
    # US-listed pharma majors and biotechs
    "AbbVie": {"ticker": "ABBV", "mic": "XNYS", "country": "US"},
    "Amicus Therapeutics": {"ticker": "FOLD", "mic": "XNAS", "country": "US"},
    "Arcus Biosciences, Inc.": {"ticker": "RCUS", "mic": "XNAS", "country": "US"},
    "Arrowhead Pharmaceuticals": {"ticker": "ARWR", "mic": "XNAS", "country": "US"},
    "Bristol-Myers Squibb": {"ticker": "BMY", "mic": "XNYS", "country": "US"},
    "Celcuity Inc": {"ticker": "CELC", "mic": "XNAS", "country": "US"},
    "CG Oncology, Inc.": {"ticker": "CGON", "mic": "XNAS", "country": "US"},
    "Cytokinetics": {"ticker": "CYTK", "mic": "XNAS", "country": "US"},
    "Eli Lilly and Company": {"ticker": "LLY", "mic": "XNYS", "country": "US"},
    "Exelixis": {"ticker": "EXEL", "mic": "XNAS", "country": "US"},
    "Ionis Pharmaceuticals, Inc.": {"ticker": "IONS", "mic": "XNAS", "country": "US"},
    "Neumora Therapeutics, Inc.": {"ticker": "NMRA", "mic": "XNAS", "country": "US"},
    "Novavax": {"ticker": "NVAX", "mic": "XNAS", "country": "US"},
    "Ocular Therapeutix, Inc.": {"ticker": "OCUL", "mic": "XNAS", "country": "US"},
    "PTC Therapeutics": {"ticker": "PTCT", "mic": "XNAS", "country": "US"},
    "Revolution Medicines, Inc.": {"ticker": "RVMD", "mic": "XNAS", "country": "US"},
    "Ultragenyx Pharmaceutical Inc": {"ticker": "RARE", "mic": "XNAS", "country": "US"},
    "Vertex Pharmaceuticals Incorporated": {"ticker": "VRTX", "mic": "XNAS", "country": "US"},
    "Viridian Therapeutics, Inc.": {"ticker": "VRDN", "mic": "XNAS", "country": "US"},
    # Foreign issuers with US-listed ADRs
    "AstraZeneca": {"ticker": "AZN", "mic": "XNAS", "country": "GB"},
    "BioNTech SE": {"ticker": "BNTX", "mic": "XNAS", "country": "DE"},
    "Novartis Pharmaceuticals": {"ticker": "NVS", "mic": "XNYS", "country": "CH"},
    "Novo Nordisk A/S": {"ticker": "NVO", "mic": "XNYS", "country": "DK"},
    "Sanofi": {"ticker": "SNY", "mic": "XNAS", "country": "FR"},
    "Takeda": {"ticker": "TAK", "mic": "XNYS", "country": "JP"},
    # Non-US primary listings
    "Hoffmann-La Roche": {"ticker": "ROG", "mic": "XSWX", "country": "CH"},
    "Ipsen": {"ticker": "IPN", "mic": "XPAR", "country": "FR"},
    # Subsidiaries → public parent
    "Janssen Research & Development, LLC": {"ticker": "JNJ", "mic": "XNYS", "country": "US"},
    "Aragon Pharmaceuticals, Inc.": {"ticker": "JNJ", "mic": "XNYS", "country": "US"},
    "Merck Sharp & Dohme LLC": {"ticker": "MRK", "mic": "XNYS", "country": "US"},
    "Seagen, a wholly owned subsidiary of Pfizer": {"ticker": "PFE", "mic": "XNYS", "country": "US"},
    "Alexion Pharmaceuticals, Inc.": {"ticker": "AZN", "mic": "XNAS", "country": "GB"},
    "Bellus Health Inc. - a GSK company": {"ticker": "GSK", "mic": "XNYS", "country": "GB"},
    # Additional commonly-seen openFDA sponsors not yet in the seed migration
    "Pfizer": {"ticker": "PFE", "mic": "XNYS", "country": "US"},
    "Pfizer Inc": {"ticker": "PFE", "mic": "XNYS", "country": "US"},
    "Pfizer, Inc.": {"ticker": "PFE", "mic": "XNYS", "country": "US"},
    "Johnson & Johnson": {"ticker": "JNJ", "mic": "XNYS", "country": "US"},
    "Merck & Co., Inc.": {"ticker": "MRK", "mic": "XNYS", "country": "US"},
    "GlaxoSmithKline": {"ticker": "GSK", "mic": "XNYS", "country": "GB"},
    "Genentech, Inc.": {"ticker": "RHHBY", "mic": "OTCM", "country": "CH"},  # parent Roche
    "Regeneron Pharmaceuticals, Inc.": {"ticker": "REGN", "mic": "XNAS", "country": "US"},
    "Gilead Sciences, Inc.": {"ticker": "GILD", "mic": "XNAS", "country": "US"},
    "Biogen Inc.": {"ticker": "BIIB", "mic": "XNAS", "country": "US"},
    "Moderna, Inc.": {"ticker": "MRNA", "mic": "XNAS", "country": "US"},
    "Axsome Therapeutics, Inc.": {"ticker": "AXSM", "mic": "XNAS", "country": "US"},
    "Axsome Therapeutics": {"ticker": "AXSM", "mic": "XNAS", "country": "US"},
}

# Issuers without a public ticker that should not enter the tradeable universe.
# Private + government + non-profit sponsors. Resolver returns
# match_method='private_discard' so callers can flag tradeable_filter_pass=false
# without re-running the Jaccard fallback.
PRIVATE_DISCARD: Set[str] = {
    "Boehringer Ingelheim",
    "Boehringer Ingelheim Pharmaceuticals, Inc.",
    "Mallinckrodt",                    # post-bankruptcy reorg; restricted public listing
    "Allergan",                        # acquired by AbbVie 2020 — but historical filings under Allergan
    "Bayer HealthCare Pharmaceuticals Inc.",  # private subsidiary; map separately if needed
    "Daiichi Sankyo, Inc.",            # JP parent ADR not US-tradeable for institutional sizing
    "Otsuka Pharmaceutical Co., Ltd.", # JP parent
    "Chugai Pharmaceutical",           # majority-owned by Roche; JP listing
    "Watson Pharmaceuticals",          # acquired/dissolved
    "Forest Laboratories",             # acquired by Actavis 2014
    "Schering-Plough",                 # merged with Merck 2009
}

# Patterns marking obvious non-pharma entries (used to filter Jaccard candidates).
_NON_SPONSOR_PATTERNS = re.compile(
    r"(merger\s*corp|acquisition\s*corp|spac|holdings?\s*ltd|"
    r"rare\s*earth|mining|energy|capital|ventures?|partners|trust|"
    r"reit|technology|software|fintech|crypto|blockchain|ai\s*inc)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Normalization (shared with curate_eval_harness fuzzy match)
# ---------------------------------------------------------------------------

_NORM_STRIP = re.compile(
    r"\b(inc|llc|ltd|corp|corporation|company|co|holdings|holding|group|"
    r"international|usa|us|s\.?a\.?|s\.?p\.?a\.?|n\.?v\.?|the|a|an)\b\.?",
    re.IGNORECASE,
)
_NORM_PUNCT = re.compile(r"[^\w\s]")
_GENERIC_TOKENS: Set[str] = {
    "pharma", "pharmaceuticals", "pharmaceutical", "therapeutics", "therapeutic",
    "biotechnology", "biosciences", "biotech", "sciences", "science",
    "labs", "laboratories", "lab", "research", "development",
    "medical", "medicines", "medicine", "health", "healthcare", "bio",
}


def _normalize_sponsor(name: str) -> str:
    s = name.lower()
    s = _NORM_PUNCT.sub(" ", s)
    s = _NORM_STRIP.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _distinctive_tokens(norm: str) -> List[str]:
    return [t for t in norm.split()
            if len(t) >= 4 and t not in _GENERIC_TOKENS]


# ---------------------------------------------------------------------------
# Resolution data class
# ---------------------------------------------------------------------------

@dataclass
class SponsorResolution:
    sponsor_name: str
    ticker: Optional[str]
    mic: Optional[str]
    country: Optional[str]
    entity_id: Optional[str]
    match_method: str            # 'curated' | 'private_discard' | 'jaccard' | 'unresolved'
    confidence: float            # 0.0–1.0

    @property
    def tradeable(self) -> bool:
        """True iff this sponsor maps to a public ticker (D-105 tradeable filter
        prerequisite). private_discard and unresolved are NOT tradeable."""
        return self.match_method in ("curated", "jaccard") and bool(self.ticker)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve_sponsor(
    sponsor_name: Optional[str],
    client: Any = None,
    *,
    skip_jaccard: bool = False,
) -> SponsorResolution:
    """Two-pass sponsor → ticker resolution.

    Parameters
    ----------
    sponsor_name : str | None
        Raw sponsor name from openFDA / EDGAR / etc. May contain trailing
        whitespace, legal-form suffixes, subsidiary qualifiers.
    client : SupabaseClient | None
        Used only for Pass 2 (Jaccard fuzzy match against entities). Pass None
        in tests / offline contexts; resolver returns 'unresolved' if curated
        miss + no client.
    skip_jaccard : bool, default False
        Set True when the caller only wants curated/discard hits (e.g. fast
        path during scanner emission where missing the Jaccard match is
        cheaper than the round-trip to Supabase).

    Returns
    -------
    SponsorResolution. Never raises on resolution failure.
    """
    if not sponsor_name or not sponsor_name.strip():
        return SponsorResolution(
            sponsor_name=sponsor_name or "",
            ticker=None, mic=None, country=None, entity_id=None,
            match_method="unresolved", confidence=0.0,
        )

    raw = sponsor_name.strip()

    # Pass 1a — exact curated hit
    if raw in CURATED_MAP:
        m = CURATED_MAP[raw]
        return SponsorResolution(
            sponsor_name=raw, ticker=m["ticker"], mic=m["mic"],
            country=m["country"], entity_id=None,
            match_method="curated", confidence=1.0,
        )

    # Pass 1b — case-insensitive curated hit
    raw_lower = raw.lower()
    for key, m in CURATED_MAP.items():
        if key.lower() == raw_lower:
            return SponsorResolution(
                sponsor_name=raw, ticker=m["ticker"], mic=m["mic"],
                country=m["country"], entity_id=None,
                match_method="curated", confidence=1.0,
            )

    # Pass 1c — private-discard list (exact + case-insensitive)
    if raw in PRIVATE_DISCARD or raw_lower in {p.lower() for p in PRIVATE_DISCARD}:
        return SponsorResolution(
            sponsor_name=raw, ticker=None, mic=None, country=None, entity_id=None,
            match_method="private_discard", confidence=1.0,
        )

    # Pass 2 — Jaccard fuzzy match (requires Supabase client)
    if skip_jaccard or client is None:
        return SponsorResolution(
            sponsor_name=raw, ticker=None, mic=None, country=None, entity_id=None,
            match_method="unresolved", confidence=0.0,
        )

    entity = match_sponsor_to_ticker(raw, client)
    if entity:
        score = entity.get("_match_score", 3)
        confidence = min(0.85, 0.55 + 0.10 * max(0, int(score) - 3))
        return SponsorResolution(
            sponsor_name=raw,
            ticker=entity.get("primary_ticker"),
            mic=entity.get("primary_mic"),
            country=entity.get("country"),
            entity_id=entity.get("id"),
            match_method="jaccard",
            confidence=confidence,
        )

    return SponsorResolution(
        sponsor_name=raw, ticker=None, mic=None, country=None, entity_id=None,
        match_method="unresolved", confidence=0.0,
    )


def match_sponsor_to_ticker(
    sponsor_name: str,
    client: Any,
) -> Optional[Dict[str, Any]]:
    """Fuzzy-match a sponsor name to an entities row. Returns the entities row
    (with ticker) on success, None on no match.

    Matching rules (in order of strictness):
      1. Search entities by ILIKE on the sponsor's longest distinctive token.
      2. Score each candidate by:
         - +2 if longest sponsor token whole-word-matches in entity name
         - +3 if longest sponsor token == longest entity token
         - +1 per shared distinctive token
         - +5 if all distinctive sponsor tokens appear as whole words in entity
      3. Require score ≥ 3 OR (sponsor has only 1 distinctive token AND that
         token whole-word-matches in entity).

    Returns the entity row with ``_match_score`` injected for confidence accounting.
    """
    norm = _normalize_sponsor(sponsor_name)
    if not norm:
        return None

    sponsor_dist = _distinctive_tokens(norm)
    if not sponsor_dist:
        return None
    sponsor_tokens = set(norm.split())
    longest = max(sponsor_dist, key=len)

    rows = client._rest(
        "GET", "entities",
        params={
            "select": "id,name,primary_ticker,primary_mic,country,issuer_figi",
            "name": f"ilike.*{longest}*",
            "primary_ticker": "not.is.null",
            "limit": "20",
        },
    ) or []
    if not rows:
        return None

    longest_re = re.compile(rf"\b{re.escape(longest)}\b", re.IGNORECASE)

    best: Optional[Dict[str, Any]] = None
    best_score = -1
    for row in rows:
        ent_name = row.get("name") or ""
        if _NON_SPONSOR_PATTERNS.search(ent_name):
            continue
        ent_norm = _normalize_sponsor(ent_name)
        ent_tokens = set(ent_norm.split())
        ent_dist = _distinctive_tokens(ent_norm)

        whole_word_match = bool(longest_re.search(ent_name))

        score = 0
        if whole_word_match:
            score += 2
        if ent_dist and longest == max(ent_dist, key=len):
            score += 3
        score += len(set(sponsor_dist) & set(ent_dist))
        if sponsor_dist and all(t in ent_tokens for t in sponsor_dist):
            score += 5

        if score > best_score:
            best_score = score
            best = row

    if best_score >= 3:
        if best is not None:
            best["_match_score"] = best_score
        return best
    if best and len(sponsor_dist) == 1 and longest_re.search(best.get("name") or ""):
        best["_match_score"] = best_score
        return best
    return None
