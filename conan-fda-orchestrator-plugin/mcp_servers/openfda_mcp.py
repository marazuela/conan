"""openfda_mcp — FastMCP server for openFDA drug endpoints.

Wraps the existing _openfda_get HTTP helper from
modal_workers/ingestion/openfda_ingest.py. Read-only — does NOT write to
documents table.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List, Optional

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "openfda_mcp requires the `mcp` package. Install with `pip install 'mcp[cli]'`."
    ) from exc

from modal_workers.ingestion.openfda_ingest import _openfda_get

mcp = FastMCP(
    name="conan-openfda",
    instructions=(
        "openFDA drug endpoints (drugsfda, label, event). Read-only. Returns raw "
        "results dicts; the regulatory_history sub-agent post-processes."
    ),
)


@mcp.tool()
def drugsfda_approvals(
    sponsor_search: Optional[str] = None,
    application_search: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    limit: int = 25,
) -> Dict[str, Any]:
    """Query drug/drugsfda.json for NDA / BLA application records.

    Args:
        sponsor_search: e.g. 'AXSOME THERAPEUTICS INC'.
        application_search: e.g. 'NDA215877' or 'BLA125514'.
        since/until: ISO date strings (YYYY-MM-DD). Filter by submission_status_date.
        limit: max records (1–100).
    """
    today = date.today()
    since_d = date.fromisoformat(since) if since else (today - timedelta(days=365 * 2))
    until_d = date.fromisoformat(until) if until else today

    clauses: List[str] = [
        f"submissions.submission_status_date:[{since_d.isoformat()} TO {until_d.isoformat()}]",
    ]
    if sponsor_search:
        clauses.append(f'sponsor_name:"{sponsor_search}"')
    if application_search:
        clauses.append(f'application_number:"{application_search}"')

    body = _openfda_get(
        "/drug/drugsfda.json",
        params={
            "search": " AND ".join(clauses),
            "limit": min(max(1, limit), 100),
            "skip": 0,
        },
    ) or {}
    results = body.get("results") or []
    return {"count": len(results), "applications": results}


@mcp.tool()
def labels_recent(
    drug_name: Optional[str] = None,
    application_number: Optional[str] = None,
    limit: int = 10,
) -> Dict[str, Any]:
    """Query drug/label.json for the most recent label records of a drug."""
    clauses: List[str] = []
    if drug_name:
        clauses.append(f'openfda.brand_name:"{drug_name}" openfda.generic_name:"{drug_name}"')
    if application_number:
        clauses.append(f'openfda.application_number:"{application_number}"')
    if not clauses:
        return {"count": 0, "labels": [], "error": "must supply drug_name or application_number"}

    body = _openfda_get(
        "/drug/label.json",
        params={
            "search": " ".join(clauses),
            "limit": min(max(1, limit), 25),
        },
    ) or {}
    results = body.get("results") or []
    return {"count": len(results), "labels": results}


@mcp.tool()
def adverse_events(
    drug_name: str,
    limit: int = 25,
) -> Dict[str, Any]:
    """Query drug/event.json for adverse event reports involving a drug."""
    body = _openfda_get(
        "/drug/event.json",
        params={
            "search": f'patient.drug.medicinalproduct:"{drug_name}"',
            "limit": min(max(1, limit), 100),
        },
    ) or {}
    results = body.get("results") or []
    return {"count": len(results), "events": results}


def main() -> None:  # pragma: no cover
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    main()
