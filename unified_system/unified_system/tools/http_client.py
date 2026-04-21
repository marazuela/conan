"""
Shared HTTP client — rate-limited, exponential backoff, per-host User-Agent.

Used by all scanners for outbound requests. Adapted from Tool 3's http_client.

Per D-015 (Tool 3 carried forward): per-host User-Agent dispatch. Different
endpoints expect different identification — SEC wants "Name email@..." format,
CourtListener wants bearer token + UA, LSE is lenient but will rate-limit.

Usage:
    from http_client import HttpClient
    client = HttpClient()
    resp = client.get("https://efts.sec.gov/LATEST/search-index?q=...")
    resp = client.get("https://www.courtlistener.com/api/rest/v4/dockets/?q=...")
"""

from __future__ import annotations

import os
import time
import logging
from collections import defaultdict
from typing import Optional, Dict, Any
from urllib.parse import urlparse

import requests

log = logging.getLogger("http_client")


# Per-host configuration. Keys are case-insensitive hostname suffixes.
HOST_CONFIG: Dict[str, Dict[str, Any]] = {
    "sec.gov": {
        "user_agent": "Pedro Research pedro@javiergorordo.example",
        "rate_limit_per_sec": 10,
        "accept": "application/json, text/html;q=0.9",
    },
    "efts.sec.gov": {
        "user_agent": "Pedro Research pedro@javiergorordo.example",
        "rate_limit_per_sec": 10,
        "accept": "application/json",
    },
    "data.sec.gov": {
        "user_agent": "Pedro Research pedro@javiergorordo.example",
        "rate_limit_per_sec": 10,
        "accept": "application/json",
    },
    "courtlistener.com": {
        "user_agent": "Pedro Research pedro@javiergorordo.example",
        "rate_limit_per_sec": 1,  # 5000/hr soft limit -> ~1.4/sec; be gentle
        "accept": "application/json",
        "auth_env": "COURTLISTENER_TOKEN",  # bearer token
    },
    "www.capitoltrades.com": {
        "user_agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36",
        "rate_limit_per_sec": 1,
        "accept": "text/html,application/xhtml+xml",
    },
    "londonstockexchange.com": {
        "user_agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36",
        "rate_limit_per_sec": 2,
        "accept": "application/json, text/html",
    },
    "release.tdnet.info": {
        "user_agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36",
        "rate_limit_per_sec": 2,
        "accept": "text/html",
    },
    "www.asx.com.au": {
        "user_agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36",
        "rate_limit_per_sec": 2,
        "accept": "application/json, text/html",
    },
    "www.sedarplus.ca": {
        "user_agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36",
        "rate_limit_per_sec": 1,
        "accept": "text/html",
    },
    "hkexnews.hk": {
        "user_agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36",
        "rate_limit_per_sec": 2,
        "accept": "text/html",
    },
    "api.openfigi.com": {
        "user_agent": "Pedro Research openfigi-resolver/4.0",
        "rate_limit_per_sec": 4,
        "accept": "application/json",
    },
    "api.fda.gov": {
        "user_agent": "Pedro Research pedro@javiergorordo.example",
        "rate_limit_per_sec": 4,
        "accept": "application/json",
    },
}

DEFAULT_CONFIG = {
    "user_agent": "Pedro Research pedro@javiergorordo.example",
    "rate_limit_per_sec": 2,
    "accept": "application/json, text/html;q=0.9",
}


class HttpClient:
    """Thread-unsafe single-process rate-limited client. Use one per scanner."""

    def __init__(self, default_timeout_s: int = 30):
        self.default_timeout = default_timeout_s
        self._last_request_time: Dict[str, float] = defaultdict(float)
        self._session = requests.Session()

    def _config_for_host(self, url: str) -> Dict[str, Any]:
        host = (urlparse(url).hostname or "").lower()
        # match suffix
        for suffix, cfg in HOST_CONFIG.items():
            if host.endswith(suffix):
                return cfg
        return DEFAULT_CONFIG

    def _wait_for_slot(self, host_key: str, rate_limit_per_sec: float) -> None:
        min_gap = 1.0 / max(rate_limit_per_sec, 0.01)
        last = self._last_request_time[host_key]
        elapsed = time.time() - last
        if elapsed < min_gap:
            time.sleep(min_gap - elapsed)
        self._last_request_time[host_key] = time.time()

    def get(self, url: str, params: Optional[Dict] = None, headers: Optional[Dict] = None,
            timeout_s: Optional[int] = None, max_retries: int = 3) -> requests.Response:
        cfg = self._config_for_host(url)
        host_key = (urlparse(url).hostname or "default").lower()
        self._wait_for_slot(host_key, cfg.get("rate_limit_per_sec", 2))

        merged_headers = {
            "User-Agent": cfg["user_agent"],
            "Accept": cfg.get("accept", "application/json"),
        }
        # Auth via env var
        auth_env = cfg.get("auth_env")
        if auth_env:
            tok = os.environ.get(auth_env)
            if tok:
                merged_headers["Authorization"] = f"Token {tok}"
        if headers:
            merged_headers.update(headers)

        backoff = 1.5
        last_exc: Optional[Exception] = None
        for attempt in range(max_retries):
            try:
                r = self._session.get(
                    url, params=params, headers=merged_headers,
                    timeout=timeout_s or self.default_timeout
                )
                if r.status_code == 429:
                    wait = int(r.headers.get("Retry-After", backoff * (attempt + 1)))
                    log.warning("429 on %s — backing off %ds", host_key, wait)
                    time.sleep(wait)
                    continue
                if r.status_code >= 500:
                    wait = backoff * (attempt + 1)
                    log.warning("%d on %s — backoff %0.1fs", r.status_code, host_key, wait)
                    time.sleep(wait)
                    continue
                return r
            except requests.RequestException as e:
                last_exc = e
                wait = backoff * (attempt + 1)
                log.warning("network error on %s: %s — backoff %0.1fs", host_key, e, wait)
                time.sleep(wait)
        if last_exc:
            raise last_exc
        raise RuntimeError(f"max retries exceeded for {url}")

    def get_json(self, url: str, **kwargs) -> Any:
        r = self.get(url, **kwargs)
        r.raise_for_status()
        return r.json()

    def get_text(self, url: str, **kwargs) -> str:
        r = self.get(url, **kwargs)
        r.raise_for_status()
        return r.text


# Module-level convenience — shared singleton if callers don't need isolation.
_default_client: Optional[HttpClient] = None


def default_client() -> HttpClient:
    global _default_client
    if _default_client is None:
        _default_client = HttpClient()
    return _default_client


if __name__ == "__main__":
    c = default_client()
    print(c.get("https://efts.sec.gov/LATEST/search-index?q=tesla&forms=10-K").status_code)

# --- END OF FILE ---
