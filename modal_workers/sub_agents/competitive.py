"""CompetitiveRunner — uses ClinicalTrials.gov + PubMed to map competitor pipelines."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List

from modal_workers.providers.pubmed import eutils as _pubmed
from modal_workers.ingestion.clinicaltrials_ingest import _ct_get
from .runtime import ROLE_REGISTRY, SubAgentRunner, ToolHandler

SKILL_PATH = (
    Path(__file__).resolve().parents[2]
    / "conan-fda-orchestrator-plugin" / "skills" / "sub_agent_competitive_landscape.md"
)


_TOOL_DEFS: List[Dict[str, Any]] = [
    {
        "name": "clinicaltrials_search",
        "description": "Search ClinicalTrials.gov v2 by free-text term + optional phase/status.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query_term": {"type": "string"},
                "phase": {"type": "string"},
                "status": {"type": "string"},
                "page_size": {"type": "integer", "default": 20, "maximum": 50},
            },
            "required": ["query_term"],
        },
    },
    {
        "name": "clinicaltrials_by_nct",
        "description": "Fetch one or more studies by NCT id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "nct_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 25},
            },
            "required": ["nct_ids"],
        },
    },
    {
        "name": "pubmed_search",
        "description": "Search PubMed for competitor mechanism-of-action papers.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 15, "maximum": 50},
            },
            "required": ["query"],
        },
    },
    {
        "name": "pubmed_fetch_abstracts",
        "description": "Bulk-fetch paper records by PMID list.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pmids": {"type": "array", "items": {"type": "string"}, "maxItems": 50},
            },
            "required": ["pmids"],
        },
    },
]


class CompetitiveRunner(SubAgentRunner):
    role = "competitive"
    skill_path = SKILL_PATH
    schema_filename = "competitive_landscape_v1.json"
    tool_defs = _TOOL_DEFS
    internal_rag_default_corpus = "all"

    def build_handler(self) -> ToolHandler:
        def handle(name: str, inp: Dict[str, Any]) -> Dict[str, Any]:
            if name == "clinicaltrials_search":
                params: Dict[str, Any] = {
                    "query.term": inp["query_term"],
                    "pageSize": min(int(inp.get("page_size", 20)), 50),
                    "format": "json",
                }
                if inp.get("phase"):
                    params["filter.advanced"] = f"AREA[Phase]{inp['phase']}"
                if inp.get("status"):
                    params["filter.overallStatus"] = inp["status"]
                body = _ct_get("/studies", params=params) or {}
                studies = body.get("studies") or []
                return {"count": len(studies), "studies": studies}
            if name == "clinicaltrials_by_nct":
                out: List[Dict[str, Any]] = []
                for nct in (inp.get("nct_ids") or [])[:25]:
                    body = _ct_get(f"/studies/{nct}", params={"format": "json"})
                    if body:
                        out.append(body)
                return {"count": len(out), "studies": out}
            if name == "pubmed_search":
                pmids = _pubmed.search(inp["query"], limit=int(inp.get("limit", 15)))
                return {"count": len(pmids), "pmids": pmids}
            if name == "pubmed_fetch_abstracts":
                papers = _pubmed.fetch_abstracts(list(inp.get("pmids") or []))
                return {"count": len(papers), "papers": [asdict(p) for p in papers]}
            raise ValueError(f"unknown tool: {name}")

        return handle


ROLE_REGISTRY["competitive"] = CompetitiveRunner  # type: ignore[assignment]
