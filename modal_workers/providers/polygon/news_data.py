"""
Polygon news provider.

Endpoints used:
  - /v2/reference/news?ticker={T}&limit=N

Future seam: NewsDataProvider Protocol allows a BiotechNewsProvider (e.g.,
BioPharma Catalyst) to drop in for biotech-specialty feeds in a later phase.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Protocol

from modal_workers.providers.polygon.base import PolygonClient


class NewsDataProvider(Protocol):
    def get_news(
        self,
        ticker: str,
        *,
        limit: int = 50,
        since: Optional[datetime] = None,
    ) -> Optional[List[Dict[str, Any]]]: ...


class PolygonNewsData:
    def __init__(self, client: PolygonClient):
        self.client = client

    def get_news(
        self,
        ticker: str,
        *,
        limit: int = 50,
        since: Optional[datetime] = None,
    ) -> Optional[List[Dict[str, Any]]]:
        params: Dict[str, Any] = {
            "ticker": ticker,
            "limit": min(max(limit, 1), 1000),
            "order": "desc",
            "sort": "published_utc",
        }
        if since is not None:
            params["published_utc.gte"] = since.astimezone(timezone.utc).isoformat()
        body = self.client.get("/v2/reference/news", params=params)
        if not body or not isinstance(body, dict):
            return None
        results = body.get("results")
        if results is None:
            return None
        return [
            {
                "id": r.get("id"),
                "title": r.get("title"),
                "publisher": (r.get("publisher") or {}).get("name"),
                "published_utc": r.get("published_utc"),
                "article_url": r.get("article_url"),
                "tickers": r.get("tickers") or [],
                "keywords": r.get("keywords") or [],
                "description": r.get("description"),
            }
            for r in results
        ]
