"""SEC-backed issuer lookup: party-name → (ticker, cik) without OpenFIGI.

Reads SEC's public company_tickers.json (~9k US-listed issuers) and builds a
normalized-name → {ticker, cik, title} index. Scanners call `resolve_issuer()`
with an extracted party name (from caption_party.extract_corporate_party);
if the name matches a public company, an EntityHints is returned with ticker
and cik populated so downstream resolvers don't have to guess.

One-file Storage cache (scanner-caches/sec-issuers/company_tickers.json) is
shared across scanners and refreshed every 30 days. Bootstraps on first call
from `www.sec.gov/files/company_tickers.json`.

Design:
  - Pure lookup helper, no side effects beyond cache read/write.
  - No OpenFIGI dependency — that's a separate batch pass for the long tail.
  - Match order: exact normalized → suffix-trimmed exact → startswith (unique).
    Ambiguous matches (>1 result) return None to keep the cap firing.
  - Failure modes (network error, bad JSON, empty list) return None, not raise.
    The scanner can still emit the signal; party_resolution_confidence stays
    low and the rubric cap handles it.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests


SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
CACHE_PREFIX = "sec-issuers"
CACHE_FILE = "company_tickers.json"
CACHE_TTL_S = 30 * 24 * 3600  # 30 days — SEC revises this list monthly
REQUEST_TIMEOUT = 15


@dataclass
class IssuerMatch:
    """Result of a successful party-name → issuer resolution."""
    ticker: str
    # CIK is zero-padded for SEC-indexed matches; None for alias matches
    # sourced from the entities table (foreign listings, subsidiaries, and
    # other names absent from SEC's company_tickers.json).
    cik: Optional[str]
    title: str          # SEC's official company name (or entities.name for alias)
    match_kind: str     # "exact" | "suffix_trimmed" | "startswith" | "alias"
    # Pre-known MIC for alias matches (entities.primary_mic). SEC matches leave
    # this None — pre_phase3 fills MIC via OpenFIGI's US-exchange resolve_ticker
    # call. Foreign aliases (e.g. 9926.XHKG for Akeso) cannot use that path,
    # so passing MIC through here avoids a guaranteed-miss OpenFIGI roundtrip.
    mic: Optional[str] = None


# Corporate suffix patterns we strip during normalization so
# "Apple Inc." ↔ "Apple, Inc." ↔ "Apple Inc" ↔ "APPLE INC" all match.
_SUFFIX_STRIP_RE = re.compile(
    r"[,\s]*\b(Inc\.?|Incorporated|Corp\.?|Corporation|LLC|L\.L\.C\.?|"
    r"Ltd\.?|Limited|LP|L\.P\.?|Company|Co\.?|Holdings?|Partners|"
    r"Trust|Bank|N\.A\.?|N\.V\.?|PLC|S\.A\.?|AG|GmbH|AB|SE|B\.V\.?|"
    r"S\.p\.A\.?|S\.r\.l\.?)\b\.?[\s,]*",
    re.IGNORECASE,
)

# Punctuation / whitespace we collapse during normalization.
_PUNCT_COLLAPSE_RE = re.compile(r"[\s.,'&/\\-]+")
# Standalone "and" — SEC filings use "&" interchangeably with the word "and"
# in registrant names (e.g. SEC: "ELI LILLY & CO", CT.gov: "Eli Lilly and
# Company"). Without this collapse, the two normalizations diverge (post-
# punct-strip: "elilly..." vs "elillyand...") and the IssuerIndex misses on
# every major pharma whose sponsor caption uses "and" — surfaced via the
# 2026-05-11 unresolved_sponsor_log telemetry (R4 Phase 1).
# \b boundary protects substrings like "Sandoz", "Holland".
_AND_AS_AMP_RE = re.compile(r"\band\b", re.IGNORECASE)


def _normalize(name: str) -> str:
    """Lowercase, strip all whitespace+punctuation, for exact comparison.

    Treats the word "and" as equivalent to "&" (both get dropped) so SEC and
    CT.gov captions reconcile to the same key.
    """
    if not name:
        return ""
    s = name.lower()
    s = _AND_AS_AMP_RE.sub("", s)
    s = _PUNCT_COLLAPSE_RE.sub("", s)
    return s.strip()


def _strip_suffix(name: str) -> str:
    """Remove trailing corporate suffix(es), lowercase, collapse punctuation.

    Applied iteratively so "Apple Inc." and "Apple Holdings, Inc." both reduce
    to just "apple" / "appleholdings".
    """
    if not name:
        return ""
    s = name
    # Iterate — some captions double-suffix ("Apple Holdings Inc., LLC").
    for _ in range(3):
        new = _SUFFIX_STRIP_RE.sub("", s).strip(" ,.")
        if new == s:
            break
        s = new
    return _normalize(s)


def _coerce_tickers_blob(raw: Any) -> Optional[Dict[str, Dict[str, Any]]]:
    """Validate + normalize SEC's company_tickers.json blob.

    Accepts the native format {"0":{"cik_str":..,"ticker":..,"title":..},...}
    OR our cache format {"cached_at":..,"entries":{...}}.
    Returns entries dict or None on malformed input.
    """
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return None
    if not isinstance(raw, dict):
        return None
    # Our cache wraps the native payload.
    if "entries" in raw and isinstance(raw["entries"], dict):
        raw = raw["entries"]
    return raw


def _fetch_sec_tickers(user_agent: str) -> Optional[Dict[str, Dict[str, Any]]]:
    """Fetch SEC's company_tickers.json. Returns None on any failure."""
    try:
        resp = requests.get(
            SEC_TICKERS_URL,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
    except (requests.exceptions.RequestException, ValueError):
        return None
    return _coerce_tickers_blob(data)


def _load_cached(client: Any) -> Optional[Dict[str, Dict[str, Any]]]:
    """Read the Storage-cached copy; None if missing / stale / corrupt."""
    try:
        raw = client.read_cache(CACHE_PREFIX, CACHE_FILE, timeout=4.0)
    except Exception:  # noqa: BLE001 — best effort
        return None
    if not raw:
        return None
    if isinstance(raw, bytes):
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None
    elif isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except ValueError:
            return None
    else:
        return None
    if not isinstance(payload, dict):
        return None
    cached_at = float(payload.get("cached_at") or 0)
    if time.time() - cached_at > CACHE_TTL_S:
        return None
    entries = payload.get("entries")
    return entries if isinstance(entries, dict) else None


def _save_cached(client: Any, entries: Dict[str, Dict[str, Any]]) -> None:
    """Best-effort save of the tickers map."""
    try:
        client.write_cache(
            CACHE_PREFIX, CACHE_FILE,
            json.dumps({
                "cached_at": time.time(),
                "entries": entries,
            }).encode("utf-8"),
            content_type="application/json",
        )
    except Exception:  # noqa: BLE001
        pass


def _load_entity_aliases(client: Any) -> List[Dict[str, Any]]:
    """Load curated sponsor aliases from the entities table.

    R4 Phase 2A.3: bridge the gap between names absent from SEC's
    company_tickers.json (foreign listings like Akeso 9926.XHKG, private
    subsidiaries like Aragon→JNJ) and the IssuerIndex.resolve path. Seed
    migrations populate entities.primary_ticker/primary_mic/country for
    these; we surface them at IssuerIndex level so universe_resolved fires
    correctly and the resolver returns useful hints (MIC, country) that
    the SEC path can't supply.

    Paginates: PostgREST silently caps result sets at 1000 rows regardless
    of the `limit` parameter (Supabase default max-rows). With ~1600 rows
    already qualifying, a single-page query was returning a non-deterministic
    subset and randomly excluding aliases (Akeso, Aragon, AriBio, Gan & Lee
    all missed in the first prod verification). Loop via offset until we
    get a short page.

    Best-effort: any failure (network, schema drift) returns the accumulated
    rows so far; SEC paths still work standalone.
    """
    rows: List[Dict[str, Any]] = []
    page_size = 1000
    offset = 0
    while True:
        try:
            batch = client._rest("GET", "entities", params={
                "select": "name,primary_ticker,primary_mic,country",
                "primary_ticker": "not.is.null",
                "order": "id",   # deterministic pagination
                "limit": str(page_size),
                "offset": str(offset),
            }) or []
        except Exception:  # noqa: BLE001 — alias index is a soft addon
            return rows
        rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    return rows


class IssuerIndex:
    """In-memory index over SEC's tickers list. One instance per scanner run.

    Build once via `IssuerIndex.load(client, user_agent)`, then call
    `.resolve(name)` per party.
    """

    def __init__(self, entries: Dict[str, Dict[str, Any]],
                 aliases: Optional[List[Dict[str, Any]]] = None):
        self._entries = entries
        # Normalized-name → list of entries (the same name can recur for
        # multi-share-class issuers, e.g., "Alphabet Inc.")
        self._by_norm: Dict[str, List[Dict[str, Any]]] = {}
        self._by_suffix_trimmed: Dict[str, List[Dict[str, Any]]] = {}
        for row in entries.values():
            if not isinstance(row, dict):
                continue
            title = row.get("title") or ""
            if not title:
                continue
            k1 = _normalize(title)
            k2 = _strip_suffix(title)
            if k1:
                self._by_norm.setdefault(k1, []).append(row)
            if k2 and k2 != k1:
                self._by_suffix_trimmed.setdefault(k2, []).append(row)

        # Alias index: entities-table rows with primary_ticker. Checked as a
        # final fallback after SEC paths, so SEC remains authoritative when
        # both have a hit. Collisions on the same normalized key are treated
        # as ambiguous (None) to match SEC convention.
        self._alias_by_norm: Dict[str, List[IssuerMatch]] = {}
        self._alias_by_suffix_trimmed: Dict[str, List[IssuerMatch]] = {}
        for arow in (aliases or []):
            name = (arow.get("name") or "").strip()
            ticker = (arow.get("primary_ticker") or "").strip()
            if not (name and ticker):
                continue
            mic = (arow.get("primary_mic") or "").strip() or None
            match = IssuerMatch(
                ticker=ticker,
                cik=None,
                title=name,
                match_kind="alias",
                mic=mic,
            )
            k1 = _normalize(name)
            k2 = _strip_suffix(name)
            if k1:
                self._alias_by_norm.setdefault(k1, []).append(match)
            if k2 and k2 != k1:
                self._alias_by_suffix_trimmed.setdefault(k2, []).append(match)

    @classmethod
    def load(cls, client: Any, user_agent: str,
             *, skip_cache: bool = False) -> Optional["IssuerIndex"]:
        """Load from cache or fetch from SEC. Returns None if all paths fail.

        Also pulls entity aliases from public.entities so foreign-listed and
        privately-owned sponsors (seeded via the binary_catalyst seed
        migrations) resolve via IssuerIndex.resolve too.
        """
        entries: Optional[Dict[str, Dict[str, Any]]] = None
        if not skip_cache:
            entries = _load_cached(client)
        if entries is None:
            entries = _fetch_sec_tickers(user_agent)
            if entries is not None:
                _save_cached(client, entries)
        if not entries:
            return None
        aliases = _load_entity_aliases(client)
        return cls(entries, aliases=aliases)

    @staticmethod
    def _pick_unique(rows: List[Dict[str, Any]],
                     match_kind: str) -> Optional[IssuerMatch]:
        if not rows:
            return None
        # Multi-class issuers: same title → same CIK. Pick the first.
        ciks = {str(r.get("cik_str") or "") for r in rows}
        if len(ciks) == 1:
            r = rows[0]
            cik = str(r.get("cik_str") or "")
            ticker = str(r.get("ticker") or "")
            title = str(r.get("title") or "")
            if not (cik and ticker and title):
                return None
            return IssuerMatch(
                ticker=ticker, cik=cik.zfill(10),
                title=title, match_kind=match_kind,
            )
        # Ambiguous — multiple distinct issuers share a normalized name.
        return None

    @staticmethod
    def _pick_unique_alias(matches: List[IssuerMatch]) -> Optional[IssuerMatch]:
        """Mirrors _pick_unique for the alias index: collapse multi-class
        entries that share a (ticker, mic), reject genuinely ambiguous keys."""
        if not matches:
            return None
        keys = {(m.ticker, m.mic) for m in matches}
        if len(keys) == 1:
            return matches[0]
        return None

    def resolve(self, party_name: str) -> Optional[IssuerMatch]:
        """Match a caption-extracted party name to a public issuer.

        Tries exact match on fully-normalized name, then suffix-trimmed
        exact, then suffix-trimmed startswith (unique). Final fallback: the
        entity-alias index (seed-migration backed names absent from SEC).
        Returns None on miss or ambiguous match.
        """
        if not party_name:
            return None
        k1 = _normalize(party_name)
        if k1 and k1 in self._by_norm:
            m = self._pick_unique(self._by_norm[k1], "exact")
            if m:
                return m

        k2 = _strip_suffix(party_name)
        if k2 and k2 in self._by_suffix_trimmed:
            m = self._pick_unique(self._by_suffix_trimmed[k2], "suffix_trimmed")
            if m:
                return m
        if k2 and k2 in self._by_norm:
            m = self._pick_unique(self._by_norm[k2], "suffix_trimmed")
            if m:
                return m

        # Alias lookup — curated entities-table entries (seed migrations).
        # Checked AFTER SEC's exact + suffix-trimmed paths (so authoritative
        # SEC matches still win) but BEFORE SEC's startswith fuzzy path.
        # Rationale: alias names are hand-curated and exact; SEC startswith
        # can short-circuit on an unrelated prefix match (e.g. "Aragon
        # Pharmaceuticals" → startswith finds "Aragon Therapeutics" or
        # returns None on ambiguity) and would otherwise prevent the alias
        # fallback from ever firing.
        if k1 and k1 in self._alias_by_norm:
            am = self._pick_unique_alias(self._alias_by_norm[k1])
            if am:
                return am
        if k2 and k2 in self._alias_by_suffix_trimmed:
            am = self._pick_unique_alias(self._alias_by_suffix_trimmed[k2])
            if am:
                return am
        if k2 and k2 in self._alias_by_norm:
            am = self._pick_unique_alias(self._alias_by_norm[k2])
            if am:
                return am

        # Startswith — unique prefix match on suffix-trimmed name.
        # Lowest-priority fuzzy fallback. Guard against short keys (avoid
        # matching every Apple* issuer when someone passes "a") by requiring
        # len(k2) >= 4.
        if k2 and len(k2) >= 4:
            hits: List[Dict[str, Any]] = []
            for k, rows in self._by_suffix_trimmed.items():
                if k.startswith(k2):
                    hits.extend(rows)
                    if len({str(r.get("cik_str") or "") for r in hits}) > 1:
                        return None  # ambiguous
            m = self._pick_unique(hits, "startswith")
            if m:
                return m

        return None
