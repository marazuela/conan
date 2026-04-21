"""
JPX market-cap fetcher — Phase 2.1.

Attaches market_cap_usd_mm to TDnet signals so pipeline_runner.triage does not
reject every Japan signal on the below_market_cap_floor condition.

Source strategy:
  - Primary: yfinance Ticker(f"{code}.T").info["marketCap"]
    * Returns JPY market cap for Tokyo-listed equities.
    * Toyota 7203.T verified live 2026-04-14 → ¥43.35T.
  - JPY→USD conversion: live FX via yfinance "JPY=X" once per scanner run,
    cached in memory. Fallback to 0.0065 (approximate 2026-04) if FX fetch fails.
  - Ticker-form handling: TDnet kjCode may be 4-digit ("7203"), 5-digit with
    check ("47550"), or alphanumeric ("469A0"). yfinance accepts 4-digit and
    4-char alphanumeric (e.g. "469A") appended with ".T". Strip trailing char
    if the 5-char form fails.

Cache:
  - Keyed by `{ticker}.T` → (marketcap_jpy, name, asof_ts).
  - TTL 7 days on disk (working/jpx_mcap_cache.json).
  - Calls yfinance only on miss; typical pipeline run has <100 unique tickers.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

ROOT = Path(__file__).parent.parent
WORKING = ROOT / "working"
WORKING.mkdir(parents=True, exist_ok=True)
CACHE_PATH = WORKING / "jpx_mcap_cache.json"
CACHE_TTL_SECONDS = 7 * 24 * 3600

_JPY_USD_DEFAULT = 0.0065  # fallback for 2026-04
_jpy_usd_runtime: Optional[float] = None


def _load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(cache: dict) -> None:
    try:
        CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning("jpx cache save failed: %s", e)


def _fresh(entry: dict) -> bool:
    asof = entry.get("asof", 0)
    return (time.time() - asof) < CACHE_TTL_SECONDS


def _get_jpy_usd() -> float:
    global _jpy_usd_runtime
    if _jpy_usd_runtime is not None:
        return _jpy_usd_runtime
    try:
        import yfinance as yf
        import warnings
        warnings.filterwarnings("ignore")
        # "JPY=X" means JPY per 1 USD. We want USD per 1 JPY → reciprocal.
        t = yf.Ticker("JPY=X")
        info = t.info or {}
        rate_jpy_per_usd = info.get("regularMarketPrice") or info.get("previousClose")
        if rate_jpy_per_usd and rate_jpy_per_usd > 50:
            _jpy_usd_runtime = 1.0 / float(rate_jpy_per_usd)
            log.info("jpx_market_cap: JPY/USD fetched: 1 JPY = %.6f USD (rate %.2f)",
                     _jpy_usd_runtime, rate_jpy_per_usd)
            return _jpy_usd_runtime
    except Exception as e:
        log.warning("jpx_market_cap: JPY/USD fetch failed: %s — using fallback", e)
    _jpy_usd_runtime = _JPY_USD_DEFAULT
    return _jpy_usd_runtime


def _candidate_symbols(ticker: str) -> list[str]:
    """Yield yfinance candidate symbols for a TDnet kjCode."""
    ticker = ticker.strip().upper()
    candidates = []
    # Exact match first (covers 4-digit canonical + 4-char alphanumeric)
    candidates.append(f"{ticker}.T")
    # For 5-char codes, strip trailing char (check digit or trailing 0)
    if len(ticker) == 5:
        candidates.append(f"{ticker[:4]}.T")
    return candidates


def get_market_cap_usd_mm(ticker: str) -> tuple[Optional[float], Optional[str]]:
    """Return (market_cap_usd_mm, company_name_en) or (None, None) if unresolvable.

    ticker: TDnet kjCode — may be 4-digit numeric, 5-digit numeric, or alphanumeric.
    """
    if not ticker:
        return None, None
    cache = _load_cache()
    for sym in _candidate_symbols(ticker):
        entry = cache.get(sym)
        if entry and _fresh(entry):
            mc_jpy = entry.get("market_cap_jpy")
            name = entry.get("name")
            if mc_jpy:
                rate = _get_jpy_usd()
                return round(mc_jpy * rate / 1e6, 2), name
            if entry.get("not_found"):
                continue  # try next candidate

    # Live fetch
    try:
        import yfinance as yf
        import warnings
        warnings.filterwarnings("ignore")
    except ImportError:
        log.warning("jpx_market_cap: yfinance not installed")
        return None, None

    for sym in _candidate_symbols(ticker):
        try:
            info = yf.Ticker(sym).info or {}
        except Exception as e:
            log.debug("jpx_market_cap: yf %s raised %s", sym, e)
            continue
        mc = info.get("marketCap")
        name = info.get("longName") or info.get("shortName")
        currency = info.get("currency")
        if mc and currency == "JPY":
            cache[sym] = {
                "market_cap_jpy": int(mc),
                "name": name,
                "asof": int(time.time()),
            }
            _save_cache(cache)
            rate = _get_jpy_usd()
            return round(mc * rate / 1e6, 2), name
        else:
            cache[sym] = {"not_found": True, "asof": int(time.time())}

    _save_cache(cache)
    return None, None


def attach_market_caps(signals: list[dict]) -> list[dict]:
    """In-place-style: for each signal with mic='XTKS' and market_cap_usd_mm=None,
    resolve via yfinance and populate market_cap_usd_mm + company_name_en if
    missing. Returns the list (same objects mutated)."""
    resolved = 0
    unresolved = 0
    for sig in signals:
        if sig.get("mic") != "XTKS":
            continue
        if sig.get("market_cap_usd_mm") is not None:
            continue
        tic = sig.get("ticker_local")
        mc, name = get_market_cap_usd_mm(tic)
        if mc is not None:
            sig["market_cap_usd_mm"] = mc
            if not sig.get("company_name_en") and name:
                sig["company_name_en"] = name
            resolved += 1
        else:
            unresolved += 1
    log.info("jpx_market_cap: resolved=%d unresolved=%d", resolved, unresolved)
    return signals


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Smoke test
    for tic in ["7203", "9984", "6758", "3092", "469A0", "469A", "99999"]:
        mc, name = get_market_cap_usd_mm(tic)
        print(f"{tic:8s} mcap_usd_mm={mc!s:>10} name={name}")

# --- END OF FILE ---
