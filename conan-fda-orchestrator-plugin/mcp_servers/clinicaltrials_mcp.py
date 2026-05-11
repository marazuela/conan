"""clinicaltrials_mcp — FastMCP server for ClinicalTrials.gov v2 search.

Wraps the existing _ct_get HTTP helper from
modal_workers/ingestion/clinicaltrials_ingest.py to reuse its retry policy.
Returns raw study dicts; does NOT write to the documents table (sub-agents need
read-only access). The ingestion path already exists for backfill.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "clinicaltrials_mcp requires the `mcp` package. "
        "Install with `pip install 'mcp[cli]'`."
    ) from exc

from modal_workers.ingestion.clinicaltrials_ingest import _ct_get

DEFAULT_PAGE_SIZE = 20

mcp = FastMCP(
    name="conan-clinicaltrials",
    instructions=(
        "ClinicalTrials.gov v2 read-only search. Use search() with a free-text "
        "term + optional phase/status filters; use by_nct() to fetch one or more "
        "trials by NCT id. Returns raw API records — the orchestrator handles "
        "field selection."
    ),
)


@mcp.tool()
def search(
    query_term: str,
    phase: Optional[str] = None,
    status: Optional[str] = None,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> Dict[str, Any]:
    """Search ClinicalTrials.gov for studies matching a free-text query.

    Args:
        query_term: free-text (drug name, sponsor, indication).
        phase: e.g. 'PHASE3'. Optional.
        status: e.g. 'RECRUITING', 'COMPLETED'. Optional.
        page_size: max studies (default 20, max ~50).
    """
    params: Dict[str, Any] = {
        "query.term": query_term,
        "pageSize": min(max(1, page_size), 50),
        "format": "json",
    }
    if phase:
        params["filter.advanced"] = f"AREA[Phase]{phase}"
    if status:
        params["filter.overallStatus"] = status

    body = _ct_get("/studies", params=params) or {}
    studies = body.get("studies") or []
    return {
        "query": query_term,
        "count": len(studies),
        "studies": studies,
    }


@mcp.tool()
def by_nct(nct_ids: List[str]) -> Dict[str, Any]:
    """Fetch one or more studies by NCT id."""
    out: List[Dict[str, Any]] = []
    for nct in nct_ids[:25]:
        body = _ct_get(f"/studies/{nct}", params={"format": "json"})
        if body:
            out.append(body)
    return {"count": len(out), "studies": out}


def main() -> None:  # pragma: no cover
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    main()
