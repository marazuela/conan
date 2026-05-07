"""
Federal Register API adapter.

Used by the regulatory specialist agent (Phase 5) to surface FDA staff review
documents, AdCom announcements, and final rules tied to a drug or sponsor.

Endpoints used:
  - /api/v1/documents.json  paginated keyword/date search
  - /api/v1/documents/{id}.json  fetch one document by ID

Public, unauthenticated. Rate limit is generous (~1000 req/hour). Cache 7d via
SupabaseClient.read_cache/write_cache, prefix scanner-caches/federal_register/.

Failure modes (mirrors the Polygon adapter pattern):
  - 404 -> None (treated as "no match")
  - 429 / 5xx -> retry with backoff (3 attempts, base 0.25s)
  - 4xx -> FederalRegisterError raised
"""

from __future__ import annotations

import logging
import time
from datetime import date
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

FEDERAL_REGISTER_BASE = "https://www.federalregister.gov/api/v1"
DEFAULT_TIMEOUT_S = 15.0


class FederalRegisterError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"federal_register http {status}: {body[:200]}")
        self.status = status
        self.body = body


class FederalRegisterClient:
    """HTTP client for the Federal Register documents API."""

    def __init__(
        self,
        *,
        base_url: str = FEDERAL_REGISTER_BASE,
        timeout: float = DEFAULT_TIMEOUT_S,
        session: Optional[requests.Session] = None,
        user_agent: Optional[str] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = session or requests.Session()
        # Federal Register requires/recommends a contact UA on automated callers.
        self._headers = {
            "User-Agent": user_agent
            or "Conan/1.0 (FDA event scanner; https://github.com/marazuela/conan)"
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        attempts: int = 3,
        backoff_s: float = 0.25,
    ) -> Optional[Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        last_exc: Optional[Exception] = None
        for attempt in range(attempts):
            try:
                r = self._session.request(
                    method,
                    url,
                    params=params,
                    headers=self._headers,
                    timeout=self.timeout,
                )
            except requests.exceptions.RequestException as exc:
                last_exc = exc
                if attempt < attempts - 1:
                    time.sleep(backoff_s * (2**attempt))
                    continue
                raise
            if r.status_code == 404:
                return None
            if r.status_code == 429 or r.status_code >= 500:
                last_exc = FederalRegisterError(r.status_code, r.text)
                if attempt < attempts - 1:
                    time.sleep(backoff_s * (2**attempt))
                    continue
                raise last_exc
            if r.status_code >= 400:
                raise FederalRegisterError(r.status_code, r.text)
            try:
                return r.json()
            except ValueError:
                return r.text
        if last_exc is not None:
            raise last_exc
        return None

    # ------------------------------------------------------------------
    # search documents
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        since: Optional[date] = None,
        until: Optional[date] = None,
        agencies: Optional[List[str]] = None,
        document_types: Optional[List[str]] = None,
        per_page: int = 20,
        page: int = 1,
    ) -> Optional[List[Dict[str, Any]]]:
        """Search Federal Register documents.

        agencies: Federal Register agency slugs, e.g. ['food-and-drug-administration'].
        document_types: 'NOTICE', 'RULE', 'PRORULE', 'PRESDOCU' (the API actually
                        accepts shortened forms like 'NOTICE' on the conditions[type][]
                        param).

        Returns up to per_page documents. Caller may iterate by `page`.
        """
        params: Dict[str, Any] = {
            "conditions[term]": query,
            "per_page": min(max(per_page, 1), 1000),
            "page": page,
            "order": "newest",
        }
        if since is not None:
            params["conditions[publication_date][gte]"] = since.isoformat()
        if until is not None:
            params["conditions[publication_date][lte]"] = until.isoformat()
        if agencies:
            for i, slug in enumerate(agencies):
                params[f"conditions[agencies][{i}]"] = slug
        if document_types:
            for i, dt in enumerate(document_types):
                params[f"conditions[type][{i}]"] = dt

        body = self._request("GET", "/documents.json", params=params)
        if not body or not isinstance(body, dict):
            return None
        results = body.get("results")
        if results is None:
            return None
        return [_normalize(r) for r in results]

    # ------------------------------------------------------------------
    # fetch one document by ID
    # ------------------------------------------------------------------

    def get_document(self, document_id: str) -> Optional[Dict[str, Any]]:
        body = self._request("GET", f"/documents/{document_id}.json")
        if not body or not isinstance(body, dict):
            return None
        return _normalize(body)

    # ------------------------------------------------------------------
    # v3: fetch raw text body
    # ------------------------------------------------------------------

    def fetch_full_text(self, document_id: str) -> Optional[Dict[str, Any]]:
        """Fetch document metadata + raw text body. Used by the v3 ingestion path
        (modal_workers/ingestion/federal_register_ingest.py) which writes through
        document_writer to the documents table.

        Returns a dict with the normalized metadata fields plus a `raw_text` key
        containing the document body (typically a few KB to tens of KB; rule
        documents can reach 100KB+). Returns None if the document is missing or
        has no public raw_text_url.

        v2 callers (the regulatory specialist agent) continue to use
        get_document(); this method is additive."""
        body = self._request("GET", f"/documents/{document_id}.json")
        if not body or not isinstance(body, dict):
            return None
        meta = _normalize(body)

        raw_text_url = meta.get("raw_text_url")
        if not raw_text_url:
            logger.warning(
                "federal_register: document %s has no raw_text_url; skipping body fetch",
                document_id,
            )
            return None

        # raw_text_url is a public Federal Register URL serving plain text.
        try:
            r = self._session.get(
                raw_text_url, headers=self._headers, timeout=self.timeout)
        except requests.exceptions.RequestException as exc:
            logger.warning("federal_register: raw_text fetch failed for %s: %s",
                           document_id, exc)
            return None
        if r.status_code != 200:
            logger.warning(
                "federal_register: raw_text fetch %s returned %d for %s",
                raw_text_url, r.status_code, document_id)
            return None

        meta["raw_text"] = r.text
        return meta


def _normalize(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Map Federal Register's record shape to a stable subset.

    The API returns ~30 fields per document; we surface the ones a regulatory
    agent actually cites, plus the html_url and publication_date as the
    canonical citation anchor.
    """
    return {
        "document_number": raw.get("document_number"),
        "title": raw.get("title"),
        "abstract": raw.get("abstract"),
        "publication_date": raw.get("publication_date"),
        "type": raw.get("type"),
        "agency_names": raw.get("agency_names") or [],
        "html_url": raw.get("html_url"),
        "pdf_url": raw.get("pdf_url"),
        "raw_text_url": raw.get("raw_text_url"),
        "topics": raw.get("topics") or [],
    }
