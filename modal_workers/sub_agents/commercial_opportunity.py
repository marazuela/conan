"""CommercialOpportunityRunner — TAM, SoC, unmet need, regulatory incentives.

Phase 2b of the v4 architecture simplification. Fills the commercial-dimensions
gap that v3 sub-agents (literature/competitive/regulatory_history/options_microstructure)
left uncovered. Dispatched from v4 Stage 1 alongside the existing four roles.

Tools (MVP scope):
  openfda_labels_for_indication — find drug labels mentioning an indication
  openfda_label_by_drug          — full label sections (adverse_reactions, indications)
  pubmed_search                  — epidemiology / prevalence / mortality literature
  pubmed_fetch_abstracts         — pull abstracts for selected PMIDs

Deliberately omitted from v0 (operator can extend later):
  - polygon get_market_cap → mcap_to_peak_revenue_ratio is best inferred for now
  - internal_rag designation cross-check → relies on extracted_facts coverage,
    not yet wide enough across the active asset set

Schema: commercial_opportunity_v1.json (conan-cowork-skills/schemas/).
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List

from modal_workers.ingestion.openfda_ingest import _openfda_get
from modal_workers.providers.pubmed import eutils as _pubmed
from .runtime import ROLE_REGISTRY, SubAgentRunner, ToolHandler

SKILL_PATH = (
    Path(__file__).resolve().parents[2]
    / "conan-fda-orchestrator-plugin" / "skills" / "sub_agent_commercial_opportunity.md"
)


_TOOL_DEFS: List[Dict[str, Any]] = [
    {
        "name": "openfda_labels_for_indication",
        "description": (
            "Search openFDA drug labels for products that treat a given indication. "
            "Use to enumerate standard-of-care drugs. Returns label results with "
            "openfda.brand_name / generic_name + indications_and_usage section."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "indication": {
                    "type": "string",
                    "description": "Indication string (free text). Matched against indications_and_usage."
                },
                "limit": {
                    "type": "integer",
                    "default": 10,
                    "maximum": 25,
                    "description": "Max label records to return.",
                },
            },
            "required": ["indication"],
        },
    },
    {
        "name": "openfda_label_by_drug",
        "description": (
            "Fetch the latest openFDA label for a specific drug by brand or generic "
            "name. Use to pull the adverse_reactions section after enumerating SoC "
            "drugs via openfda_labels_for_indication."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "drug_name": {"type": "string"},
                "limit": {"type": "integer", "default": 3, "maximum": 10},
            },
            "required": ["drug_name"],
        },
    },
    {
        "name": "pubmed_search",
        "description": (
            "Search PubMed. Use to find epidemiology / prevalence / mortality / "
            "burden-of-disease papers backing unmet-need severity."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 10, "maximum": 25},
            },
            "required": ["query"],
        },
    },
    {
        "name": "pubmed_fetch_abstracts",
        "description": "Fetch PubMed abstracts by PMID list.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pmids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 25,
                },
            },
            "required": ["pmids"],
        },
    },
]


class CommercialOpportunityRunner(SubAgentRunner):
    role = "commercial_opportunity"
    skill_path = SKILL_PATH
    schema_filename = "commercial_opportunity_v1.json"
    tool_defs = _TOOL_DEFS
    internal_rag_default_corpus = "all"

    def build_handler(self) -> ToolHandler:
        def handle(name: str, inp: Dict[str, Any]) -> Dict[str, Any]:
            if name == "openfda_labels_for_indication":
                indication = inp["indication"].strip()
                # openFDA search syntax: phrase match inside indications_and_usage.
                # Surround with quotes for multi-word indication strings (e.g.
                # "type 2 diabetes" vs token-split "type 2 diabetes").
                body = _openfda_get(
                    "/drug/label.json",
                    params={
                        "search": f'indications_and_usage:"{indication}"',
                        "limit": min(int(inp.get("limit", 10)), 25),
                    },
                ) or {}
                results = body.get("results") or []
                return {"count": len(results), "labels": results}

            if name == "openfda_label_by_drug":
                drug_q = inp["drug_name"].strip()
                # Try brand_name OR generic_name. openFDA boolean OR is implicit
                # with two clauses on the same field group.
                body = _openfda_get(
                    "/drug/label.json",
                    params={
                        "search": (
                            f'openfda.brand_name:"{drug_q}" '
                            f'openfda.generic_name:"{drug_q}"'
                        ),
                        "limit": min(int(inp.get("limit", 3)), 10),
                    },
                ) or {}
                results = body.get("results") or []
                return {"count": len(results), "labels": results}

            if name == "pubmed_search":
                pmids = _pubmed.search(
                    inp["query"], limit=int(inp.get("limit", 10)),
                )
                return {"count": len(pmids), "pmids": pmids}

            if name == "pubmed_fetch_abstracts":
                papers = _pubmed.fetch_abstracts(list(inp.get("pmids") or []))
                return {"count": len(papers), "papers": [asdict(p) for p in papers]}

            raise ValueError(f"unknown tool: {name}")

        return handle


ROLE_REGISTRY["commercial_opportunity"] = CommercialOpportunityRunner  # type: ignore[assignment]
