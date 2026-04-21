"""
OpenFIGI resolver — v2 port of unified_system/unified_system/tools/openfigi_resolver.py.

The ticker-normalization logic, resolve_* functions, and rate limiter are preserved
VERBATIM from v1 (see PRD §6 "preserved artifacts"). The only structural change is the
cache backend: v1 wrote JSON files under working/openfigi_cache/; v2 exposes hook points
(`set_cache_backend`) so Modal workers can inject a Supabase Storage-backed backend at
startup. Default behaviour remains file-based and matches v1 byte-for-byte, so bridge-mode
and local tests need no changes.

API (public):
  normalize_ticker(ticker, mic) -> str
  resolve_ticker_mic(ticker, mic) -> FigiResolution
  resolve_ticker(ticker, exch_code="US") -> FigiResolution
  resolve_isin(isin) -> FigiResolution
  resolve_batch(queries) -> list[FigiResolution]
  set_cache_backend(load_fn, save_fn) -> None   # Modal-only

Do not re-implement normalize_ticker. See Q-003 / Q-016 and DECISIONS.md — the Japanese
5-char alphanumeric fix is the single most audit-sensitive line in this file.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional, List, Dict, Any, Callable

import requests

OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"

# Cache directory. Configurable via env so Modal / bridge mode / local tests can each point
# at a sensible location. Default mirrors v1 layout at modal_workers/working/openfigi_cache/.
_CACHE_DIR_ENV = os.environ.get("OPENFIGI_CACHE_DIR")
if _CACHE_DIR_ENV:
    CACHE_DIR = Path(_CACHE_DIR_ENV)
else:
    CACHE_DIR = Path(__file__).resolve().parent.parent / "working" / "openfigi_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

REQUEST_TIMEOUT = 15
CACHE_TTL_SECONDS = 7 * 24 * 3600  # 7 days

_API_KEY = os.environ.get("OPENFIGI_API_KEY")
_MAX_BATCH = 100 if _API_KEY else 10
_RATE_LIMIT_WINDOW = 60 if _API_KEY else 6
_RATE_LIMIT_REQS = 250 if _API_KEY else 25

_request_log: List[float] = []
_inmem_cache: Dict[str, Dict[str, Any]] = {}


@dataclass
class FigiResolution:
    ticker_local: Optional[str]
    mic: Optional[str]
    figi: Optional[str]
    issuer_figi: Optional[str]  # compositeFIGI — the convergence key
    name: Optional[str]
    security_type: Optional[str]
    exchange_code: Optional[str]
    isin: Optional[str] = None
    resolved: bool = False
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


# ----------------------------------------------------------------------
# Ticker normalization — including the JP 5-char alphanumeric fix (Q-003)
# ----------------------------------------------------------------------

def normalize_ticker(ticker: str, mic: Optional[str] = None) -> str:
    """Apply exchange-specific ticker normalizations before sending to OpenFIGI.

    JP alphanumeric 5-char fix: tickers like '469A0' drop the trailing '0' to become
    '469A'. Detection: len == 5 AND position 3 is a letter AND position 4 is '0' AND
    the MIC is a known JP MIC (or unspecified).
    """
    if not ticker:
        return ticker
    t = ticker.strip().upper()

    if len(t) == 5 and t[3].isalpha() and t[4] == "0":
        if mic in ("XTKS", "XJPX", "XSAP", "XNGO", "XFKA") or (mic is None):
            return t[:4]
    return t


def _cache_key(id_type: str, id_value: str, mic: Optional[str]) -> str:
    safe_val = re.sub(r"[^A-Za-z0-9_-]", "_", id_value)
    return f"{id_type}__{safe_val}__{mic or 'NOMIC'}"


def _cache_path(key: str) -> Path:
    return CACHE_DIR / f"{key}.json"


def _atomic_write(path: Path, data: Dict[str, Any]) -> None:
    """D-052 — atomic write via tmp + rename."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


# ----------------------------------------------------------------------
# Pluggable cache backend
#
# Default (file-based) is a verbatim port of v1. Modal workers swap in a Storage-backed
# implementation at start via `set_cache_backend(load_fn, save_fn)`. The in-memory LRU
# wraps whichever backend is active.
# ----------------------------------------------------------------------

def _file_load_cache(key: str) -> Optional[Dict[str, Any]]:
    path = _cache_path(key)
    if not path.exists():
        return None
    age = time.time() - path.stat().st_mtime
    if age > CACHE_TTL_SECONDS:
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _file_save_cache(key: str, data: Dict[str, Any]) -> None:
    try:
        _atomic_write(_cache_path(key), data)
    except OSError:
        pass  # best effort; caller doesn't depend on disk cache


_load_cache_hook: Callable[[str], Optional[Dict[str, Any]]] = _file_load_cache
_save_cache_hook: Callable[[str, Dict[str, Any]], None] = _file_save_cache


def set_cache_backend(
    load_fn: Callable[[str], Optional[Dict[str, Any]]],
    save_fn: Callable[[str, Dict[str, Any]], None],
) -> None:
    """Install an alternate cache backend. Modal workers call this once at startup to
    route cache reads/writes through Supabase Storage. File-based default is unchanged
    for local dev and bridge mode."""
    global _load_cache_hook, _save_cache_hook
    _load_cache_hook = load_fn
    _save_cache_hook = save_fn


def _load_cache(key: str) -> Optional[Dict[str, Any]]:
    if key in _inmem_cache:
        return _inmem_cache[key]
    data = _load_cache_hook(key)
    if data is not None:
        _inmem_cache[key] = data
    return data


def _save_cache(key: str, data: Dict[str, Any]) -> None:
    _inmem_cache[key] = data
    _save_cache_hook(key, data)


# ----------------------------------------------------------------------
# Rate limiter
# ----------------------------------------------------------------------

def _wait_for_rate_slot() -> None:
    global _request_log
    now = time.time()
    _request_log = [t for t in _request_log if now - t < _RATE_LIMIT_WINDOW]
    if len(_request_log) >= _RATE_LIMIT_REQS:
        sleep_for = _RATE_LIMIT_WINDOW - (now - _request_log[0]) + 0.1
        if sleep_for > 0:
            time.sleep(sleep_for)
    _request_log.append(time.time())


# ----------------------------------------------------------------------
# Core API call
# ----------------------------------------------------------------------

def _post_batch(queries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """POST a batch to OpenFIGI. Returns response list (one per query)."""
    headers = {"Content-Type": "application/json"}
    if _API_KEY:
        headers["X-OPENFIGI-APIKEY"] = _API_KEY
    _wait_for_rate_slot()
    try:
        r = requests.post(OPENFIGI_URL, headers=headers, json=queries, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        return [{"error": f"network: {e}"} for _ in queries]
    if r.status_code == 429:
        time.sleep(6)
        return _post_batch(queries)  # retry once
    if r.status_code != 200:
        return [{"error": f"http {r.status_code}"} for _ in queries]
    try:
        return r.json()
    except ValueError:
        return [{"error": "invalid json response"} for _ in queries]


def _first_match_to_resolution(query: Dict[str, Any], response: Dict[str, Any]) -> FigiResolution:
    if "error" in response:
        return FigiResolution(
            ticker_local=query.get("idValue"),
            mic=query.get("micCode"),
            figi=None, issuer_figi=None, name=None,
            security_type=None, exchange_code=None,
            resolved=False, error=response.get("error"),
        )
    data = response.get("data") or []
    if not data:
        return FigiResolution(
            ticker_local=query.get("idValue"),
            mic=query.get("micCode"),
            figi=None, issuer_figi=None, name=None,
            security_type=None, exchange_code=None,
            resolved=False, error="no match",
        )
    # Prefer Common Stock / Equity types over warrants/options if multiple.
    preferred = None
    for d in data:
        st = (d.get("securityType") or "").lower()
        if "common" in st or "equity" in st or "ord" in st:
            preferred = d
            break
    m = preferred or data[0]
    # OpenFIGI's response object `m` carries the exchange-native ticker at
    # `m["ticker"]`. Prefer it so that ISIN/FIGI-keyed queries return the actual
    # ticker, not the query's idValue (which would be the ISIN/FIGI string).
    # Fallback to idValue preserves the TICKER_MIC-flavored behavior where the
    # caller already supplied a ticker.
    return FigiResolution(
        ticker_local=m.get("ticker") or query.get("idValue"),
        mic=query.get("micCode"),
        figi=m.get("figi"),
        issuer_figi=m.get("compositeFIGI"),
        name=m.get("name"),
        security_type=m.get("securityType"),
        exchange_code=m.get("exchCode"),
        isin=None,
        resolved=bool(m.get("figi")),
        error=None,
    )


# ----------------------------------------------------------------------
# Public resolve functions
# ----------------------------------------------------------------------

def resolve_ticker_mic(ticker: str, mic: str) -> FigiResolution:
    """Resolve a ticker + MIC pair. Preferred resolution for non-US markets.

    Applies JP alphanumeric normalization before querying.
    """
    if not ticker or not mic:
        return FigiResolution(
            ticker_local=ticker, mic=mic, figi=None, issuer_figi=None,
            name=None, security_type=None, exchange_code=None,
            resolved=False, error="missing ticker or mic",
        )
    norm_ticker = normalize_ticker(ticker, mic)
    key = _cache_key("TICKER_MIC", norm_ticker, mic)
    cached = _load_cache(key)
    if cached is not None:
        return FigiResolution(**cached)

    query = {"idType": "TICKER", "idValue": norm_ticker, "micCode": mic}
    results = _post_batch([query])
    resolution = _first_match_to_resolution(query, results[0])
    _save_cache(key, resolution.to_dict())
    return resolution


def resolve_ticker(ticker: str, exch_code: Optional[str] = "US") -> FigiResolution:
    """Resolve a bare ticker (US by default). Exchange code is 'US' for US listings,
    'LN' for LSE, 'JP' for JPX, etc.
    """
    if not ticker:
        return FigiResolution(
            ticker_local=ticker, mic=None, figi=None, issuer_figi=None,
            name=None, security_type=None, exchange_code=None,
            resolved=False, error="missing ticker",
        )
    norm_ticker = normalize_ticker(ticker)
    key = _cache_key("TICKER_EXCH", norm_ticker, exch_code)
    cached = _load_cache(key)
    if cached is not None:
        return FigiResolution(**cached)

    query: Dict[str, Any] = {"idType": "TICKER", "idValue": norm_ticker}
    if exch_code:
        query["exchCode"] = exch_code
    results = _post_batch([query])
    resolution = _first_match_to_resolution(query, results[0])
    _save_cache(key, resolution.to_dict())
    return resolution


def resolve_isin(isin: str) -> FigiResolution:
    if not isin:
        return FigiResolution(
            ticker_local=None, mic=None, figi=None, issuer_figi=None,
            name=None, security_type=None, exchange_code=None,
            resolved=False, error="missing isin",
        )
    key = _cache_key("ID_ISIN", isin, None)
    cached = _load_cache(key)
    if cached is not None:
        return FigiResolution(**cached)

    query = {"idType": "ID_ISIN", "idValue": isin}
    results = _post_batch([query])
    resolution = _first_match_to_resolution(query, results[0])
    resolution.isin = isin
    _save_cache(key, resolution.to_dict())
    return resolution


def resolve_batch(queries: List[Dict[str, Any]]) -> List[FigiResolution]:
    """Batch resolution. Each query is a dict with idType, idValue, optional micCode/exchCode.

    Uses cache where possible and only sends fresh queries to the API.
    """
    out: List[Optional[FigiResolution]] = [None] * len(queries)
    to_post: List[Dict[str, Any]] = []
    to_post_indices: List[int] = []

    for i, q in enumerate(queries):
        tv = q.get("idValue")
        mic = q.get("micCode")
        if q.get("idType") == "TICKER":
            tv = normalize_ticker(tv or "", mic)
            q = {**q, "idValue": tv}
        cache_k = _cache_key(q.get("idType", "?"), tv or "?", mic)
        cached = _load_cache(cache_k)
        if cached is not None:
            out[i] = FigiResolution(**cached)
        else:
            to_post.append(q)
            to_post_indices.append(i)

    for start in range(0, len(to_post), _MAX_BATCH):
        chunk = to_post[start:start + _MAX_BATCH]
        chunk_indices = to_post_indices[start:start + _MAX_BATCH]
        responses = _post_batch(chunk)
        for j, (q, r) in enumerate(zip(chunk, responses)):
            res = _first_match_to_resolution(q, r)
            out[chunk_indices[j]] = res
            cache_k = _cache_key(q.get("idType", "?"), q.get("idValue") or "?", q.get("micCode"))
            _save_cache(cache_k, res.to_dict())

    return [o for o in out if o is not None]
