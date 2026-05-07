"""RegulatoryHistoryRunner — uses openFDA + the catalyst_universe table for regulatory history."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from modal_workers.ingestion.openfda_ingest import _openfda_get
from modal_workers.shared.supabase_client import SupabaseClient
from .runtime import ROLE_REGISTRY, SubAgentRunner, ToolHandler

SKILL_PATH = (
    Path(__file__).resolve().parents[2]
    / "conan-fda-orchestrator-plugin" / "skills" / "sub_agent_regulatory_history.md"
)


_TOOL_DEFS: List[Dict[str, Any]] = [
    {
        "name": "openfda_drugsfda_approvals",
        "description": "Query openFDA drug/drugsfda for NDA/BLA application records by sponsor/application/date.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sponsor_search": {"type": "string"},
                "application_search": {"type": "string"},
                "since": {"type": "string", "description": "ISO date YYYY-MM-DD"},
                "until": {"type": "string", "description": "ISO date YYYY-MM-DD"},
                "limit": {"type": "integer", "default": 25, "maximum": 100},
            },
        },
    },
    {
        "name": "openfda_labels_recent",
        "description": "Query openFDA drug/label for recent label records of a drug.",
        "input_schema": {
            "type": "object",
            "properties": {
                "drug_name": {"type": "string"},
                "application_number": {"type": "string"},
                "limit": {"type": "integer", "default": 10, "maximum": 25},
            },
        },
    },
    {
        "name": "openfda_adverse_events",
        "description": "Query openFDA drug/event for adverse event reports involving a drug.",
        "input_schema": {
            "type": "object",
            "properties": {
                "drug_name": {"type": "string"},
                "limit": {"type": "integer", "default": 25, "maximum": 100},
            },
            "required": ["drug_name"],
        },
    },
    {
        "name": "fda_adcomm_upcoming",
        "description": "List upcoming AdComm/PDUFA events from catalyst_universe (forward-looking).",
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string"},
                "end_date": {"type": "string"},
                "catalyst_type": {"type": "string", "enum": ["adcomm", "pdufa"]},
                "limit": {"type": "integer", "default": 50},
            },
        },
    },
    {
        "name": "fda_adcomm_historical",
        "description": "Resolved AdComm/PDUFA events (catalyst_date < today) filtered by drug/sponsor/indication.",
        "input_schema": {
            "type": "object",
            "properties": {
                "drug_name": {"type": "string"},
                "sponsor_search": {"type": "string"},
                "indication": {"type": "string"},
                "catalyst_type": {"type": "string", "enum": ["adcomm", "pdufa"]},
                "limit": {"type": "integer", "default": 50},
            },
        },
    },
]


_sb: Optional[SupabaseClient] = None


def _client() -> SupabaseClient:
    global _sb
    if _sb is None:
        _sb = SupabaseClient()
    return _sb


class RegulatoryHistoryRunner(SubAgentRunner):
    role = "regulatory_history"
    skill_path = SKILL_PATH
    schema_filename = "regulatory_history_v1.json"
    tool_defs = _TOOL_DEFS

    def build_handler(self) -> ToolHandler:
        def handle(name: str, inp: Dict[str, Any]) -> Dict[str, Any]:
            if name == "openfda_drugsfda_approvals":
                today = date.today()
                since_d = (
                    date.fromisoformat(inp["since"])
                    if inp.get("since") else (today - timedelta(days=365 * 2))
                )
                until_d = date.fromisoformat(inp["until"]) if inp.get("until") else today
                clauses: List[str] = [
                    f"submissions.submission_status_date:[{since_d.isoformat()} TO {until_d.isoformat()}]",
                ]
                if inp.get("sponsor_search"):
                    clauses.append(f'sponsor_name:"{inp["sponsor_search"]}"')
                if inp.get("application_search"):
                    clauses.append(f'application_number:"{inp["application_search"]}"')
                body = _openfda_get(
                    "/drug/drugsfda.json",
                    params={
                        "search": " AND ".join(clauses),
                        "limit": min(int(inp.get("limit", 25)), 100),
                        "skip": 0,
                    },
                ) or {}
                results = body.get("results") or []
                return {"count": len(results), "applications": results}

            if name == "openfda_labels_recent":
                clauses: List[str] = []
                if inp.get("drug_name"):
                    name_q = inp["drug_name"]
                    clauses.append(
                        f'openfda.brand_name:"{name_q}" openfda.generic_name:"{name_q}"'
                    )
                if inp.get("application_number"):
                    clauses.append(f'openfda.application_number:"{inp["application_number"]}"')
                if not clauses:
                    return {"count": 0, "labels": [], "error": "must supply drug_name or application_number"}
                body = _openfda_get(
                    "/drug/label.json",
                    params={
                        "search": " ".join(clauses),
                        "limit": min(int(inp.get("limit", 10)), 25),
                    },
                ) or {}
                results = body.get("results") or []
                return {"count": len(results), "labels": results}

            if name == "openfda_adverse_events":
                body = _openfda_get(
                    "/drug/event.json",
                    params={
                        "search": f'patient.drug.medicinalproduct:"{inp["drug_name"]}"',
                        "limit": min(int(inp.get("limit", 25)), 100),
                    },
                ) or {}
                results = body.get("results") or []
                return {"count": len(results), "events": results}

            if name == "fda_adcomm_upcoming":
                today = date.today()
                s = date.fromisoformat(inp["start_date"]) if inp.get("start_date") else today
                e = date.fromisoformat(inp["end_date"]) if inp.get("end_date") else today + timedelta(days=180)
                params: Dict[str, str] = {
                    "select": "id,profile,catalyst_type,catalyst_date,ticker,sponsor_name,raw_payload",
                    "catalyst_date": f"gte.{s.isoformat()}",
                    "and": f"(catalyst_date.lte.{e.isoformat()})",
                    "order": "catalyst_date.asc",
                    "limit": str(min(int(inp.get("limit", 50)), 200)),
                }
                if inp.get("catalyst_type"):
                    params["catalyst_type"] = f"eq.{inp['catalyst_type']}"
                rows = _client()._rest("GET", "catalyst_universe", params=params) or []
                return {"count": len(rows), "events": rows}

            if name == "fda_adcomm_historical":
                today = date.today().isoformat()
                params: Dict[str, str] = {
                    "select": "id,profile,catalyst_type,catalyst_date,ticker,sponsor_name,raw_payload,material_outcome",
                    "catalyst_date": f"lt.{today}",
                    "order": "catalyst_date.desc",
                    "limit": str(min(int(inp.get("limit", 50)), 200)),
                }
                if inp.get("catalyst_type"):
                    params["catalyst_type"] = f"eq.{inp['catalyst_type']}"
                if inp.get("sponsor_search"):
                    params["sponsor_name"] = f"ilike.*{inp['sponsor_search']}*"
                rows = _client()._rest("GET", "catalyst_universe", params=params) or []

                drug = (inp.get("drug_name") or "").lower()
                indi = (inp.get("indication") or "").lower()
                if drug or indi:
                    filtered: List[Dict[str, Any]] = []
                    for r in rows:
                        payload = r.get("raw_payload") or {}
                        blob = " ".join(
                            str(v) for v in payload.values()
                            if isinstance(v, (str, int, float))
                        ).lower()
                        if drug and drug not in blob:
                            continue
                        if indi and indi not in blob:
                            continue
                        filtered.append(r)
                    rows = filtered
                return {"count": len(rows), "events": rows}

            raise ValueError(f"unknown tool: {name}")

        return handle


ROLE_REGISTRY["regulatory_history"] = RegulatoryHistoryRunner  # type: ignore[assignment]
