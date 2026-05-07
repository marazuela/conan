"""OptionsMicrostructureRunner — Polygon options chain → straddle / IV / OI."""

from __future__ import annotations

from datetime import date as _date
from pathlib import Path
from typing import Any, Dict, List, Optional

from modal_workers.providers.polygon.options_data import PolygonOptionsData
from .runtime import ROLE_REGISTRY, SubAgentRunner, ToolHandler

SKILL_PATH = (
    Path(__file__).resolve().parents[2]
    / "conan-fda-orchestrator-plugin" / "skills" / "sub_agent_options_microstructure.md"
)


_TOOL_DEFS: List[Dict[str, Any]] = [
    {
        "name": "polygon_get_chain",
        "description": "Pull the options chain for ticker. expiry as ISO date or null for nearest.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "expiry": {"type": "string", "description": "ISO date YYYY-MM-DD"},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "polygon_straddle_implied_move",
        "description": "ATM straddle as % of underlying for the expiry covering event_date.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "event_date": {"type": "string", "description": "ISO date YYYY-MM-DD"},
            },
            "required": ["ticker", "event_date"],
        },
    },
    {
        "name": "polygon_event_window_liquidity",
        "description": "0-5 score reflecting two-sided liquidity in the event-window expiry.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "event_date": {"type": "string"},
            },
            "required": ["ticker", "event_date"],
        },
    },
]


_provider: Optional[PolygonOptionsData] = None


def _p() -> PolygonOptionsData:
    global _provider
    if _provider is None:
        _provider = PolygonOptionsData()
    return _provider


class OptionsMicrostructureRunner(SubAgentRunner):
    role = "options_microstructure"
    skill_path = SKILL_PATH
    schema_filename = "options_microstructure_v1.json"
    tool_defs = _TOOL_DEFS

    def build_handler(self) -> ToolHandler:
        def handle(name: str, inp: Dict[str, Any]) -> Dict[str, Any]:
            if name == "polygon_get_chain":
                expiry = _date.fromisoformat(inp["expiry"]) if inp.get("expiry") else None
                rows = _p().get_chain(inp["ticker"], expiry=expiry)
                return {
                    "ticker": inp["ticker"],
                    "expiry": inp.get("expiry"),
                    "count": len(rows or []),
                    "chain": rows or [],
                }
            if name == "polygon_straddle_implied_move":
                res = _p().get_straddle_implied_move(
                    inp["ticker"], _date.fromisoformat(inp["event_date"])
                )
                return res or {
                    "ticker": inp["ticker"],
                    "event_date": inp["event_date"],
                    "straddle_implied_move_pct": None,
                }
            if name == "polygon_event_window_liquidity":
                res = _p().get_event_window_liquidity(
                    inp["ticker"], _date.fromisoformat(inp["event_date"])
                )
                return res or {
                    "ticker": inp["ticker"],
                    "event_date": inp["event_date"],
                    "score": 0,
                }
            raise ValueError(f"unknown tool: {name}")

        return handle


ROLE_REGISTRY["options_microstructure"] = OptionsMicrostructureRunner  # type: ignore[assignment]
