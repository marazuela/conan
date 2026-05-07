"""
Shared HTTP client for the Polygon REST API.

Auth: Polygon expects ?apiKey=<key> on every request, OR a Bearer token. We use
the query-param form so cached URLs are easy to inspect (the apiKey is stripped
before logging by `_redact_url`).

Caching: optional, opt-in per call. Pass `cache_prefix` and `cache_key` to
read_cache/write_cache via SupabaseClient. TTL is enforced by the caller (it
checks the cached payload's "fetched_at" against an age budget). Quote endpoints
default to a 1h soft TTL; reference data (market cap, ticker details) to 7d.

Failure modes:
  - Network errors / 5xx / 429 → retry with backoff (3 attempts, base 0.25s).
  - 404 → return None from the calling provider (treated as "no data").
  - All other 4xx → PolygonError raised.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)

POLYGON_BASE = "https://api.polygon.io"
DEFAULT_TIMEOUT_S = 15.0


class PolygonError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"polygon http {status}: {body[:200]}")
        self.status = status
        self.body = body


def _redact_url(url: str) -> str:
    return re.sub(r"apiKey=[^&]+", "apiKey=***", url)


class PolygonClient:
    """Thin wrapper around requests.Session with retry + apiKey injection."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        base_url: str = POLYGON_BASE,
        timeout: float = DEFAULT_TIMEOUT_S,
        session: Optional[requests.Session] = None,
    ):
        self.api_key = api_key or os.environ.get("POLYGON_API_KEY") or ""
        if not self.api_key:
            raise RuntimeError("POLYGON_API_KEY env var is unset")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = session or requests.Session()

    # ------------------------------------------------------------------
    # HTTP plumbing
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        attempts: int = 3,
        backoff_s: float = 0.25,
    ) -> Optional[Any]:
        merged: Dict[str, Any] = dict(params or {})
        merged["apiKey"] = self.api_key
        url = f"{self.base_url}/{path.lstrip('/')}"
        last_exc: Optional[Exception] = None
        for attempt in range(attempts):
            try:
                r = self._session.request(method, url, params=merged, timeout=self.timeout)
            except requests.exceptions.RequestException as exc:
                last_exc = exc
                if attempt < attempts - 1:
                    time.sleep(backoff_s * (2**attempt))
                    continue
                raise
            if r.status_code == 404:
                return None
            if r.status_code == 429 or r.status_code >= 500:
                last_exc = PolygonError(r.status_code, r.text)
                if attempt < attempts - 1:
                    time.sleep(backoff_s * (2**attempt))
                    continue
                raise last_exc
            if r.status_code >= 400:
                raise PolygonError(r.status_code, r.text)
            try:
                return r.json()
            except ValueError:
                return r.text
        if last_exc is not None:
            raise last_exc
        return None

    def get(self, path: str, *, params: Optional[Dict[str, Any]] = None) -> Optional[Any]:
        return self._request("GET", path, params=params)

    def paginate(
        self,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        max_pages: int = 10,
    ):
        """Yield pages from a Polygon endpoint that returns next_url."""
        url_path: Optional[str] = path
        page_params: Optional[Dict[str, Any]] = dict(params or {})
        for _ in range(max_pages):
            if url_path is None:
                return
            page = self._request("GET", url_path, params=page_params)
            if page is None:
                return
            yield page
            next_url = page.get("next_url") if isinstance(page, dict) else None
            if not next_url:
                return
            # next_url is absolute; strip the base for the next call. The apiKey
            # query param is appended again by _request.
            if next_url.startswith(self.base_url):
                url_path = next_url[len(self.base_url):]
            else:
                url_path = next_url
            page_params = None  # next_url already has params baked in
