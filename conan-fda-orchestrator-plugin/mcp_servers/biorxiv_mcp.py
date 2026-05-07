"""biorxiv_mcp — placeholder for bioRxiv preprint search.

v1 returns an empty result set so the literature sub-agent's allowed-tools list
remains valid without delivering noisy preprint hits. A real implementation
will fetch from https://api.biorxiv.org/details/biorxiv/<doi> + the search API
once preprint coverage is prioritized.
"""

from __future__ import annotations

from typing import Any, Dict, List

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "biorxiv_mcp requires the `mcp` package. Install with `pip install 'mcp[cli]'`."
    ) from exc


mcp = FastMCP(
    name="conan-biorxiv",
    instructions=(
        "bioRxiv preprint search stub. Always returns empty in v1; intended as a "
        "wired-but-dormant tool the literature sub-agent may enumerate."
    ),
)


@mcp.tool()
def search(query: str, limit: int = 10) -> Dict[str, Any]:
    """Search bioRxiv for preprints (stubbed — returns empty in v1)."""
    return {
        "query": query,
        "count": 0,
        "preprints": [],
        "note": "biorxiv_mcp is a v1 stub; no preprints returned.",
    }


@mcp.tool()
def fetch_preprint_pdf(doi: str) -> Dict[str, Any]:
    """Fetch a bioRxiv preprint PDF by DOI (stubbed)."""
    return {"doi": doi, "found": False, "text": ""}


def main() -> None:  # pragma: no cover
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    main()
