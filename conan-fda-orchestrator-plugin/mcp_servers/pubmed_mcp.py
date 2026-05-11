"""pubmed_mcp — FastMCP server wrapping providers/pubmed/eutils.py.

Tools:
  - search(query, limit) → list of PMIDs
  - fetch_abstracts(pmids) → list of paper records (title/abstract/authors/journal/year/doi/url)
  - fetch_full_text(pmid) → full text from PubMed Central if available, else abstract
  - citation_graph_expand(pmid, direction, limit) → 1-hop neighbors (cited_by | references)

Optional NCBI_API_KEY env var lifts the rate limit from 3/sec to 10/sec.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict, List, Optional

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "pubmed_mcp requires the `mcp` package with FastMCP support. "
        "Install with `pip install 'mcp[cli]'`."
    ) from exc

from modal_workers.providers.pubmed import eutils as _eutils


mcp = FastMCP(
    name="conan-pubmed",
    instructions=(
        "PubMed E-utilities wrapper for the literature sub-agent. Use search to "
        "find PMIDs by relevance, fetch_abstracts to retrieve paper metadata in "
        "bulk, fetch_full_text to pull PMC full text where open-access, and "
        "citation_graph_expand to surface seminal references the index missed."
    ),
)


@mcp.tool()
def search(query: str, limit: int = 25) -> Dict[str, Any]:
    """Search PubMed for PMIDs matching a free-text query.

    Args:
        query: free-text query (drug name, MoA, indication, NCT id, etc.).
        limit: max PMIDs to return (1–50).
    """
    pmids = _eutils.search(query, limit=limit)
    return {"query": query, "count": len(pmids), "pmids": pmids}


@mcp.tool()
def fetch_abstracts(pmids: List[str]) -> Dict[str, Any]:
    """Bulk-fetch paper records for a list of PMIDs."""
    papers = _eutils.fetch_abstracts(pmids)
    return {
        "count": len(papers),
        "papers": [asdict(p) for p in papers],
    }


@mcp.tool()
def fetch_full_text(pmid: str) -> Dict[str, Any]:
    """Pull PMC open-access full text if available; else returns abstract.

    The returned `text` may be JATS XML when sourced from PMC. The caller is
    responsible for parsing if structured extraction is needed.
    """
    text = _eutils.fetch_full_text(pmid)
    return {
        "pmid": pmid,
        "found": text is not None,
        "text": text or "",
    }


@mcp.tool()
def citation_graph_expand(
    pmid: str, direction: str = "cited_by", limit: int = 20
) -> Dict[str, Any]:
    """1-hop citation graph neighbors.

    Args:
        pmid: source paper.
        direction: 'cited_by' (papers citing this) or 'references' (papers this cites).
        limit: max neighbors.
    """
    neighbors = _eutils.citation_graph_expand(pmid, direction=direction, limit=limit)
    return {"pmid": pmid, "direction": direction, "neighbors": neighbors}


def main() -> None:  # pragma: no cover
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    main()
