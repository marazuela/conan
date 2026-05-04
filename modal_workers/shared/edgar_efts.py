"""
Minimal EFTS / EDGAR full-text search helper for non-flagship scanners.

The flagship `edgar_filing_monitor` keeps its own `_efts_search` with
deep metrics + partial-state instrumentation; that's not worth refactoring
through a shared seam. Other scanners that just need a thin EFTS query
(fda_pdufa_pipeline PDUFA + CRL discovery) can use this module.

Reuses the rate limiter from `edgar_filing_monitor` so SEC's 10 req/s
ceiling is enforced globally across whichever scanner is active.
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional

import requests

from modal_workers.scanners.edgar_filing_monitor import (
    EFTS_URL,
    MAX_EFTS_RETRIES,
    REQUEST_TIMEOUT,
    RETRYABLE_STATUS_CODES,
    _http_get,
    _rate_limiter,
    _retry_backoff_s,
)


def efts_search(query: str, date_from: str, date_to: str,
                *, forms: str = "", size: int = 50,
                user_agent: str) -> List[Dict[str, Any]]:
    """Return the raw `hits.hits[]` list from EFTS, or [] on permanent failure.

    Retries on RETRYABLE_STATUS_CODES (429/5xx) up to MAX_EFTS_RETRIES.
    Caller is responsible for shaping the hits — this helper is purpose-built
    for thin discovery passes (PDUFA / CRL 8-K mining) and does not project
    the hit dict the way edgar_filing_monitor._efts_search does.
    """
    params: Dict[str, Any] = {
        "q": query,
        "dateRange": "custom",
        "startdt": date_from,
        "enddt": date_to,
        "from": 0,
        "size": size,
    }
    if forms:
        params["forms"] = forms

    attempt = 0
    while True:
        _rate_limiter.wait()
        try:
            resp = _http_get(EFTS_URL, params=params,
                             headers={"User-Agent": user_agent},
                             timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            return data.get("hits", {}).get("hits", []) or []
        except requests.exceptions.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            retriable = (
                isinstance(exc, (requests.exceptions.Timeout,
                                 requests.exceptions.ConnectionError))
                or status in RETRYABLE_STATUS_CODES
            )
            if retriable and attempt < MAX_EFTS_RETRIES:
                attempt += 1
                time.sleep(_retry_backoff_s(attempt))
                continue
            return []


def fetch_filing_text(file_id: str, cik: str, adsh: str,
                      *, user_agent: str) -> Optional[str]:
    """Fetch a filing's body and return whitespace-collapsed plain text.

    Returns None on parse failure or non-200 response. Used for downstream
    regex extraction (PDUFA date, CRL drug name, etc.).
    """
    parts = file_id.split(":")
    if len(parts) != 2:
        return None
    filename = parts[1]
    cik_clean = cik.lstrip("0") or "0"
    adsh_nodash = adsh.replace("-", "")
    url = f"https://www.sec.gov/Archives/edgar/data/{cik_clean}/{adsh_nodash}/{filename}"
    _rate_limiter.wait()
    try:
        r = _http_get(url, headers={"User-Agent": user_agent},
                      timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            return None
        text = re.sub(r"<[^>]+>", " ", r.text)
        text = re.sub(r"&[^;]+;", " ", text)
        text = re.sub(r"\s+", " ", text)
        return text
    except requests.exceptions.RequestException:
        return None
