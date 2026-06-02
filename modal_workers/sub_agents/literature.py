"""LiteratureRunner — wraps PubMed (+ bioRxiv stub) for the literature sub-agent."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List

from modal_workers.providers.pubmed import eutils as _pubmed
from .runtime import ROLE_REGISTRY, SubAgentRunner, ToolHandler

SKILL_PATH = (
    Path(__file__).resolve().parents[2]
    / "conan-fda-orchestrator-plugin" / "skills" / "sub_agent_literature_reviewer.md"
)


_TOOL_DEFS: List[Dict[str, Any]] = [
    {
        "name": "pubmed_search",
        "description": "Search PubMed for PMIDs by relevance. Returns up to `limit` PMIDs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 25},
            },
            "required": ["query"],
        },
    },
    {
        "name": "pubmed_fetch_abstracts",
        "description": "Bulk-fetch paper records (title, abstract, authors, year, journal, doi, url) for a list of PMIDs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pmids": {"type": "array", "items": {"type": "string"}, "maxItems": 50},
            },
            "required": ["pmids"],
        },
    },
    {
        "name": "pubmed_fetch_full_text",
        "description": "Fetch open-access full text from PubMed Central if available; else returns abstract.",
        "input_schema": {
            "type": "object",
            "properties": {"pmid": {"type": "string"}},
            "required": ["pmid"],
        },
    },
    {
        "name": "pubmed_citation_graph_expand",
        "description": "1-hop neighbors via elink. direction='cited_by' or 'references'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pmid": {"type": "string"},
                "direction": {
                    "type": "string", "enum": ["cited_by", "references"], "default": "cited_by",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
            },
            "required": ["pmid"],
        },
    },
    {
        "name": "biorxiv_search",
        "description": "Search bioRxiv preprints (v1 stub: always empty).",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["query"],
        },
    },
]


class LiteratureRunner(SubAgentRunner):
    role = "literature"
    skill_path = SKILL_PATH
    schema_filename = "literature_review_v1.json"
    tool_defs = _TOOL_DEFS
    # PubMed/bioRxiv role tools + injected internal_rag_* (corpus="literature")
    # over the local already-linked corpus. The skill frontmatter + "Tools
    # actually available" table list all of these by their real names (reconciled
    # 2026-06-02 — the prior "no internal-rag" body claim was wrong; tests in
    # test_sub_agent_rag_tools.py require literature to expose internal_rag).
    internal_rag_default_corpus = "literature"

    def build_handler(self) -> ToolHandler:
        def handle(name: str, inp: Dict[str, Any]) -> Dict[str, Any]:
            if name == "pubmed_search":
                pmids = _pubmed.search(inp["query"], limit=int(inp.get("limit", 25)))
                return {"count": len(pmids), "pmids": pmids}
            if name == "pubmed_fetch_abstracts":
                papers = _pubmed.fetch_abstracts(list(inp.get("pmids") or []))
                return {"count": len(papers), "papers": [asdict(p) for p in papers]}
            if name == "pubmed_fetch_full_text":
                text = _pubmed.fetch_full_text(inp["pmid"])
                return {"pmid": inp["pmid"], "found": text is not None, "text": text or ""}
            if name == "pubmed_citation_graph_expand":
                neighbors = _pubmed.citation_graph_expand(
                    inp["pmid"],
                    direction=inp.get("direction", "cited_by"),
                    limit=int(inp.get("limit", 20)),
                )
                return {"pmid": inp["pmid"], "neighbors": neighbors}
            if name == "biorxiv_search":
                return {"query": inp.get("query", ""), "count": 0, "preprints": []}
            raise ValueError(f"unknown tool: {name}")

        return handle

    def build_degraded_payload(
        self,
        *,
        asset_context: Dict[str, Any],
        question: str,
        tool_log: List[Dict[str, Any]],
        errors: List[str],
    ) -> Dict[str, Any]:
        """Schema-valid partial shape for when PubMed retrieval consumes the
        tool/token budget before the model emits a final review. Returns
        papers=[] + partial_output=true so the orchestrator gets a usable
        (if empty) literature signal instead of a hard SubAgentSchemaError.
        Uses only the top-level keys allowed by literature_review_v1.json."""
        from datetime import datetime, timezone

        queries = [
            (t.get("input") or {}).get("query")
            for t in (tool_log or [])
            if t.get("name") in ("pubmed_search", "biorxiv_search")
            and (t.get("input") or {}).get("query")
        ]
        query_used = (
            "; ".join(queries)
            or (question or "").strip()[:200]
            or "no queries executed"
        )
        asset_id = asset_context.get("asset_id") or asset_context.get("id") or ""
        return {
            "schema_version": 1,
            "asset_id": str(asset_id),
            "papers": [],
            "synthesis": {
                "thesis_alignment": "neutral",
                "summary": (
                    "Partial: literature retrieval did not complete within the "
                    "tool/token budget; no papers were synthesized on this run."
                ),
            },
            "query_used": query_used,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "sourcing_completeness_pct": 0.0,
            "partial_output": True,
        }


ROLE_REGISTRY["literature"] = LiteratureRunner  # type: ignore[assignment]
