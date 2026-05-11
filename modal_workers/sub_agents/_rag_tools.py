"""Shared tool definitions + handler routing for `internal_rag_*` and
`compute_*` cross-cutting tools that any sub-agent can opt into.

Sub-agent runners enable these by setting the class attribute
`internal_rag_default_corpus` (one of literature/filings/labels_aes/news/all)
and/or `compute_tools_enabled = True`. The base SubAgentRunner merges these
into its `tool_defs` at instantiation and chains the handler so role-specific
tools and shared tools both route correctly.

Why a single shared module instead of duplicating the wiring per runner:
each tool's definition + handler routing is identical regardless of which
sub-agent calls it. Per D-114's pattern, the runtime imports the underlying
function directly (no FastMCP overhead); the MCP wrapper exists for Cowork
bulk + operator-triggered use.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


VALID_CORPUS = ("literature", "filings", "labels_aes", "news", "all")


def internal_rag_tool_defs(default_corpus: str = "all") -> List[Dict[str, Any]]:
    """Return the two `internal_rag_*` tool defs.

    `default_corpus` becomes the schema's default for the corpus parameter,
    nudging the model toward the corpus most relevant to the sub-agent's role.
    """
    if default_corpus not in VALID_CORPUS:
        raise ValueError(
            f"default_corpus must be one of {VALID_CORPUS}, got {default_corpus!r}"
        )
    return [
        {
            "name": "internal_rag_hybrid_search",
            "description": (
                "Search the local primary-source corpus (FDA filings, EDGAR, "
                "DailyMed, FAERS, ClinicalTrials.gov, PubMed) via BM25 + dense "
                "+ RRF + rerank. Cheaper than fetching from external APIs and "
                "covers documents already linked to the asset. Use when you "
                "need quick context on prior filings, labels, or papers."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "corpus": {
                        "type": "string",
                        "enum": list(VALID_CORPUS),
                        "default": default_corpus,
                    },
                    "k": {
                        "type": "integer", "minimum": 1, "maximum": 25,
                        "default": 8,
                    },
                    "asset_id": {
                        "type": "string",
                        "description": (
                            "Optional fda_assets.id; restricts retrieval to "
                            "documents linked to this asset."
                        ),
                    },
                    "document_ids": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Explicit document allowlist (overrides asset_id).",
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "internal_rag_get_chunk",
            "description": (
                "Fetch one chunk and (optionally) its preceding/following "
                "siblings within the same document. Use to expand context "
                "around a hybrid_search hit you want to cite."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "chunk_id": {"type": "string"},
                    "with_neighbors": {
                        "type": "integer", "minimum": 0, "maximum": 5,
                        "default": 0,
                    },
                },
                "required": ["chunk_id"],
            },
        },
    ]


def compute_tool_defs() -> List[Dict[str, Any]]:
    """Return the `compute_similar_resolved_cases` tool def. Used by the
    regulatory_history sub-agent to enumerate prior approvals/CRLs in the
    same reference class."""
    return [
        {
            "name": "compute_similar_resolved_cases",
            "description": (
                "Find resolved historical FDA decisions sharing the same "
                "reference-class signature as the current asset (drug class "
                "+ indication + endpoint type). Returns up to `k` cases with "
                "outcome, realized_move_pct, and event date — anchors the "
                "regulatory_history analysis to empirical base rates."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "reference_class_signature": {"type": "string"},
                    "k": {
                        "type": "integer", "minimum": 1, "maximum": 25,
                        "default": 10,
                    },
                },
                "required": ["reference_class_signature"],
            },
        },
    ]


def make_internal_rag_handler(sb=None) -> Callable[[str, Dict[str, Any]], Dict[str, Any]]:
    """Return a closure that routes the two internal_rag tools.

    `sb` is a SupabaseClient. If None, one is created lazily on first use.
    """
    sb_holder: Dict[str, Any] = {"sb": sb}

    def _sb():
        if sb_holder["sb"] is None:
            from modal_workers.shared.supabase_client import SupabaseClient
            sb_holder["sb"] = SupabaseClient()
        return sb_holder["sb"]

    def handle(name: str, inp: Dict[str, Any]) -> Dict[str, Any]:
        from orchestrator_runtime import rag_handle
        if name == "internal_rag_hybrid_search":
            return {
                "results": rag_handle.hybrid_search(
                    _sb(),
                    inp["query"],
                    corpus=inp.get("corpus", "all"),
                    k=int(inp.get("k", 8)),
                    asset_id=inp.get("asset_id"),
                    document_ids=inp.get("document_ids"),
                )
            }
        if name == "internal_rag_get_chunk":
            return rag_handle.get_chunk(
                _sb(), inp["chunk_id"],
                with_neighbors=int(inp.get("with_neighbors", 0)),
            )
        raise KeyError(f"_rag_tools handler does not own tool: {name}")

    return handle


def make_compute_handler() -> Callable[[str, Dict[str, Any]], Dict[str, Any]]:
    """Return a closure routing `compute_similar_resolved_cases` to the
    in-process compute helper (D-114 pattern)."""

    def handle(name: str, inp: Dict[str, Any]) -> Dict[str, Any]:
        if name == "compute_similar_resolved_cases":
            from modal_workers.shared.compute import similar_resolved_cases
            from modal_workers.shared.supabase_client import SupabaseClient
            sb = SupabaseClient()
            cases = similar_resolved_cases(
                sb,
                reference_class=inp["reference_class_signature"],
                k=int(inp.get("k", 10)),
            )
            # SimilarResolvedCase is a dataclass; serialize for tool result.
            from dataclasses import asdict
            return {
                "count": len(cases),
                "cases": [asdict(c) for c in cases],
            }
        raise KeyError(f"_compute handler does not own tool: {name}")

    return handle


def chain_handlers(
    *handlers: Callable[[str, Dict[str, Any]], Dict[str, Any]],
) -> Callable[[str, Dict[str, Any]], Dict[str, Any]]:
    """Try each handler in order; raise the first non-`KeyError` exception
    or return the first non-error result. Used by sub-agent runners to
    compose their role-specific handler with the shared rag/compute ones."""

    def handle(name: str, inp: Dict[str, Any]) -> Dict[str, Any]:
        last_err: Optional[Exception] = None
        for h in handlers:
            try:
                return h(name, inp)
            except KeyError as exc:
                last_err = exc
                continue
            except ValueError as exc:
                # Some role handlers raise ValueError for unknown tools
                # (LiteratureRunner et al). Treat as soft pass-through.
                if "unknown tool" in str(exc).lower():
                    last_err = exc
                    continue
                raise
        raise last_err or ValueError(f"no handler claimed tool: {name}")

    return handle
