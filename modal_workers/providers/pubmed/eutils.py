"""PubMed E-utilities fetcher.

Wraps NCBI E-utilities for the literature sub-agent (Stream 4):
  - esearch.fcgi  → PMID list for a query
  - efetch.fcgi   → abstracts (rettype=abstract) or XML full text
  - elink.fcgi    → 1-hop citation graph (pubmed_pubmed_citedin / pubmed_pubmed_refs)

Public, unauthenticated. Rate-limit: 3 req/sec without API key, 10/sec with.
Set NCBI_API_KEY env var to lift the limit.

Failure modes mirror federal_register.py:
  - 404 → None (no match)
  - 429 / 5xx → retry (3 attempts, base backoff 0.5s)
  - other 4xx → PubMedError
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree as ET

import requests

logger = logging.getLogger(__name__)

EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
DEFAULT_TIMEOUT_S = 20.0
DEFAULT_MAX_LIMIT = 50


class PubMedError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"pubmed http {status}: {body[:200]}")
        self.status = status
        self.body = body


@dataclass
class PubMedPaper:
    pmid: str
    title: str
    abstract: str
    authors: List[str]
    journal: Optional[str]
    year: Optional[int]
    doi: Optional[str]
    primary_source_url: str


class PubMedClient:
    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: str = EUTILS_BASE,
        timeout: float = DEFAULT_TIMEOUT_S,
        session: Optional[requests.Session] = None,
        user_agent: Optional[str] = None,
    ):
        self.api_key = api_key or os.environ.get("NCBI_API_KEY")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = session or requests.Session()
        self._headers = {
            "User-Agent": user_agent
            or "Conan/1.0 (FDA literature sub-agent; https://github.com/marazuela/conan)"
        }

    def _request(
        self,
        path: str,
        *,
        params: Dict[str, Any],
        attempts: int = 3,
        backoff_s: float = 0.5,
    ) -> Optional[str]:
        if self.api_key:
            params = {**params, "api_key": self.api_key}
        url = f"{self.base_url}/{path.lstrip('/')}"
        last_exc: Optional[Exception] = None
        for attempt in range(attempts):
            try:
                r = self._session.get(
                    url, params=params, headers=self._headers, timeout=self.timeout
                )
            except requests.RequestException as exc:
                last_exc = exc
                time.sleep(backoff_s * (2 ** attempt))
                continue
            if r.status_code == 404:
                return None
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(backoff_s * (2 ** attempt))
                continue
            if r.status_code >= 400:
                raise PubMedError(r.status_code, r.text)
            return r.text
        if last_exc:
            raise PubMedError(0, str(last_exc))
        raise PubMedError(0, "exhausted retries")

    # ------------------------------------------------------------------ search

    def search(self, query: str, *, limit: int = 25) -> List[str]:
        """esearch.fcgi → list of PMIDs ordered by relevance."""
        limit = min(max(1, limit), DEFAULT_MAX_LIMIT)
        body = self._request(
            "esearch.fcgi",
            params={
                "db": "pubmed",
                "term": query,
                "retmode": "json",
                "retmax": limit,
                "sort": "relevance",
            },
        )
        if not body:
            return []
        try:
            import json as _json

            data = _json.loads(body)
        except Exception:
            logger.warning("pubmed.search: bad JSON for query=%r", query[:80])
            return []
        return list(data.get("esearchresult", {}).get("idlist", []) or [])

    # ----------------------------------------------------------- fetch_abstracts

    def fetch_abstracts(self, pmids: List[str]) -> List[PubMedPaper]:
        """efetch.fcgi rettype=abstract → parsed paper records."""
        if not pmids:
            return []
        body = self._request(
            "efetch.fcgi",
            params={
                "db": "pubmed",
                "id": ",".join(pmids[:DEFAULT_MAX_LIMIT]),
                "retmode": "xml",
                "rettype": "abstract",
            },
        )
        if not body:
            return []
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            logger.warning("pubmed.fetch_abstracts: parse error: %s", exc)
            return []
        out: List[PubMedPaper] = []
        for art in root.findall(".//PubmedArticle"):
            pmid_el = art.find(".//PMID")
            pmid = (pmid_el.text or "").strip() if pmid_el is not None else ""
            if not pmid:
                continue
            title_el = art.find(".//Article/ArticleTitle")
            title = "".join(title_el.itertext()).strip() if title_el is not None else ""
            abstract_parts: List[str] = []
            for ab in art.findall(".//Abstract/AbstractText"):
                label = ab.attrib.get("Label")
                txt = "".join(ab.itertext()).strip()
                if label and txt:
                    abstract_parts.append(f"{label}: {txt}")
                elif txt:
                    abstract_parts.append(txt)
            abstract = "\n".join(abstract_parts)
            authors: List[str] = []
            for au in art.findall(".//AuthorList/Author"):
                last = au.findtext("LastName") or ""
                init = au.findtext("Initials") or ""
                if last:
                    authors.append(f"{last} {init}".strip())
            journal = art.findtext(".//Journal/Title")
            year_text = art.findtext(".//Journal/JournalIssue/PubDate/Year")
            try:
                year = int(year_text) if year_text else None
            except ValueError:
                year = None
            doi = None
            for art_id in art.findall(".//ArticleIdList/ArticleId"):
                if art_id.attrib.get("IdType", "").lower() == "doi":
                    doi = (art_id.text or "").strip() or None
                    break
            out.append(
                PubMedPaper(
                    pmid=pmid,
                    title=title,
                    abstract=abstract,
                    authors=authors,
                    journal=journal,
                    year=year,
                    doi=doi,
                    primary_source_url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                )
            )
        return out

    # ----------------------------------------------------------- fetch_full_text

    def fetch_full_text(self, pmid: str) -> Optional[str]:
        """efetch.fcgi from PubMed Central if available; else abstract.

        PMC-OAI full text only exists for open-access papers; for closed-access
        papers we degrade to abstract-only and the caller decides whether the
        paper is usable.
        """
        # Resolve PMID → PMCID via elink, then efetch from pmc.
        pmcid: Optional[str] = None
        body = self._request(
            "elink.fcgi",
            params={
                "dbfrom": "pubmed",
                "db": "pmc",
                "id": pmid,
                "retmode": "json",
            },
        )
        if body:
            try:
                import json as _json

                data = _json.loads(body)
                links = (
                    data.get("linksets", [{}])[0]
                    .get("linksetdbs", [])
                )
                for ld in links:
                    if ld.get("dbto") == "pmc":
                        ids = ld.get("links") or []
                        if ids:
                            pmcid = f"PMC{ids[0]}"
                            break
            except Exception:
                pmcid = None

        if pmcid:
            text = self._request(
                "efetch.fcgi",
                params={"db": "pmc", "id": pmcid, "retmode": "xml"},
            )
            if text:
                return text  # caller can parse JATS XML if desired

        # Fallback: abstract only
        papers = self.fetch_abstracts([pmid])
        if papers:
            return papers[0].abstract or None
        return None

    # ------------------------------------------------------- citation_graph_expand

    def citation_graph_expand(
        self, pmid: str, *, direction: str = "cited_by", limit: int = 20
    ) -> List[str]:
        """1-hop neighbors via elink.

        direction='cited_by' → papers that CITE this one (pubmed_pubmed_citedin)
        direction='references' → papers this one cites (pubmed_pubmed_refs)
        """
        link_name = {
            "cited_by": "pubmed_pubmed_citedin",
            "references": "pubmed_pubmed_refs",
        }.get(direction, "pubmed_pubmed_citedin")
        body = self._request(
            "elink.fcgi",
            params={
                "dbfrom": "pubmed",
                "linkname": link_name,
                "id": pmid,
                "retmode": "json",
            },
        )
        if not body:
            return []
        try:
            import json as _json

            data = _json.loads(body)
        except Exception:
            return []
        out: List[str] = []
        for ls in data.get("linksets", []) or []:
            for ld in ls.get("linksetdbs", []) or []:
                if ld.get("linkname") == link_name:
                    out.extend(ld.get("links") or [])
        return out[:limit]


# ----------------------------------------------------------- module-level helpers

_default_client: Optional[PubMedClient] = None


def _client() -> PubMedClient:
    global _default_client
    if _default_client is None:
        _default_client = PubMedClient()
    return _default_client


def search(query: str, *, limit: int = 25) -> List[str]:
    return _client().search(query, limit=limit)


def fetch_abstracts(pmids: List[str]) -> List[PubMedPaper]:
    return _client().fetch_abstracts(pmids)


def fetch_full_text(pmid: str) -> Optional[str]:
    return _client().fetch_full_text(pmid)


def citation_graph_expand(pmid: str, *, direction: str = "cited_by", limit: int = 20) -> List[str]:
    return _client().citation_graph_expand(pmid, direction=direction, limit=limit)
