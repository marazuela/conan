"""
Polygon market data: quotes, historical aggregates, market cap, ADV.

Endpoints used:
  - /v2/aggs/ticker/{T}/prev          previous-day OHLC (used for current quote proxy)
  - /v2/aggs/ticker/{T}/range/1/day/{from}/{to}   daily aggregates window
  - /v3/reference/tickers/{T}         ticker metadata incl. market_cap
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, Optional, Protocol

from modal_workers.providers.polygon.base import PolygonClient


class MarketDataProvider(Protocol):
    def get_quote(self, ticker: str) -> Optional[Dict[str, Any]]: ...
    def get_historical_prices(self, ticker: str, days: int) -> Optional[list]: ...
    def get_market_cap(self, ticker: str) -> Optional[float]: ...
    def get_adv(self, ticker: str, days: int = 30) -> Optional[float]: ...


class PolygonMarketData:
    def __init__(self, client: PolygonClient):
        self.client = client

    # --------------------------------------------------------------
    # Quote (last available)
    # --------------------------------------------------------------

    def get_quote(self, ticker: str) -> Optional[Dict[str, Any]]:
        body = self.client.get(f"/v2/aggs/ticker/{ticker}/prev", params={"adjusted": "true"})
        if not body or not isinstance(body, dict):
            return None
        results = body.get("results") or []
        if not results:
            return None
        agg = results[0]
        return {
            "ticker": ticker,
            "close": agg.get("c"),
            "open": agg.get("o"),
            "high": agg.get("h"),
            "low": agg.get("l"),
            "volume": agg.get("v"),
            "vwap": agg.get("vw"),
            "timestamp_ms": agg.get("t"),
        }

    # --------------------------------------------------------------
    # Historical aggregates
    # --------------------------------------------------------------

    def get_historical_prices(self, ticker: str, days: int) -> Optional[list]:
        if days <= 0:
            return []
        end = date.today()
        start = end - timedelta(days=max(days, 1))
        path = (
            f"/v2/aggs/ticker/{ticker}/range/1/day/"
            f"{start.isoformat()}/{end.isoformat()}"
        )
        body = self.client.get(path, params={"adjusted": "true", "sort": "asc", "limit": 50000})
        if not body or not isinstance(body, dict):
            return None
        return body.get("results") or []

    # --------------------------------------------------------------
    # Market cap (USD)
    # --------------------------------------------------------------

    def get_market_cap(self, ticker: str) -> Optional[float]:
        body = self.client.get(f"/v3/reference/tickers/{ticker}")
        if not body or not isinstance(body, dict):
            return None
        results = body.get("results") or {}
        mcap = results.get("market_cap")
        return float(mcap) if mcap is not None else None

    # --------------------------------------------------------------
    # Average Daily Volume in USD (close * volume averaged across N days)
    # --------------------------------------------------------------

    def get_adv(self, ticker: str, days: int = 30) -> Optional[float]:
        rows = self.get_historical_prices(ticker, days)
        if rows is None:
            return None
        usable = [r for r in rows if r.get("c") and r.get("v")]
        if not usable:
            return None
        dollar_volumes = [float(r["c"]) * float(r["v"]) for r in usable]
        return sum(dollar_volumes) / len(dollar_volumes)
