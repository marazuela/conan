"""Tests for Phase 2B — `stage_1_rag_retrieve` + RAG chunks rendered into
the Stage 1 user content. No network.

Run: python -m pytest orchestrator_runtime/tests/test_stage_1_rag.py -v
"""
from __future__ import annotations

import os
from typing import Any, Dict, List

os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")


def _ctx(**overrides: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "asset": {
            "id": "asset-1",
            "ticker": "AXSM",
            "drug_name": "AXS-05",
            "indication": "Major depressive disorder",
            "indication_normalized": "MDD",
        },
        "facts": [],
        "documents": [],
        "memory_text": None,
        "memory_blobs": None,
        "asset_doc_links": [],
        "reference_class_anchor": None,
    }
    base.update(overrides)
    return base


def test_stage_1_rag_retrieve_populates_rag_chunks(monkeypatch):
    from orchestrator_runtime import runtime

    captured: Dict[str, Any] = {}

    def stub_hs(_sb, query, *, corpus, k, asset_id, document_ids=None):
        captured["query"] = query
        captured["corpus"] = corpus
        captured["k"] = k
        captured["asset_id"] = asset_id
        return [
            {"chunk_id": "c1", "document_id": "d1", "chunk_text": "x",
             "contextual_prefix": None, "section_path": [],
             "score": 0.9, "rerank_score": 0.9,
             "source": "pubmed", "title": "T", "published_at": "2024-06-01"},
        ]

    monkeypatch.setattr(
        "orchestrator_runtime.rag_handle.hybrid_search", stub_hs,
    )

    ctx = _ctx()
    metric = runtime.stage_1_rag_retrieve(object(), ctx, k=4)
    assert ctx["rag_chunks"] is not None
    assert len(ctx["rag_chunks"]) == 1
    assert metric.notes["n_chunks"] == 1
    # Query is indication + drug_name
    assert "MDD" in captured["query"] and "AXS-05" in captured["query"]
    assert captured["corpus"] == "all"
    assert captured["k"] == 4
    assert captured["asset_id"] is None  # asset_scoped=False default


def test_stage_1_rag_retrieve_asset_scoped_passes_id(monkeypatch):
    from orchestrator_runtime import runtime

    seen: Dict[str, Any] = {}

    def stub_hs(_sb, _q, *, corpus, k, asset_id, document_ids=None):
        seen["asset_id"] = asset_id
        return []

    monkeypatch.setattr(
        "orchestrator_runtime.rag_handle.hybrid_search", stub_hs,
    )

    ctx = _ctx()
    runtime.stage_1_rag_retrieve(object(), ctx, k=2, asset_scoped=True)
    assert seen["asset_id"] == "asset-1"


def test_stage_1_rag_retrieve_handles_search_failure(monkeypatch):
    """If hybrid_search raises (e.g. RPC missing pre-backfill), the function
    degrades to an empty list and Stage 1 falls through to legacy path."""
    from orchestrator_runtime import runtime

    def boom(*_a, **_kw):
        raise RuntimeError("rag_dense_search RPC unavailable")

    monkeypatch.setattr(
        "orchestrator_runtime.rag_handle.hybrid_search", boom,
    )

    ctx = _ctx()
    metric = runtime.stage_1_rag_retrieve(object(), ctx)
    assert ctx["rag_chunks"] == []
    assert metric.notes["n_chunks"] == 0


def test_stage_1_rag_retrieve_empty_query_short_circuits(monkeypatch):
    """If asset has no drug, indication, or ticker, skip search entirely."""
    from orchestrator_runtime import runtime

    called = {"n": 0}

    def stub_hs(*_a, **_kw):
        called["n"] += 1
        return []

    monkeypatch.setattr(
        "orchestrator_runtime.rag_handle.hybrid_search", stub_hs,
    )

    ctx = _ctx(asset={"id": "x"})
    metric = runtime.stage_1_rag_retrieve(object(), ctx)
    assert ctx["rag_chunks"] == []
    assert called["n"] == 0
    assert metric.notes["skipped"] == "no_query"


def test_build_stage_1_user_content_renders_rag_section_when_present():
    from orchestrator_runtime import runtime

    ctx = _ctx(
        documents=[],
        memory_text=None,
        rag_chunks=[
            {
                "chunk_id": "abcd0000", "document_id": "0000abcd",
                "chunk_text": "AXS-05 hit primary endpoint.",
                "contextual_prefix": "Phase 3 GEMINI study.",
                "section_path": ["results"], "score": 0.9,
                "source": "pubmed", "title": "GEMINI Trial",
                "published_at": "2024-06-15",
            },
        ],
    )
    out = runtime._build_stage_1_user_content(ctx)
    assert "## Retrieved context" in out
    assert "GEMINI Trial" in out
    assert "AXS-05 hit primary endpoint." in out


def test_build_stage_1_user_content_omits_rag_section_when_absent():
    from orchestrator_runtime import runtime

    ctx = _ctx(documents=[], memory_text=None)
    out = runtime._build_stage_1_user_content(ctx)
    assert "## Retrieved context" not in out


def test_run_one_invokes_stage_1_rag_retrieve_when_env_enabled(monkeypatch):
    """End-to-end check: when ORCH_ENABLE_STAGE_1_RAG=1 (read at module load),
    `_run_one_inner` calls stage_1_rag_retrieve. We don't run the full
    orchestrator — just verify the call is wired."""
    from orchestrator_runtime import runtime

    # Force the flag on for this test, even though the module-load constant
    # was read before — patch the constant directly so the dispatch sees it.
    monkeypatch.setattr(runtime, "ENABLE_STAGE_1_RAG_DEFAULT", True)

    called = {"n": 0, "k": None}

    def stub_retrieve(_sb, _ctx, *, k=8, asset_scoped=False):
        called["n"] += 1
        called["k"] = k
        _ctx["rag_chunks"] = []
        return runtime.StageMetric(
            stage_name="stage_1_rag_retrieve", model="rag",
            input_tokens=0, output_tokens=0, cost_usd=0.0,
            latency_ms=0, notes={"n_chunks": 0},
        )

    monkeypatch.setattr(runtime, "stage_1_rag_retrieve", stub_retrieve)

    # Stub everything else to avoid the full pipeline.
    def stub_load(_sb, _aid):
        return _ctx()

    def stub_anchor(_sb, _ctx):
        from orchestrator_runtime.runtime import Stage4Anchor
        a = Stage4Anchor(reference_class=None, base_rate=None,
                         similar_cases=[])
        return a, runtime.StageMetric(
            stage_name="stage_4_anchor", model="x",
            input_tokens=0, output_tokens=0, cost_usd=0.0,
            latency_ms=0, notes={},
        )

    monkeypatch.setattr(runtime, "stage_0_load", stub_load)
    monkeypatch.setattr(runtime, "stage_4_anchor", stub_anchor)

    # Halt the pipeline immediately after the RAG call by raising a known
    # signal from the next downstream call we can intercept cleanly.
    sentinel = RuntimeError("__halt_after_rag__")

    def stub_build_shared(_ctx):
        raise sentinel

    monkeypatch.setattr(runtime, "build_shared_system_prefix", stub_build_shared)

    try:
        runtime._run_one_inner(
            sb=object(), a_client=object(), asset_id="asset-1",
            trigger_type="manual", model="x", extractor_model="x",
            ensemble_n=1, ensemble_mode="streaming",
            run_constitutional=False, constitutional_skip_semantic=True,
            enable_premortem=False, dry_run=True,
        )
    except RuntimeError as exc:
        assert str(exc) == "__halt_after_rag__"

    assert called["n"] == 1
    assert called["k"] == runtime.STAGE_1_RAG_K
