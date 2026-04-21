"""
Market Cap Cache — Shared cross-scanner caching for yfinance market cap lookups.

Reduces redundant yfinance API calls when multiple scanners query the same tickers.
Cache entries expire after CACHE_TTL_HOURS to ensure fresh data.

Usage in scanner tools:
    from mcap_cache import get_market_cap_cached
    mcap = get_market_cap_cached("AAPL")  # Returns float (millions) or None
"""

import json
import logging
import os
import time
from typing import Optional

logger = logging.getLogger("mcap_cache")

# Config
CACHE_TTL_HOURS = 24  # Cache entries valid for 24 hours
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_DIR = os.path.dirname(_SCRIPT_DIR)
CACHE_FILE = os.path.join(_PROJECT_DIR, "signals", "mcap_cache.json")

# In-memory cache (populated from file on first call)
_mem_cache: dict = {}
_cache_loaded = False


def _load_cache() -> dict:
    """Load cache from file."""
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_cache(cache: dict):
    """Save cache to file."""
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        with open(CACHE_FILE, "w") as f:
            json.dump(cache, f, indent=2)
    except Exception as e:
        logger.debug(f"Failed to save mcap cache: {e}")


def _is_valid(entry: dict) -> bool:
    """Check if a cache entry is still within TTL."""
    ts = entry.get("timestamp", 0)
    return (time.time() - ts) < (CACHE_TTL_HOURS * 3600)


def get_market_cap_cached(ticker: str) -> Optional[float]:
    """Get market cap in millions, using cache first, then yfinance.

    Returns:
        Market cap in millions (float) or None on failure.
    """
    global _mem_cache, _cache_loaded

    if not ticker:
        return None

    ticker = ticker.upper().strip()

    # Load file cache on first call
    if not _cache_loaded:
        _mem_cache = _load_cache()
        _cache_loaded = True

    # Check cache
    if ticker in _mem_cache and _is_valid(_mem_cache[ticker]):
        mcap = _mem_cache[ticker].get("mcap_mm")
        logger.debug(f"Cache HIT: {ticker} = ${mcap:.0f}M" if mcap else f"Cache HIT (None): {ticker}")
        return mcap

    # Cache miss — fetch from yfinance
    mcap = _fetch_yfinance(ticker)

    # Store in cache (even None results, to avoid re-querying failed tickers)
    _mem_cache[ticker] = {
        "mcap_mm": mcap,
        "timestamp": time.time(),
    }

    # Persist to file every write (lightweight — cache is small)
    _save_cache(_mem_cache)

    return mcap


def _fetch_yfinance(ticker: str) -> Optional[float]:
    """Fetch market cap from yfinance. Returns millions or None."""
    try:
        import yfinance as yf
        stock = yf.Ticker(ticker)
        info = stock.fast_info
        mcap = getattr(info, "market_cap", None)
        if mcap and mcap > 0:
            result = mcap / 1_000_000
            logger.debug(f"yfinance: {ticker} = ${result:.0f}M")
            return result
    except Exception as e:
        logger.debug(f"yfinance lookup failed for {ticker}: {e}")
    return None


def cache_stats() -> dict:
    """Return cache statistics."""
    global _mem_cache, _cache_loaded
    if not _cache_loaded:
        _mem_cache = _load_cache()
        _cache_loaded = True

    total = len(_mem_cache)
    valid = sum(1 for v in _mem_cache.values() if _is_valid(v))
    stale = total - valid
    return {"total": total, "valid": valid, "stale": stale}


if __name__ == "__main__":
    # Self-test
    import sys
    logging.basicConfig(level=logging.DEBUG)

    if len(sys.argv) > 1:
        for t in sys.argv[1:]:
            mcap = get_market_cap_cached(t)
            print(f"{t}: {'$' + f'{mcap:.0f}M' if mcap else 'N/A'}")
    else:
        stats = cache_stats()
        print(f"Cache: {stats['total']} entries ({stats['valid']} valid, {stats['stale']} stale)")
        print(f"File: {CACHE_FILE}")
