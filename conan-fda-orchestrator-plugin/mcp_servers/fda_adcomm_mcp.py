"""fda_adcomm_mcp — FastMCP server for upcoming AdComm + PDUFA dates.

Read-only over catalyst_universe (the canonical event store, populated by
fetchers/universe/fda_adcomm_pdufa.py). The regulatory_history sub-agent uses
this to find prior AdComms relevant to an asset.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List, Optional

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "fda_adcomm_mcp requires the `mcp` package. Install with `pip install 'mcp[cli]'`."
    ) from exc

from modal_workers.shared.supabase_client import SupabaseClient


_sb: Optional[SupabaseClient] = None


def _client() -> SupabaseClient:
    global _sb
    if _sb is None:
        _sb = SupabaseClient()
    return _sb


mcp = FastMCP(
    name="conan-fda-adcomm",
    instructions=(
        "Read-only access to the FDA catalyst_universe table for AdComm and "
        "PDUFA events. Use upcoming() for forward-looking events and "
        "historical() for resolved priors."
    ),
)


@mcp.tool()
def upcoming(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    catalyst_type: Optional[str] = None,
    limit: int = 50,
) -> Dict[str, Any]:
    """List upcoming AdComm / PDUFA events.

    Args:
        start_date: ISO date (default today).
        end_date: ISO date (default today + 180d).
        catalyst_type: 'adcomm' | 'pdufa' (None = both).
        limit: max rows.
    """
    today = date.today()
    s = date.fromisoformat(start_date) if start_date else today
    e = date.fromisoformat(end_date) if end_date else today + timedelta(days=180)

    # NOTE: catalyst_universe has no top-level `sponsor_name` column; sponsor /
    # company name is exposed inside raw_payload (sponsor_name for FDA-derived
    # rows, company_name for SEC-derived). Select raw_payload and let callers
    # pull it from there.
    params: Dict[str, str] = {
        "select": "id,profile,catalyst_type,catalyst_date,ticker,raw_payload",
        "catalyst_date": f"gte.{s.isoformat()}",
        "and": f"(catalyst_date.lte.{e.isoformat()})",
        "order": "catalyst_date.asc",
        "limit": str(min(max(1, limit), 200)),
    }
    if catalyst_type:
        params["catalyst_type"] = f"eq.{catalyst_type}"

    rows = _client()._rest("GET", "catalyst_universe", params=params) or []
    return {"count": len(rows), "events": rows}


@mcp.tool()
def historical(
    drug_name: Optional[str] = None,
    sponsor_search: Optional[str] = None,
    indication: Optional[str] = None,
    catalyst_type: Optional[str] = None,
    limit: int = 50,
) -> Dict[str, Any]:
    """Resolved historical AdComm / PDUFA events (catalyst_date < today)."""
    today = date.today().isoformat()
    # See note in `upcoming()` — sponsor_name lives inside raw_payload, not
    # as a top-level column. We select raw_payload and post-filter in Python.
    params: Dict[str, str] = {
        "select": "id,profile,catalyst_type,catalyst_date,ticker,raw_payload,material_outcome",
        "catalyst_date": f"lt.{today}",
        "order": "catalyst_date.desc",
        # Pull a wider page when a sponsor filter applies so post-filtering
        # has enough rows to choose from (most rows have a sponsor in payload).
        "limit": str(min(max(1, limit * (4 if sponsor_search else 1)), 800)),
    }
    if catalyst_type:
        params["catalyst_type"] = f"eq.{catalyst_type}"

    rows = _client()._rest("GET", "catalyst_universe", params=params) or []

    if sponsor_search or drug_name or indication:
        sponsor_lc = sponsor_search.lower() if sponsor_search else None
        drug_lc = drug_name.lower() if drug_name else None
        indication_lc = indication.lower() if indication else None
        filtered: List[Dict[str, Any]] = []
        for r in rows:
            payload = r.get("raw_payload") or {}
            blob = " ".join(
                str(v) for v in payload.values() if isinstance(v, (str, int, float))
            ).lower()
            if sponsor_lc:
                payload_sponsor = (
                    str(payload.get("sponsor_name") or payload.get("company_name") or "")
                ).lower()
                if sponsor_lc not in payload_sponsor and sponsor_lc not in blob:
                    continue
            if drug_lc and drug_lc not in blob:
                continue
            if indication_lc and indication_lc not in blob:
                continue
            filtered.append(r)
        rows = filtered[:limit]

    return {"count": len(rows), "events": rows}


def main() -> None:  # pragma: no cover
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    main()
