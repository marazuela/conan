"""Lightweight market snapshot enrichment for ticker-backed heuristic scoring.

This module is intentionally narrow:
  - cached yfinance snapshot
  - ADV-in-USD proxy (`adv_usd`)
  - market cap in USD (`market_cap_usd`)
  - price-based valuation cushion proxy (`valuation_cushion_pct`)

It is meant to feed low-risk heuristic dimensions like liquidity and a coarse
valuation cushion proxy. It is NOT a full fundamental model.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from modal_workers.shared.supabase_client import SupabaseClient, SupabaseError

CACHE_TTL_S = 24 * 3600

MIC_TO_YF_SUFFIX = {
    "XNAS": "",
    "XNYS": "",
    "XASE": "",
    "ARCX": "",
    "BATS": "",
    "IEXG": "",
    "XLON": ".L",
    "XPAR": ".PA",
    "XAMS": ".AS",
    "XETR": ".DE",
    "XMAD": ".MC",
    "XMIL": ".MI",
    "XSWX": ".SW",
    "XBRU": ".BR",
    "XWBO": ".VI",
    "XDUB": ".IR",
    "XLIS": ".LS",
    "XSTO": ".ST",
    "XOSL": ".OL",
    "XCSE": ".CO",
    "XHEL": ".HE",
    "XTSE": ".TO",
    "XTSX": ".V",
    "XASX": ".AX",
    "XHKG": ".HK",
    "XTKS": ".T",
    "XNSE": ".NS",
    "XBOM": ".BO",
    "XBMV": ".MX",
    "XBSP": ".SA",
}

CURRENCY_TO_USD = {
    "USD": 1.0,
    "EUR": 1.08,
    "GBP": 1.26,
    "GBX": 0.0126,
    "GBPX": 0.0126,
    "GBp": 0.0126,
    "JPY": 0.0063,
    "CHF": 1.11,
    "SEK": 0.093,
    "NOK": 0.095,
    "DKK": 0.145,
    "HKD": 0.128,
    "AUD": 0.64,
    "CAD": 0.74,
    "INR": 0.012,
    "BRL": 0.19,
    "MXN": 0.059,
}

_MEMO: Dict[str, Optional[Dict[str, Any]]] = {}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _cache_key(ticker: str, mic: Optional[str]) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.@-]", "_", ticker.upper())
    venue = (mic or "NONE").upper()
    return f"{safe}@{venue}"


def _symbol_for(ticker: str, mic: Optional[str]) -> str:
    suffix = MIC_TO_YF_SUFFIX.get((mic or "").upper(), "")
    return f"{ticker.upper()}{suffix}"


def _coerce_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_number(*values: Any) -> Optional[float]:
    for value in values:
        coerced = _coerce_float(value)
        if coerced is not None:
            return coerced
    return None


def _read_cache(client: Optional[SupabaseClient], key: str) -> Optional[Dict[str, Any]]:
    if client is None:
        return None
    try:
        raw = client.read_cache("market-snapshots", f"{key}.json", timeout=4.0)
    except (SupabaseError, Exception):
        return None
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    cached_at = _coerce_float(payload.get("cached_at"))
    if cached_at is None or time.time() - cached_at > CACHE_TTL_S:
        return None
    snapshot = payload.get("snapshot")
    return snapshot if isinstance(snapshot, dict) else None


def _write_cache(client: Optional[SupabaseClient], key: str, snapshot: Dict[str, Any]) -> None:
    if client is None:
        return
    payload = {"cached_at": time.time(), "snapshot": snapshot}
    try:
        client.write_cache(
            "market-snapshots",
            f"{key}.json",
            json.dumps(payload).encode("utf-8"),
            content_type="application/json",
        )
    except (SupabaseError, Exception):
        pass


def load_market_snapshot(
    ticker: str,
    *,
    mic: Optional[str] = None,
    client: Optional[SupabaseClient] = None,
) -> Optional[Dict[str, Any]]:
    if not ticker:
        return None

    key = _cache_key(ticker, mic)
    if key in _MEMO:
        return _MEMO[key]

    cached = _read_cache(client, key)
    if cached is not None:
        _MEMO[key] = cached
        return cached

    snapshot = _fetch_market_snapshot(ticker, mic)
    _MEMO[key] = snapshot
    if snapshot is not None:
        _write_cache(client, key, snapshot)
    return snapshot


def _fetch_market_snapshot(ticker: str, mic: Optional[str]) -> Optional[Dict[str, Any]]:
    try:
        import yfinance as yf  # type: ignore
    except ImportError:
        return None

    symbol = _symbol_for(ticker, mic)
    try:
        instrument = yf.Ticker(symbol)
        fast_info = instrument.fast_info or {}
        info = instrument.info or {}
        history = instrument.history(period="5y", interval="1mo", auto_adjust=True)
    except Exception:
        return None

    currency = (
        info.get("currency")
        or fast_info.get("currency")
        or "USD"
    )
    fx = CURRENCY_TO_USD.get(str(currency), 1.0)

    price = _first_number(
        fast_info.get("lastPrice"),
        fast_info.get("regularMarketPrice"),
        info.get("currentPrice"),
        info.get("regularMarketPrice"),
        info.get("previousClose"),
    )
    avg_volume = _first_number(
        fast_info.get("threeMonthAverageVolume"),
        fast_info.get("tenDayAverageVolume"),
        info.get("averageVolume"),
        info.get("averageVolume10days"),
    )
    market_cap = _first_number(
        fast_info.get("marketCap"),
        info.get("marketCap"),
    )

    valuation_cushion_pct: Optional[float] = None
    price_vs_5y_median_pct: Optional[float] = None
    if price is not None and hasattr(history, "__getitem__") and "Close" in history:
        closes = history["Close"].dropna()
        if len(closes) >= 12:
            median_close = _coerce_float(closes.median())
            if median_close and median_close > 0:
                price_vs_5y_median_pct = round(((median_close - price) / median_close) * 100, 2)
                valuation_cushion_pct = price_vs_5y_median_pct

    adv_usd = round(price * avg_volume * fx, 2) if price is not None and avg_volume is not None else None
    market_cap_usd = round(market_cap * fx, 2) if market_cap is not None else None

    snapshot: Dict[str, Any] = {
        "market_snapshot_source": "yfinance",
        "market_snapshot_symbol": symbol,
        "market_snapshot_at": _utc_now(),
        "adv_usd": adv_usd,
        "market_cap_usd": market_cap_usd,
        "valuation_cushion_pct": valuation_cushion_pct,
        "price_vs_5y_median_pct": price_vs_5y_median_pct,
    }
    if not any(snapshot.get(key) is not None for key in ("adv_usd", "market_cap_usd", "valuation_cushion_pct")):
        return None
    return snapshot
