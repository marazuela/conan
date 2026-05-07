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

    params: Dict[str, str] = {
        "select": "id,profile,catalyst_type,catalyst_date,ticker,sponsor_name,raw_payload",
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
    params: Dict[str, str] = {
        "select": "id,profile,catalyst_type,catalyst_date,ticker,sponsor_name,raw_payload,material_outcome",
        "catalyst_date": f"lt.{today}",
        "order": "catalyst_date.desc",
        "limit": str(min(max(1, limit), 200)),
    }
    if catalyst_type:
        params["catalyst_type"] = f"eq.{catalyst_type}"
    if sponsor_search:
        params["sponsor_name"] = f"ilike.*{sponsor_search}*"

    rows = _client()._rest("GET", "catalyst_universe", params=params) or []

    if drug_name or indication:
        filtered: List[Dict[str, Any]] = []
        for r in rows:
            payload = r.get("raw_payload") or {}
            blob = " ".join(
                str(v) for v in payload.values() if isinstance(v, (str, int, float))
            ).lower()
            if drug_name and drug_name.lower() not in blob:
                continue
            if indication and indication.lower() not in blob:
                continue
            filtered.append(r)
        rows = filtered

    return {"count": len(rows), "events": rows}


def main() -> None:  # pragma: no cover
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    main()
