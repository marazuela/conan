"""
OpenFIGI Entity Resolution Module  (v1.3 — 2026-04-09)
==================================
Maps company identifiers (ticker, ISIN, CUSIP, name) to a canonical entity
using the OpenFIGI v3 API. This is the entity normalization layer that enables
cross-strategy signal matching.

API: https://api.openfigi.com/v3/mapping
- Free, no auth required
- Rate limit: 25 requests/minute (no API key), 250/min with key
- Batch: up to 100 items per request
- v2 sunsets July 1, 2026 — always use v3

Usage:
    from openfigi_resolver import resolve_ticker, resolve_isin, resolve_batch, EntityRecord

    # Single lookups
    entity = resolve_ticker("AAPL")
    entity = resolve_isin("US0378331005")

    # Batch (most efficient)
    results = resolve_batch([
        {"idType": "TICKER", "idValue": "AAPL", "exchCode": "US"},
        {"idType": "ID_ISIN", "idValue": "GB0009252882"},
    ])

    # With caching (default: on, in-memory LRU)
    entity = resolve_ticker("AAPL")  # API call
    entity = resolve_ticker("AAPL")  # cache hit, no API call
"""

import json
import time
import hashlib
import logging
import os
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"
MAX_BATCH_SIZE = 10           # OpenFIGI v3 limit without API key (100 with key)
RATE_LIMIT_PER_MIN = 25       # Without API key
RATE_LIMIT_WINDOW = 60        # seconds
REQUEST_TIMEOUT = 15           # seconds per request
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 2        # exponential backoff: 2^attempt seconds

# Cache settings
CACHE_MAX_SIZE = 5000         # in-memory LRU cap
CACHE_FILE = None             # set to a path to enable persistent JSON cache

# Logging
logger = logging.getLogger("openfigi_resolver")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class EntityRecord:
    """Canonical entity representation after OpenFIGI resolution."""
    composite_figi: str                     # Primary key for cross-strategy matching
    share_class_figi: Optional[str] = None
    name: str = ""
    ticker: str = ""
    exch_code: str = ""                     # Primary exchange code
    security_type: str = ""
    market_sector: str = ""
    # Additional identifiers (populated when available)
    isin: Optional[str] = None
    cusip: Optional[str] = None
    # Metadata
    resolved_at: str = ""                   # ISO timestamp of resolution
    resolution_source: str = "openfigi_v3"

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_figi_response(item: dict, query: dict) -> "EntityRecord":
        """Create EntityRecord from a single OpenFIGI response data item."""
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        record = EntityRecord(
            composite_figi=item.get("compositeFIGI", ""),
            share_class_figi=item.get("shareClassFIGI"),
            name=item.get("name", ""),
            ticker=item.get("ticker", ""),
            exch_code=item.get("exchCode", ""),
            security_type=item.get("securityType2") or item.get("securityType", ""),
            market_sector=item.get("marketSector", ""),
            resolved_at=now,
        )
        # Back-fill identifiers from the query if available
        id_type = query.get("idType", "")
        id_value = query.get("idValue", "")
        if id_type == "ID_ISIN":
            record.isin = id_value
        elif id_type == "ID_CUSIP":
            record.cusip = id_value
        return record


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

class _RateLimiter:
    """Simple sliding-window rate limiter."""

    def __init__(self, max_calls: int = RATE_LIMIT_PER_MIN, window: int = RATE_LIMIT_WINDOW):
        self.max_calls = max_calls
        self.window = window
        self._timestamps: List[float] = []

    def wait_if_needed(self):
        now = time.time()
        # Prune old timestamps
        self._timestamps = [t for t in self._timestamps if now - t < self.window]
        if len(self._timestamps) >= self.max_calls:
            sleep_time = self.window - (now - self._timestamps[0]) + 0.5
            if sleep_time > 0:
                logger.info(f"Rate limit reached, sleeping {sleep_time:.1f}s")
                time.sleep(sleep_time)
        self._timestamps.append(time.time())


_rate_limiter = _RateLimiter()


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

class _Cache:
    """In-memory LRU cache with optional JSON persistence."""

    def __init__(self, max_size: int = CACHE_MAX_SIZE, persist_path: Optional[str] = None):
        self.max_size = max_size
        self.persist_path = persist_path
        self._store: Dict[str, dict] = {}  # key -> EntityRecord.to_dict()
        self._access_order: List[str] = []
        if persist_path and os.path.exists(persist_path):
            try:
                with open(persist_path, "r") as f:
                    loaded = json.load(f)
                self._store = {k: v for k, v in loaded.items()}
                self._access_order = list(self._store.keys())
                logger.info(f"Loaded {len(self._store)} cached entities from {persist_path}")
            except Exception as e:
                logger.warning(f"Failed to load cache from {persist_path}: {e}")

    @staticmethod
    def _make_key(query: dict) -> str:
        """Deterministic cache key from a query dict."""
        canonical = json.dumps(
            {k: query.get(k, "") for k in sorted(["idType", "idValue", "exchCode", "currency"])},
            sort_keys=True
        )
        return hashlib.md5(canonical.encode()).hexdigest()

    def get(self, query: dict) -> Optional[EntityRecord]:
        key = self._make_key(query)
        if key in self._store:
            # Move to end (most recently accessed)
            if key in self._access_order:
                self._access_order.remove(key)
            self._access_order.append(key)
            data = self._store[key]
            return EntityRecord(**data)
        return None

    def put(self, query: dict, entity: EntityRecord):
        key = self._make_key(query)
        self._store[key] = entity.to_dict()
        if key in self._access_order:
            self._access_order.remove(key)
        self._access_order.append(key)
        # Evict oldest if over capacity
        while len(self._store) > self.max_size:
            oldest = self._access_order.pop(0)
            self._store.pop(oldest, None)

    def save(self):
        if self.persist_path:
            try:
                with open(self.persist_path, "w") as f:
                    json.dump(self._store, f)
                logger.debug(f"Saved {len(self._store)} entities to {self.persist_path}")
            except Exception as e:
                logger.warning(f"Failed to save cache: {e}")

    def stats(self) -> dict:
        return {"size": len(self._store), "max_size": self.max_size}


_cache = _Cache(max_size=CACHE_MAX_SIZE, persist_path=CACHE_FILE)


def get_cache_stats() -> dict:
    """Return cache statistics."""
    return _cache.stats()


# ---------------------------------------------------------------------------
# Core API functions
# ---------------------------------------------------------------------------

def _post_with_retry(payload: list, retries: int = MAX_RETRIES) -> list:
    """POST to OpenFIGI v3 with rate limiting and exponential backoff retry."""
    headers = {"Content-Type": "application/json"}

    for attempt in range(retries):
        _rate_limiter.wait_if_needed()
        try:
            resp = requests.post(
                OPENFIGI_URL,
                json=payload,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 429:
                # Rate limited — back off
                wait = RETRY_BACKOFF_BASE ** (attempt + 1)
                logger.warning(f"429 rate limited, backing off {wait}s (attempt {attempt+1}/{retries})")
                time.sleep(wait)
                continue
            elif resp.status_code >= 500:
                wait = RETRY_BACKOFF_BASE ** (attempt + 1)
                logger.warning(f"Server error {resp.status_code}, retrying in {wait}s")
                time.sleep(wait)
                continue
            else:
                logger.error(f"OpenFIGI error {resp.status_code}: {resp.text[:500]}")
                return [{"error": f"HTTP {resp.status_code}"} for _ in payload]
        except requests.exceptions.Timeout:
            wait = RETRY_BACKOFF_BASE ** (attempt + 1)
            logger.warning(f"Timeout, retrying in {wait}s (attempt {attempt+1}/{retries})")
            time.sleep(wait)
        except requests.exceptions.RequestException as e:
            logger.error(f"Request failed: {e}")
            if attempt == retries - 1:
                return [{"error": str(e)} for _ in payload]
            time.sleep(RETRY_BACKOFF_BASE ** (attempt + 1))

    return [{"error": "Max retries exceeded"} for _ in payload]


def _pick_best_match(data_items: list, query: dict) -> Optional[dict]:
    """From multiple matches, pick the best one.

    Priority:
    1. Equity > other market sectors
    2. Common Stock > ADR > other security types
    3. Prefer US exchange, then LN, then others
    4. First result as tiebreaker
    """
    if not data_items:
        return None

    def score(item):
        s = 0
        if item.get("marketSector") == "Equity":
            s += 100
        sec_type = (item.get("securityType2") or item.get("securityType", "")).lower()
        if "common stock" in sec_type:
            s += 50
        elif "adr" in sec_type or "depositary" in sec_type:
            s += 30
        exch = item.get("exchCode", "")
        if exch in ("US", "UN", "UQ", "UA", "UP"):
            s += 20
        elif exch == "LN":
            s += 15
        return s

    sorted_items = sorted(data_items, key=score, reverse=True)
    return sorted_items[0]


def resolve_batch(queries: List[dict], use_cache: bool = True) -> List[Optional[EntityRecord]]:
    """Resolve a batch of identifier queries via OpenFIGI v3.

    Args:
        queries: List of dicts, each with at minimum:
            - idType: "TICKER", "ID_ISIN", "ID_CUSIP", etc.
            - idValue: the identifier value
            Optional:
            - exchCode: exchange code (e.g., "US", "LN")
            - currency: currency filter
        use_cache: whether to check/populate the cache

    Returns:
        List of EntityRecord (or None for failed lookups), same order as input.
    """
    results: List[Optional[EntityRecord]] = [None] * len(queries)
    uncached_indices: List[int] = []
    uncached_queries: List[dict] = []

    # Check cache first
    if use_cache:
        for i, q in enumerate(queries):
            cached = _cache.get(q)
            if cached:
                results[i] = cached
            else:
                uncached_indices.append(i)
                uncached_queries.append(q)
    else:
        uncached_indices = list(range(len(queries)))
        uncached_queries = list(queries)

    if not uncached_queries:
        logger.debug(f"All {len(queries)} queries served from cache")
        return results

    logger.info(f"Resolving {len(uncached_queries)} entities via OpenFIGI ({len(queries) - len(uncached_queries)} cached)")

    # Split into batches of MAX_BATCH_SIZE
    for batch_start in range(0, len(uncached_queries), MAX_BATCH_SIZE):
        batch_queries = uncached_queries[batch_start:batch_start + MAX_BATCH_SIZE]
        batch_idx = uncached_indices[batch_start:batch_start + MAX_BATCH_SIZE]

        api_response = _post_with_retry(batch_queries)

        for j, (resp_item, orig_query) in enumerate(zip(api_response, batch_queries)):
            idx = batch_idx[j]
            if "data" in resp_item and resp_item["data"]:
                best = _pick_best_match(resp_item["data"], orig_query)
                if best:
                    entity = EntityRecord.from_figi_response(best, orig_query)
                    results[idx] = entity
                    if use_cache:
                        _cache.put(orig_query, entity)
            elif "warning" in resp_item:
                logger.debug(f"No match for {orig_query.get('idValue')}: {resp_item['warning']}")
            elif "error" in resp_item:
                logger.warning(f"Error for {orig_query.get('idValue')}: {resp_item['error']}")

    # Persist cache if configured
    if use_cache:
        _cache.save()

    return results


# ---------------------------------------------------------------------------
# Convenience functions
# ---------------------------------------------------------------------------

def resolve_ticker(ticker: str, exch_code: str = "US") -> Optional[EntityRecord]:
    """Resolve a single ticker to an EntityRecord."""
    q = {"idType": "TICKER", "idValue": ticker.upper()}
    if exch_code:
        q["exchCode"] = exch_code
    results = resolve_batch([q])
    return results[0]


def resolve_isin(isin: str) -> Optional[EntityRecord]:
    """Resolve a single ISIN to an EntityRecord."""
    results = resolve_batch([{"idType": "ID_ISIN", "idValue": isin.upper()}])
    return results[0]


def resolve_cusip(cusip: str) -> Optional[EntityRecord]:
    """Resolve a single CUSIP to an EntityRecord."""
    results = resolve_batch([{"idType": "ID_CUSIP", "idValue": cusip.upper()}])
    return results[0]


def resolve_figi(figi: str) -> Optional[EntityRecord]:
    """Resolve a composite FIGI to an EntityRecord.

    Note: OpenFIGI v3 /mapping does not support COMPOSITE_FIGI as idType.
    We use the /v3/search endpoint instead (POST with query=FIGI string).
    """
    # Check cache first
    cache_query = {"idType": "COMPOSITE_FIGI", "idValue": figi}
    cached = _cache.get(cache_query)
    if cached:
        return cached

    _rate_limiter.wait_if_needed()
    try:
        resp = requests.post(
            "https://api.openfigi.com/v3/search",
            json={"query": figi},
            headers={"Content-Type": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 200:
            data = resp.json().get("data", [])
            if data:
                best = _pick_best_match(data, cache_query)
                if best:
                    entity = EntityRecord.from_figi_response(best, cache_query)
                    _cache.put(cache_query, entity)
                    return entity
    except Exception as e:
        logger.warning(f"FIGI search lookup failed for {figi}: {e}")
    return None


def resolve_entity(identifier: str, id_type: str = "auto", exch_code: str = "") -> Optional[EntityRecord]:
    """Smart resolver -- auto-detects identifier type if id_type='auto'.

    Detection heuristic:
    - Starts with 'BBG' and 12 chars -> FIGI (checked first)
    - 12 chars starting with 2 letters -> ISIN
    - 9 alphanumeric chars -> CUSIP
    - Otherwise -> TICKER
    """
    identifier = identifier.strip().upper()

    if id_type == "auto":
        if identifier.startswith("BBG") and len(identifier) == 12:
            # Composite FIGIs: 12 chars starting with BBG -- check before ISIN
            return resolve_figi(identifier)
        elif len(identifier) == 12 and identifier[:2].isalpha():
            id_type = "ID_ISIN"
        elif len(identifier) == 9 and identifier.isalnum():
            id_type = "ID_CUSIP"
        else:
            id_type = "TICKER"

    q: Dict[str, str] = {"idType": id_type, "idValue": identifier}
    if exch_code:
        q["exchCode"] = exch_code
    elif id_type == "TICKER" and not exch_code:
        # Default to US for tickers without exchange specified
        q["exchCode"] = "US"

    results = resolve_batch([q])
    return results[0]


def normalize_signals(signals: List[dict]) -> List[dict]:
    """Given a list of signal dicts (from any scanner tool), enrich each with
    canonical entity info from OpenFIGI.

    Each signal should have at least one of: ticker, isin, cusip, figi.
    Adds/updates: composite_figi, canonical_name, canonical_ticker, exch_code.

    Returns the same list with entity fields added. Signals that fail resolution
    get composite_figi=None (downstream should handle gracefully).
    """
    # Separate signals with FIGI (need search endpoint) from others (use /mapping batch)
    figi_signals = []          # (index, signal) pairs
    batch_signals = []         # (index, signal, query) triples
    queries_for_batch = []     # queries aligned with batch_signals

    for i, sig in enumerate(signals):
        if sig.get("figi"):
            figi_signals.append((i, sig))
        elif sig.get("isin"):
            q = {"idType": "ID_ISIN", "idValue": sig["isin"].upper()}
            batch_signals.append((i, sig, q))
            queries_for_batch.append(q)
        elif sig.get("cusip"):
            q = {"idType": "ID_CUSIP", "idValue": sig["cusip"].upper()}
            batch_signals.append((i, sig, q))
            queries_for_batch.append(q)
        elif sig.get("ticker"):
            q = {"idType": "TICKER", "idValue": sig["ticker"].upper()}
            if sig["ticker"].upper().endswith(".L"):
                q["idValue"] = sig["ticker"].upper().replace(".L", "")
                q["exchCode"] = "LN"
            else:
                q["exchCode"] = "US"
            batch_signals.append((i, sig, q))
            queries_for_batch.append(q)
        else:
            # No usable identifier -- mark as unresolved
            pass

    # Resolve FIGI signals individually (search endpoint)
    entities_by_idx: Dict[int, Optional[EntityRecord]] = {}
    for idx, sig in figi_signals:
        entities_by_idx[idx] = resolve_figi(sig["figi"])

    # Batch resolve the rest via /mapping
    if queries_for_batch:
        batch_results = resolve_batch(queries_for_batch)
        for j, (idx, sig, q) in enumerate(batch_signals):
            entities_by_idx[idx] = batch_results[j]

    # Enrich all signals
    for i, sig in enumerate(signals):
        entity = entities_by_idx.get(i)
        if entity:
            sig["composite_figi"] = entity.composite_figi
            sig["canonical_name"] = entity.name
            sig["canonical_ticker"] = entity.ticker
            sig["exch_code"] = entity.exch_code
            sig["share_class_figi"] = entity.share_class_figi
            if entity.isin:
                sig["isin"] = entity.isin
        else:
            sig["composite_figi"] = None
            sig["canonical_name"] = sig.get("company_name", "")
            sig["canonical_ticker"] = sig.get("ticker", "")

    return signals


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    """CLI entry point for testing and manual lookups."""
    import argparse

    parser = argparse.ArgumentParser(description="OpenFIGI Entity Resolver")
    parser.add_argument("identifiers", nargs="+", help="Identifiers to resolve (tickers, ISINs, CUSIPs, FIGIs)")
    parser.add_argument("--type", default="auto", choices=["auto", "TICKER", "ID_ISIN", "ID_CUSIP", "COMPOSITE_FIGI"],
                        help="Identifier type (default: auto-detect)")
    parser.add_argument("--exchange", default="", help="Exchange code filter (e.g., US, LN)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

    for ident in args.identifiers:
        entity = resolve_entity(ident, id_type=args.type, exch_code=args.exchange)
        if entity:
            if args.json:
                print(json.dumps(entity.to_dict(), indent=2))
            else:
                print(f"{ident} -> {entity.name} | {entity.ticker} ({entity.exch_code}) | "
                      f"FIGI: {entity.composite_figi} | Type: {entity.security_type}")
        else:
            print(f"{ident} -> NOT FOUND")


if __name__ == "__main__":
    main()
# --- END OF FILE ---
