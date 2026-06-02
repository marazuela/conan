"""Tests for `modal_workers.sub_agents._rag_tools` — tool defs, handler
chaining, and runner-level opt-in. No network.

Run: python -m pytest modal_workers/tests/test_sub_agent_rag_tools.py -v
"""
from __future__ import annotations

import os
from typing import Any, Dict

import pytest

os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")


def test_internal_rag_tool_defs_default_corpus_validation():
    from modal_workers.sub_agents._rag_tools import internal_rag_tool_defs

    defs = internal_rag_tool_defs("literature")
    names = [d["name"] for d in defs]
    assert "internal_rag_hybrid_search" in names
    assert "internal_rag_get_chunk" in names
    # default corpus is reflected in the schema
    hs = next(d for d in defs if d["name"] == "internal_rag_hybrid_search")
    assert hs["input_schema"]["properties"]["corpus"]["default"] == "literature"


def test_internal_rag_tool_defs_rejects_invalid_corpus():
    from modal_workers.sub_agents._rag_tools import internal_rag_tool_defs

    with pytest.raises(ValueError):
        internal_rag_tool_defs("bogus")


def test_compute_tool_defs_includes_similar_resolved_cases():
    from modal_workers.sub_agents._rag_tools import compute_tool_defs

    defs = compute_tool_defs()
    assert any(d["name"] == "compute_similar_resolved_cases" for d in defs)


def test_chain_handlers_passes_through_unknown_tool_to_next(monkeypatch):
    from modal_workers.sub_agents._rag_tools import chain_handlers, ToolNotOwned

    def role_h(name: str, inp: Dict[str, Any]):
        if name == "role_only_tool":
            return {"from": "role"}
        raise ValueError(f"unknown tool: {name}")

    def shared_h(name: str, inp: Dict[str, Any]):
        if name == "shared_tool":
            return {"from": "shared"}
        raise ToolNotOwned(name)

    chained = chain_handlers(role_h, shared_h)
    assert chained("role_only_tool", {})["from"] == "role"
    assert chained("shared_tool", {})["from"] == "shared"


def test_chain_handlers_raises_when_no_handler_owns_tool():
    from modal_workers.sub_agents._rag_tools import chain_handlers, ToolNotOwned

    def h1(name, inp):
        raise ToolNotOwned(name)

    def h2(name, inp):
        raise ToolNotOwned(name)

    chained = chain_handlers(h1, h2)
    with pytest.raises((KeyError, ValueError)):
        chained("nonexistent", {})


def test_chain_handlers_propagates_real_backend_keyerror():
    """Round-5 regression: a genuine KeyError from a handler's tool BACKEND
    (e.g. inp['query_term'] on a malformed/missing-arg call) must PROPAGATE — not
    be masked as 'tool not owned' and swallowed into the next handler, which sent
    the model into a retry loop until max_turns -> empty payload."""
    from modal_workers.sub_agents._rag_tools import chain_handlers, ToolNotOwned

    shared_calls = {"n": 0}

    def role_h(name, inp):
        if name == "clinicaltrials_search":
            return {"q": inp["query_term"]}  # KeyError if the model omitted the arg
        raise ValueError(f"unknown tool: {name}")

    def shared_h(name, inp):
        shared_calls["n"] += 1
        raise ToolNotOwned(name)

    chained = chain_handlers(role_h, shared_h)
    with pytest.raises(KeyError) as ei:
        chained("clinicaltrials_search", {})  # missing required query_term
    assert "query_term" in str(ei.value)
    assert not isinstance(ei.value, ToolNotOwned)  # the REAL error, not a routing miss
    assert shared_calls["n"] == 0  # NOT masked + passed downstream


def test_internal_rag_handler_routes_hybrid_search(monkeypatch):
    """make_internal_rag_handler should call rag_handle.hybrid_search with
    the right kwargs and return the results in a `{results: [...]}` envelope."""
    from modal_workers.sub_agents._rag_tools import make_internal_rag_handler

    captured: Dict[str, Any] = {}

    def stub_hs(_sb, query, *, corpus, k, asset_id, document_ids):
        captured.update({
            "query": query, "corpus": corpus, "k": k,
            "asset_id": asset_id, "document_ids": document_ids,
        })
        return [{"chunk_id": "c1", "score": 0.9}]

    monkeypatch.setattr(
        "orchestrator_runtime.rag_handle.hybrid_search", stub_hs,
    )

    class _FakeSb:
        pass

    handler = make_internal_rag_handler(sb=_FakeSb())
    out = handler("internal_rag_hybrid_search", {
        "query": "AXS-05 PDUFA",
        "corpus": "filings",
        "k": 5,
        "asset_id": "asset-1",
    })
    assert out["results"] == [{"chunk_id": "c1", "score": 0.9}]
    assert captured["query"] == "AXS-05 PDUFA"
    assert captured["corpus"] == "filings"
    assert captured["k"] == 5
    assert captured["asset_id"] == "asset-1"


def test_internal_rag_handler_routes_get_chunk(monkeypatch):
    from modal_workers.sub_agents._rag_tools import make_internal_rag_handler

    def stub_gc(_sb, chunk_id, *, with_neighbors):
        return {"id": chunk_id, "siblings_n": with_neighbors}

    monkeypatch.setattr(
        "orchestrator_runtime.rag_handle.get_chunk", stub_gc,
    )

    handler = make_internal_rag_handler(sb=object())
    out = handler("internal_rag_get_chunk", {
        "chunk_id": "c5", "with_neighbors": 2,
    })
    assert out == {"id": "c5", "siblings_n": 2}


def test_internal_rag_handler_unknown_tool_raises_keyerror(monkeypatch):
    from modal_workers.sub_agents._rag_tools import make_internal_rag_handler
    handler = make_internal_rag_handler(sb=object())
    with pytest.raises(KeyError):
        handler("not_a_tool", {})


# -------- runner-level opt-in --------


def test_literature_runner_merges_internal_rag_into_tool_defs():
    from modal_workers.sub_agents.literature import LiteratureRunner

    runner = LiteratureRunner()
    names = [d["name"] for d in runner.effective_tool_defs()]
    # role-specific tools still present
    assert "pubmed_search" in names
    # shared tools merged in
    assert "internal_rag_hybrid_search" in names
    assert "internal_rag_get_chunk" in names


def test_regulatory_history_runner_merges_compute_tools():
    from modal_workers.sub_agents.regulatory_history import (
        RegulatoryHistoryRunner,
    )

    runner = RegulatoryHistoryRunner()
    names = [d["name"] for d in runner.effective_tool_defs()]
    assert "fda_adcomm_historical" in names
    assert "internal_rag_hybrid_search" in names
    assert "compute_similar_resolved_cases" in names


def test_options_runner_does_not_merge_shared_tools():
    """The options sub-agent doesn't benefit from RAG (live market data) and
    should keep its tool surface tight to encourage focused queries."""
    from modal_workers.sub_agents.options_microstructure import (
        OptionsMicrostructureRunner,
    )

    runner = OptionsMicrostructureRunner()
    names = [d["name"] for d in runner.effective_tool_defs()]
    assert "internal_rag_hybrid_search" not in names
    assert "compute_similar_resolved_cases" not in names


def test_wrap_handler_routes_role_and_shared_tools(monkeypatch):
    """When both role and shared handlers are active, calling _wrap_handler's
    chain should dispatch the right one."""
    from modal_workers.sub_agents.literature import LiteratureRunner

    runner = LiteratureRunner()

    # Stub rag_handle so the shared handler doesn't try to hit Supabase.
    monkeypatch.setattr(
        "orchestrator_runtime.rag_handle.hybrid_search",
        lambda *a, **kw: [{"chunk_id": "x", "score": 1.0}],
    )

    def role_h(name, inp):
        if name == "pubmed_search":
            return {"count": 0, "pmids": []}
        raise ValueError(f"unknown tool: {name}")

    chained = runner._wrap_handler(role_h)
    assert chained("pubmed_search", {"query": "q"})["count"] == 0
    assert (
        chained("internal_rag_hybrid_search", {"query": "q"})["results"]
        == [{"chunk_id": "x", "score": 1.0}]
    )
