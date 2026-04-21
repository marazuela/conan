"""
party_resolver.py — Two-stage party → issuer resolution for Tool 3.

Implements the protocol specified in CONTEXT.md §"Entity Resolution Protocol"
and D-003. Stage 1 normalizes raw party strings. Stage 2 walks a fallback
chain of lookup sources, assigning a confidence score on success.

Resolution chain (first success wins):
  1. Internal cache (baselines/party_resolution_cache.json)         conf 1.00
  2. SEC EDGAR company-name exact match                             conf 0.95
  3. SEC EDGAR company-name fuzzy match (Levenshtein ≤ 3)           conf 0.80
  4. Exhibit-21 subsidiary table (baselines/exhibit21_subsidiary_table.json)
                                                                     conf 0.90 direct
                                                                     conf 0.75 indirect
  5. OpenFIGI NAME idType mapping (fallback, low precision)         conf ≤ 0.70
  6. Unresolved → logged to working/unresolved_parties.md

Confidence thresholds (D-003):
  ≥ 0.85 : signal admitted to convergence engine and scoring
  0.70 ≤ c < 0.85 : admitted with caveat; triaged at Stage 1 unless corroborated
  < 0.70 : logged but excluded from active pipeline

Per-host UA discipline (D-015):
  SEC hosts  → operational UA "Litigation Signal Tool contact-<email>"
  USITC hosts → browser UA (not used in this module; for future scanners)
  OpenFIGI, Wikidata → neutral UA acceptable

Never keys convergence on a legal-entity name string — only issuer_figi.

Status: Phase 2 scaffold. Stage 1 is fully functional and unit-testable
offline. Stage 2 API calls (_resolve_via_edgar_exact, _resolve_via_edgar_fuzzy,
_resolve_via_openfigi_name) are shape-correct but have NOT been smoke-tested
against live endpoints in this session — the Cowork sandbox was unavailable
when this code was written. Next session must run
`tools/test_party_resolver.py --online` before relying on Stage 2 in scanners.
"""

from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional, Dict, List, Tuple, Literal

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

OPERATIONAL_UA = "Litigation Signal Tool contact-javiergorordo13@hotmail.com"
NEUTRAL_UA = "Mozilla/5.0 (compatible; LitigationSignalTool/1.0)"

# Paths are resolved relative to the litigation_system folder, not CWD,
# so the resolver works from any invocation directory.
_THIS_DIR = Path(__file__).resolve().parent          # tools/
_LITIGATION_ROOT = _THIS_DIR.parent                   # litigation_system/

CACHE_PATH = _LITIGATION_ROOT / "baselines" / "party_resolution_cache.json"
EXHIBIT21_PATH = _LITIGATION_ROOT / "baselines" / "exhibit21_subsidiary_table.json"
UNRESOLVED_PATH = _LITIGATION_ROOT / "working" / "unresolved_parties.md"

# HTTP timeouts & retry budget. Conservative; scanners may override.
HTTP_TIMEOUT_SECONDS = 20
HTTP_RETRY_COUNT = 2
HTTP_RETRY_BACKOFF_SECONDS = 2.0

# Fuzzy-match threshold for EDGAR name resolution (Levenshtein on normalized form).
FUZZY_MAX_DISTANCE = 3

# SEC fair-access: at most 10 requests per second per IP, but we self-throttle
# harder for the resolver's background role. Scanners should own their own
# request budget; the resolver's budget is small.
SEC_MIN_REQUEST_INTERVAL_SECONDS = 0.15

logger = logging.getLogger("party_resolver")


# ----------------------------------------------------------------------------
# Types
# ----------------------------------------------------------------------------

PartyClass = Literal["corporate_entity", "individual", "government", "unknown"]

ResolutionMethod = Literal[
    "cache_exact",
    "sec_edgar_exact",
    "sec_edgar_fuzzy",
    "exhibit21_direct",
    "exhibit21_indirect",
    "openfigi_name",
    "unresolved",
]


@dataclass
class NormalizedParty:
    """Output of Stage 1."""
    raw_name: str
    normalized_name: str     # lowercased, suffix-stripped, whitespace-collapsed
    party_class: PartyClass
    stripped_suffixes: List[str] = field(default_factory=list)
    # For class=individual, set if the name looks executive-shaped
    # (used by SEC enforcement scanner executive-lookup cross-ref).
    is_possible_executive: bool = False


@dataclass
class Resolution:
    """Output of Stage 2 (and the full resolve() pipeline)."""
    raw_name: str
    normalized_name: str
    party_class: PartyClass
    method: ResolutionMethod
    confidence: float
    # Issuer identity — populated only if method != "unresolved".
    cik: Optional[str] = None                 # zero-padded 10-digit string
    ticker: Optional[str] = None
    issuer_figi: Optional[str] = None
    issuer_name: Optional[str] = None
    # Metadata
    resolved_at: Optional[str] = None         # ISO 8601 UTC
    notes: Optional[str] = None

    def as_signal_raw_data(self) -> Dict[str, object]:
        """Produce the fields the signal JSON schema expects inside `raw_data`.

        Matches CONTEXT.md §"Signal JSON Schema" and INSTRUCTIONS.md §3.
        """
        return {
            "party_raw_name": self.raw_name,
            "resolution_method": self.method,
            "resolution_confidence": self.confidence,
        }


# ----------------------------------------------------------------------------
# Stage 1 — Normalization
# ----------------------------------------------------------------------------

# Ordered most-specific first so multi-token suffixes (e.g., "L.L.C.")
# are matched before their single-token substrings. Case-insensitive.
_CORP_SUFFIXES = [
    # Multi-token / punctuated forms first
    r"l\.?\s*l\.?\s*c\.?",                   # L.L.C., LLC
    r"l\.?\s*l\.?\s*p\.?",                   # L.L.P., LLP
    r"l\.?\s*p\.?",                          # L.P., LP
    r"p\.?\s*l\.?\s*c\.?",                   # P.L.C., PLC
    r"s\.?\s*a\.?",                          # S.A., SA (also matches some non-corp; low risk given ordering)
    r"n\.?\s*v\.?",                          # N.V.
    r"a\.?\s*g\.?",                          # A.G.
    r"s\.?\s*e\.?",                          # S.E. (European entity)
    r"g\.?\s*m\.?\s*b\.?\s*h\.?",            # GmbH
    r"pty\.?\s*ltd\.?",                      # Pty Ltd
    # Single-token forms
    r"incorporated",
    r"corporation",
    r"company",
    r"limited",
    r"holdings?",                            # "Holdings" often appears but is part of the name; see note
    r"inc\.?",
    r"corp\.?",
    r"co\.?",
    r"ltd\.?",
    r"plc",
    r"ab",                                    # Swedish aktiebolag — only at tail
    r"oyj",                                   # Finnish publicly-listed
    r"bv",
    r"nv",
    r"ag",
    r"se",
    r"kk",                                    # Japanese kabushiki kaisha
]

# Note on "holdings": stripping "Holdings" is aggressive — "Apple Holdings LLC" and
# "Apple Inc." both normalize to "apple" which is desirable for subsidiary matching,
# but "Berkshire Hathaway Holdings" stripped to "berkshire hathaway" risks a wrong
# match against "Berkshire Hathaway Inc.". The resolver records `stripped_suffixes`
# so downstream callers can inspect what was removed and escalate confidence
# gates when "Holdings" was among them. See test_party_resolver.py cases.

_SUFFIX_REGEX = re.compile(
    r"[,\s]*(?:" + r"|".join(_CORP_SUFFIXES) + r")[\.]?\s*$",
    flags=re.IGNORECASE,
)

# Individual-name heuristic. Very rough — used only to flag "classify as individual"
# vs "corporate_entity". Court captions often say "John Q. Smith, an individual" or
# name-with-trailing-role like "Jane Doe, defendant".
_INDIVIDUAL_HINT_REGEX = re.compile(
    r"\b(an\s+individual|defendant|plaintiff|respondent|petitioner|appellant|appellee)\b",
    flags=re.IGNORECASE,
)

_GOVERNMENT_TOKENS = {
    "united states",
    "united states of america",
    "commonwealth",
    "state of",
    "people of the state",
    "securities and exchange commission",
    "federal trade commission",
    "department of justice",
}


def normalize_party(raw_name: str) -> NormalizedParty:
    """Stage 1: normalize a raw party string from a docket caption.

    Pure function. No I/O. Unit-testable offline. See CONTEXT.md §"Stage 1".
    """
    if raw_name is None:
        raise ValueError("raw_name must be a non-None string")
    original = raw_name

    # Strip role phrases first ("John Smith, an individual" → "John Smith,")
    name = _INDIVIDUAL_HINT_REGEX.sub("", raw_name).strip().rstrip(",").strip()

    # Unicode normalize (NFKC) to fold width variants and decompose accents.
    name = unicodedata.normalize("NFKC", name)

    # Collapse whitespace early
    name = re.sub(r"\s+", " ", name).strip()

    # Detect government — handled before suffix stripping, since gov entities
    # often contain no corporate suffixes.
    lowered = name.lower()
    if any(tok in lowered for tok in _GOVERNMENT_TOKENS):
        return NormalizedParty(
            raw_name=original,
            normalized_name=lowered,
            party_class="government",
            stripped_suffixes=[],
        )

    # Iteratively strip corporate-form suffixes (some entities have stacked
    # suffixes, e.g., "Acme Holdings, Inc."). Cap iterations to avoid loops.
    stripped: List[str] = []
    current = name
    for _ in range(4):
        m = _SUFFIX_REGEX.search(current)
        if not m:
            break
        stripped.append(current[m.start(): m.end()].strip(" ,."))
        current = current[: m.start()].rstrip(" ,.")

    is_corporate_shape = bool(stripped)
    normalized_core = re.sub(r"\s+", " ", current).strip().lower()
    # Remove stray trailing punctuation
    normalized_core = normalized_core.rstrip(" ,.")

    # Classify
    if is_corporate_shape:
        party_class: PartyClass = "corporate_entity"
        is_possible_executive = False
    elif _INDIVIDUAL_HINT_REGEX.search(original):
        party_class = "individual"
        is_possible_executive = _looks_like_person_name(normalized_core)
    elif _looks_like_person_name(normalized_core):
        party_class = "individual"
        is_possible_executive = True
    else:
        # Neither corporate shape nor person shape — could be a government agency
        # with unusual wording, a trust, or something unparseable.
        party_class = "unknown"
        is_possible_executive = False

    return NormalizedParty(
        raw_name=original,
        normalized_name=normalized_core,
        party_class=party_class,
        stripped_suffixes=stripped,
        is_possible_executive=is_possible_executive,
    )


def _looks_like_person_name(normalized: str) -> bool:
    """Heuristic: 2–4 tokens, each capitalized in the original, no corp keywords.

    Since the input here is already lowercased, we key off token-count + absence
    of corporate-form words. This is coarse; the executive-lookup table (D-010)
    does the real work.
    """
    tokens = normalized.split()
    if not 2 <= len(tokens) <= 4:
        return False
    corp_keywords = {"bank", "capital", "partners", "group", "fund", "advisors",
                     "ventures", "technologies", "systems", "solutions"}
    return not any(t in corp_keywords for t in tokens)


# ----------------------------------------------------------------------------
# Cache I/O
# ----------------------------------------------------------------------------

def _load_cache() -> Dict[str, Dict]:
    if not CACHE_PATH.exists():
        return {}
    try:
        with CACHE_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("entries", {}) if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("party_resolution_cache.json unreadable: %s", e)
        return {}


def _save_cache(entries: Dict[str, Dict]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "description": (
            "Monotonically-growing party→issuer resolution cache per D-009. "
            "Keys are Stage-1 normalized names (lowercase, suffix-stripped)."
        ),
        "entries": entries,
    }
    # Write atomically via rename
    tmp = CACHE_PATH.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True, ensure_ascii=False)
    tmp.replace(CACHE_PATH)


def _load_exhibit21() -> Dict[str, Dict]:
    if not EXHIBIT21_PATH.exists():
        return {}
    try:
        with EXHIBIT21_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("entries", {}) if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("exhibit21_subsidiary_table.json unreadable: %s", e)
        return {}


# ----------------------------------------------------------------------------
# Stage 2 — Resolution chain
# ----------------------------------------------------------------------------

def resolve(
    raw_name: str,
    *,
    allow_openfigi_name: bool = True,
    http_session=None,
) -> Resolution:
    """Full pipeline: normalize → walk fallback chain → return Resolution.

    `http_session` is an optional requests.Session; if None, one is created
    per-call (fine for ad-hoc resolution, wasteful for bulk).

    Stage 2 network callers are stubbed to import `requests` lazily so that
    Stage 1 unit tests can run in an environment without `requests` installed.
    """
    normalized = normalize_party(raw_name)

    if normalized.party_class != "corporate_entity":
        return Resolution(
            raw_name=raw_name,
            normalized_name=normalized.normalized_name,
            party_class=normalized.party_class,
            method="unresolved",
            confidence=0.0,
            resolved_at=_now_iso(),
            notes=f"party_class={normalized.party_class}; Stage 2 skipped per CONTEXT.md Stage 1 classification.",
        )

    cache = _load_cache()
    exhibit21 = _load_exhibit21()

    # 1. Cache exact
    if normalized.normalized_name in cache:
        hit = cache[normalized.normalized_name]
        return Resolution(
            raw_name=raw_name,
            normalized_name=normalized.normalized_name,
            party_class="corporate_entity",
            method="cache_exact",
            confidence=1.0,
            cik=hit.get("cik"),
            ticker=hit.get("ticker"),
            issuer_figi=hit.get("issuer_figi"),
            issuer_name=hit.get("issuer_name"),
            resolved_at=_now_iso(),
            notes="cache hit",
        )

    # Establish shared HTTP session lazily
    session = http_session or _build_session()

    # 2. SEC EDGAR exact
    try:
        r = _resolve_via_edgar_exact(normalized, session)
        if r is not None:
            _write_through_cache(cache, normalized.normalized_name, r)
            return r
    except Exception as e:
        logger.warning("EDGAR-exact resolver raised: %s", e)

    # 3. SEC EDGAR fuzzy
    try:
        r = _resolve_via_edgar_fuzzy(normalized, session)
        if r is not None:
            _write_through_cache(cache, normalized.normalized_name, r)
            return r
    except Exception as e:
        logger.warning("EDGAR-fuzzy resolver raised: %s", e)

    # 4. Exhibit-21 lookup (local, no HTTP)
    r = _resolve_via_exhibit21(normalized, exhibit21)
    if r is not None:
        _write_through_cache(cache, normalized.normalized_name, r)
        return r

    # 5. OpenFIGI NAME (fallback, low precision; opt-out via flag)
    if allow_openfigi_name:
        try:
            r = _resolve_via_openfigi_name(normalized, session)
            if r is not None:
                # Do NOT write-through cache for low-confidence results —
                # a future human-curated cache entry would be overwritten
                # by this stale low-precision result.
                return r
        except Exception as e:
            logger.warning("OpenFIGI-NAME resolver raised: %s", e)

    # 6. Unresolved
    _log_unresolved(raw_name, normalized)
    return Resolution(
        raw_name=raw_name,
        normalized_name=normalized.normalized_name,
        party_class="corporate_entity",
        method="unresolved",
        confidence=0.0,
        resolved_at=_now_iso(),
        notes="all resolution paths exhausted",
    )


# ----------------------------------------------------------------------------
# Stage 2 internal resolvers
# ----------------------------------------------------------------------------

def _resolve_via_edgar_exact(np: NormalizedParty, session) -> Optional[Resolution]:
    """Try an exact-phrase query against EDGAR full-text.

    Endpoint: https://efts.sec.gov/LATEST/search-index?q="<name>"&forms=10-K
    Response shape (verified Phase 1 via the catalog probe): JSON with
      hits.hits[*]._source.ciks, display_names, form, file_date

    Precision rule: we accept only if at least one hit has a `display_names`
    whose normalized form matches ours AND the form is 10-K / 10-K/A
    (weeds out filings where the entity is merely mentioned).
    """
    import requests  # local import: keeps Stage 1 importable without requests

    quoted = f'"{np.raw_name.replace(chr(34), "")}"'
    params = {"q": quoted, "forms": "10-K"}
    url = "https://efts.sec.gov/LATEST/search-index"
    resp = _sec_get(session, url, params=params)
    if resp is None:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None

    hits = (data.get("hits") or {}).get("hits") or []
    for h in hits:
        src = h.get("_source") or {}
        form = (src.get("form") or "").upper()
        if not form.startswith("10-K"):
            continue
        display_names = src.get("display_names") or []
        for dn in display_names:
            # display_names look like "APPLE INC  (AAPL) (CIK 0000320193)"
            cand_normalized = _normalize_display_name(dn)
            if cand_normalized == np.normalized_name:
                cik_list = src.get("ciks") or []
                cik = _zero_pad_cik(cik_list[0]) if cik_list else None
                ticker = _extract_ticker_from_display_name(dn)
                issuer_name = _extract_issuer_name_from_display_name(dn)
                return Resolution(
                    raw_name=np.raw_name,
                    normalized_name=np.normalized_name,
                    party_class="corporate_entity",
                    method="sec_edgar_exact",
                    confidence=0.95,
                    cik=cik,
                    ticker=ticker,
                    issuer_name=issuer_name,
                    resolved_at=_now_iso(),
                    notes=f"EDGAR 10-K display_name exact match: {dn!r}",
                )
    return None


def _resolve_via_edgar_fuzzy(np: NormalizedParty, session) -> Optional[Resolution]:
    """Same endpoint, but accept fuzzy matches within Levenshtein threshold.

    Lazily imports rapidfuzz.
    """
    import requests
    try:
        from rapidfuzz.distance import Levenshtein
    except ImportError:
        logger.warning("rapidfuzz not available; fuzzy resolver disabled")
        return None

    params = {"q": np.raw_name, "forms": "10-K"}  # unquoted → broader match
    url = "https://efts.sec.gov/LATEST/search-index"
    resp = _sec_get(session, url, params=params)
    if resp is None:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None

    hits = (data.get("hits") or {}).get("hits") or []
    best: Optional[Tuple[int, Dict, str]] = None  # (distance, source, display_name)
    for h in hits:
        src = h.get("_source") or {}
        form = (src.get("form") or "").upper()
        if not form.startswith("10-K"):
            continue
        for dn in src.get("display_names") or []:
            cand_normalized = _normalize_display_name(dn)
            dist = Levenshtein.distance(cand_normalized, np.normalized_name)
            if dist <= FUZZY_MAX_DISTANCE and (best is None or dist < best[0]):
                best = (dist, src, dn)

    if best is None:
        return None

    dist, src, dn = best
    cik_list = src.get("ciks") or []
    cik = _zero_pad_cik(cik_list[0]) if cik_list else None
    return Resolution(
        raw_name=np.raw_name,
        normalized_name=np.normalized_name,
        party_class="corporate_entity",
        method="sec_edgar_fuzzy",
        confidence=0.80,
        cik=cik,
        ticker=_extract_ticker_from_display_name(dn),
        issuer_name=_extract_issuer_name_from_display_name(dn),
        resolved_at=_now_iso(),
        notes=f"EDGAR fuzzy match (Levenshtein={dist}) against {dn!r}",
    )


def _resolve_via_exhibit21(np: NormalizedParty, table: Dict[str, Dict]) -> Optional[Resolution]:
    """Look up a subsidiary in the pre-built Exhibit-21 table.

    Table schema (v1):
      {
        "<normalized subsidiary name>": {
          "parent_cik": "0000320193",
          "parent_name": "Apple Inc.",
          "parent_ticker": "AAPL",
          "relationship": "direct" | "indirect",
          "source_form_accession": "0000320193-24-000123",
          "as_of_date": "2024-11-01"
        },
        ...
      }
    """
    hit = table.get(np.normalized_name)
    if hit is None:
        return None
    rel = hit.get("relationship", "direct")
    method: ResolutionMethod = "exhibit21_direct" if rel == "direct" else "exhibit21_indirect"
    confidence = 0.90 if rel == "direct" else 0.75
    return Resolution(
        raw_name=np.raw_name,
        normalized_name=np.normalized_name,
        party_class="corporate_entity",
        method=method,
        confidence=confidence,
        cik=hit.get("parent_cik"),
        ticker=hit.get("parent_ticker"),
        issuer_name=hit.get("parent_name"),
        resolved_at=_now_iso(),
        notes=(
            f"Exhibit-21 {rel} subsidiary of {hit.get('parent_name')!r} "
            f"(accession {hit.get('source_form_accession')})"
        ),
    )


def _resolve_via_openfigi_name(np: NormalizedParty, session) -> Optional[Resolution]:
    """Fallback: OpenFIGI `NAME` idType. Low precision; never write to cache.

    Endpoint: POST https://api.openfigi.com/v3/mapping
    Request body (verified Phase 1): JSON array of jobs; response is parallel array.
    """
    import requests
    url = "https://api.openfigi.com/v3/mapping"
    body = [{"idType": "NAME", "idValue": np.raw_name}]
    try:
        resp = session.post(url, json=body, timeout=HTTP_TIMEOUT_SECONDS)
    except requests.RequestException as e:
        logger.warning("OpenFIGI POST failed: %s", e)
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    if not isinstance(data, list) or not data:
        return None
    entry = data[0]
    mappings = entry.get("data") or []
    if not mappings:
        return None
    # Take the first US-exchange match, if any; else the first mapping at all.
    preferred = next((m for m in mappings if m.get("exchCode") == "US"), mappings[0])
    confidence = 0.70 if preferred.get("exchCode") == "US" else 0.60
    return Resolution(
        raw_name=np.raw_name,
        normalized_name=np.normalized_name,
        party_class="corporate_entity",
        method="openfigi_name",
        confidence=confidence,
        ticker=preferred.get("ticker"),
        issuer_figi=preferred.get("figi"),
        issuer_name=preferred.get("name"),
        resolved_at=_now_iso(),
        notes="OpenFIGI NAME fallback — below 0.85 admission threshold; triaged",
    )


# ----------------------------------------------------------------------------
# HTTP plumbing
# ----------------------------------------------------------------------------

_last_sec_request_ts = 0.0


def _build_session():
    import requests
    s = requests.Session()
    s.headers.update({"Accept": "application/json"})
    return s


def _sec_get(session, url: str, *, params: Optional[Dict] = None):
    """GET an SEC host with operational UA + self-throttle + retry."""
    import requests
    global _last_sec_request_ts

    for attempt in range(HTTP_RETRY_COUNT + 1):
        elapsed = time.monotonic() - _last_sec_request_ts
        if elapsed < SEC_MIN_REQUEST_INTERVAL_SECONDS:
            time.sleep(SEC_MIN_REQUEST_INTERVAL_SECONDS - elapsed)
        try:
            resp = session.get(
                url,
                params=params,
                headers={"User-Agent": OPERATIONAL_UA},
                timeout=HTTP_TIMEOUT_SECONDS,
            )
            _last_sec_request_ts = time.monotonic()
            if resp.status_code == 200:
                return resp
            if resp.status_code in (429, 503):
                time.sleep(HTTP_RETRY_BACKOFF_SECONDS * (2 ** attempt))
                continue
            # Any other status: give up for this call
            logger.info("SEC GET %s → %s", url, resp.status_code)
            return None
        except requests.RequestException as e:
            logger.warning("SEC GET %s attempt %d raised: %s", url, attempt, e)
            time.sleep(HTTP_RETRY_BACKOFF_SECONDS * (2 ** attempt))
    return None


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def _now_iso() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _zero_pad_cik(cik_raw) -> str:
    s = str(cik_raw).strip()
    if not s.isdigit():
        return s
    return s.zfill(10)


_DISPLAY_NAME_CIK_RE = re.compile(r"\(CIK\s*(\d+)\)", flags=re.IGNORECASE)
_DISPLAY_NAME_TICKER_RE = re.compile(r"\(([A-Z0-9\.\-]{1,8})\)")


def _extract_ticker_from_display_name(dn: str) -> Optional[str]:
    # "APPLE INC  (AAPL) (CIK 0000320193)" — first parenthesized group that is not CIK.
    for m in _DISPLAY_NAME_TICKER_RE.finditer(dn):
        candidate = m.group(1)
        if candidate.upper().startswith("CIK"):
            continue
        return candidate
    return None


def _extract_issuer_name_from_display_name(dn: str) -> str:
    # Strip everything from the first "(" onwards
    idx = dn.find("(")
    if idx == -1:
        return dn.strip()
    return dn[:idx].strip()


def _normalize_display_name(dn: str) -> str:
    """Normalize an EDGAR display_name string the same way Stage 1 normalizes a raw name.

    Uses the Stage-1 pipeline so that comparisons are symmetric.
    """
    # Extract the bare issuer name from "NAME  (TICKER) (CIK N)" pattern first.
    bare = _extract_issuer_name_from_display_name(dn)
    return normalize_party(bare).normalized_name


def _write_through_cache(cache: Dict[str, Dict], key: str, r: Resolution) -> None:
    # Only cache high-confidence results (≥ 0.85). Fuzzy and openfigi_name do not cache.
    if r.confidence < 0.85:
        return
    cache[key] = {
        "cik": r.cik,
        "ticker": r.ticker,
        "issuer_figi": r.issuer_figi,
        "issuer_name": r.issuer_name,
        "method": r.method,
        "confidence": r.confidence,
        "last_verified": r.resolved_at,
    }
    _save_cache(cache)


def _log_unresolved(raw_name: str, np: NormalizedParty) -> None:
    """Append an entry to working/unresolved_parties.md per D-009.

    Format is markdown for easy human review; the maintenance task parses
    it by line. See INSTRUCTIONS.md §4 maintenance task.
    """
    UNRESOLVED_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = (
        f"- `{raw_name}` → normalized `{np.normalized_name}` "
        f"(class={np.party_class}; stripped={np.stripped_suffixes}) "
        f"— {_now_iso()}\n"
    )
    with UNRESOLVED_PATH.open("a", encoding="utf-8") as f:
        f.write(line)


# ----------------------------------------------------------------------------
# CLI (for smoke testing)
# ----------------------------------------------------------------------------

def _main() -> None:
    import argparse
    p = argparse.ArgumentParser(description="Resolve a party name.")
    p.add_argument("name", help="Raw party name, e.g. 'Apple Inc.'")
    p.add_argument("--no-openfigi", action="store_true",
                   help="Disable OpenFIGI NAME fallback")
    p.add_argument("--offline", action="store_true",
                   help="Stage 1 only; do not call external APIs")
    args = p.parse_args()

    if args.offline:
        print(json.dumps(asdict(normalize_party(args.name)), indent=2))
        return

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    r = resolve(args.name, allow_openfigi_name=not args.no_openfigi)
    print(json.dumps(asdict(r), indent=2))


if __name__ == "__main__":
    _main()
