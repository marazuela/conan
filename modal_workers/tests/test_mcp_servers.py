"""Smoke tests for the six v3 MCP servers — assert tool surface + return shape.

Run: python -m pytest modal_workers/tests/test_mcp_servers.py -v
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")
os.environ.setdefault("POLYGON_API_KEY", "x")

# Add the plugin's mcp_servers/ to sys.path so we can import them as plain modules.
PLUGIN_MCP_DIR = (
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    + "/conan-fda-orchestrator-plugin/mcp_servers"
)
if PLUGIN_MCP_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_MCP_DIR)


# Skip the whole module if `mcp` isn't installed locally — these are optional.
mcp = pytest.importorskip("mcp", reason="install with `pip install 'mcp[cli]'`")


def _load(modname: str):
    return importlib.import_module(modname)


def _registered_tools(mod) -> List[str]:
    """Pull the registered tool names off a FastMCP instance.

    FastMCP keeps tools in `_tool_manager._tools` (a dict of name → ToolInfo)
    on recent SDKs; older ones expose `tools`. This helper handles both.
    """
    server = getattr(mod, "mcp", None)
    assert server is not None, f"{mod.__name__}: missing module-level `mcp` instance"
    tm = getattr(server, "_tool_manager", None)
    if tm is not None and hasattr(tm, "_tools"):
        return list(tm._tools.keys())
    if hasattr(server, "tools"):
        return list(server.tools.keys()) if isinstance(server.tools, dict) else list(server.tools)
    pytest.skip("FastMCP internals changed — update _registered_tools helper")


def test_pubmed_mcp_tool_surface():
    mod = _load("pubmed_mcp")
    tools = _registered_tools(mod)
    for expected in ["search", "fetch_abstracts", "fetch_full_text", "citation_graph_expand"]:
        assert expected in tools, f"pubmed_mcp missing tool {expected}"


def test_biorxiv_mcp_tool_surface():
    mod = _load("biorxiv_mcp")
    tools = _registered_tools(mod)
    for expected in ["search", "fetch_preprint_pdf"]:
        assert expected in tools


def test_clinicaltrials_mcp_tool_surface():
    mod = _load("clinicaltrials_mcp")
    tools = _registered_tools(mod)
    for expected in ["search", "by_nct"]:
        assert expected in tools


def test_openfda_mcp_tool_surface():
    mod = _load("openfda_mcp")
    tools = _registered_tools(mod)
    for expected in ["drugsfda_approvals", "labels_recent", "adverse_events"]:
        assert expected in tools


def test_polygon_mcp_tool_surface():
    mod = _load("polygon_mcp")
    tools = _registered_tools(mod)
    for expected in ["get_chain", "get_iv", "straddle_implied_move", "event_window_liquidity"]:
        assert expected in tools


def test_fda_adcomm_mcp_tool_surface():
    mod = _load("fda_adcomm_mcp")
    tools = _registered_tools(mod)
    for expected in ["upcoming", "historical"]:
        assert expected in tools


# ---------- behavioral checks (sample one tool per server) ----------


def test_biorxiv_search_returns_empty_in_v1():
    mod = _load("biorxiv_mcp")
    # FastMCP wraps the function — call the underlying.
    fn = mod.search.fn if hasattr(mod.search, "fn") else mod.search
    out = fn("anything")
    assert out["count"] == 0
    assert out["preprints"] == []


def test_pubmed_search_uses_eutils_module():
    mod = _load("pubmed_mcp")
    fn = mod.search.fn if hasattr(mod.search, "fn") else mod.search
    with patch("modal_workers.providers.pubmed.eutils.search", return_value=["1", "2"]):
        out = fn("BRAF melanoma", limit=10)
    assert out["pmids"] == ["1", "2"]
    assert out["count"] == 2


# ---------- internal_rag + compute (Phase 1B additions) ----------


def test_internal_rag_mcp_tool_surface():
    mod = _load("internal_rag_mcp")
    tools = _registered_tools(mod)
    for expected in [
        "hybrid_search", "get_chunk", "get_document_summary",
        "get_citation_graph", "verify_claim",
    ]:
        assert expected in tools, f"internal_rag_mcp missing tool {expected}"


def test_compute_mcp_tool_surface():
    mod = _load("compute_mcp")
    tools = _registered_tools(mod)
    # D-114 names the five tools — accept either snake variant since
    # FastMCP exposes the function name directly.
    for expected in ["base_rate", "isotonic_calibrate", "brier", "verify_claim"]:
        assert expected in tools, f"compute_mcp missing tool {expected}"
    # similar_resolved_cases or similar_cases — accept either.
    assert ("similar_resolved_cases" in tools) or ("similar_cases" in tools), \
        "compute_mcp missing similar-cases tool"


# ---------- polygon degraded-mode (no POLYGON_API_KEY) ----------


def test_polygon_mcp_degraded_when_key_missing(monkeypatch):
    """Without POLYGON_API_KEY, every tool returns status='degraded' instead
    of raising. The microstructure sub-agent depends on this for graceful
    output."""
    monkeypatch.delenv("POLYGON_API_KEY", raising=False)
    # Force a fresh import so the lazy provider doesn't carry state.
    if "polygon_mcp" in sys.modules:
        del sys.modules["polygon_mcp"]
    mod = _load("polygon_mcp")
    # Reset module-level lazy state so we exercise the missing-key path.
    mod._provider = None
    mod._init_error = None
    fn = mod.get_chain.fn if hasattr(mod.get_chain, "fn") else mod.get_chain
    out = fn("AXSM", expiry=None)
    assert out["status"] == "degraded"
    assert "POLYGON_API_KEY" in out["reason"] or "unset" in out["reason"]
    assert out["count"] == 0
    assert out["chain"] == []
