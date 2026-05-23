"""OptionsMicrostructureRunner — Polygon options chain → straddle / IV / OI.

Degraded mode: if POLYGON_API_KEY is unset (or PolygonClient init raises),
each tool returns {"status": "degraded", "reason": ...} instead of raising.
The runner still emits schema-valid output (low confidence + uncertainty
note) rather than failing the whole sub-agent dispatch loop. Mirrors the
pattern in conan-fda-orchestrator-plugin/mcp_servers/polygon_mcp.py.
"""

from __future__ import annotations

import logging
import os
from datetime import date as _date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .runtime import ROLE_REGISTRY, SubAgentRunner, SubAgentResult, ToolHandler

logger = logging.getLogger(__name__)

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


_provider = None  # PolygonOptionsData | None
_init_error: Optional[str] = None


def _p():
    """Lazy provider getter. Returns None when degraded; populates _init_error."""
    global _provider, _init_error
    if _provider is not None:
        return _provider
    if _init_error is not None:
        return None
    if not os.environ.get("POLYGON_API_KEY"):
        _init_error = "POLYGON_API_KEY env var is unset"
        logger.warning("options_microstructure runner degraded: %s", _init_error)
        return None
    try:
        from modal_workers.providers.polygon.base import PolygonClient
        from modal_workers.providers.polygon.options_data import (
            PolygonOptionsData,
        )
        _provider = PolygonOptionsData(client=PolygonClient())
        return _provider
    except Exception as exc:  # noqa: BLE001
        _init_error = str(exc)
        logger.warning("options_microstructure runner degraded: %s", _init_error)
        return None


def _degraded(extra: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "status": "degraded",
        "reason": _init_error or "POLYGON_API_KEY unavailable",
        **extra,
    }


class OptionsMicrostructureRunner(SubAgentRunner):
    role = "options_microstructure"
    skill_path = SKILL_PATH
    schema_filename = "options_microstructure_v1.json"
    tool_defs = _TOOL_DEFS

    def run(
        self,
        *,
        question: str,
        asset_context: Dict[str, Any],
        budget_token_cap: Optional[int] = None,
    ) -> SubAgentResult:
        """Short-circuit when POLYGON_API_KEY is absent — emit the literal
        degraded shape (schema-valid) without entering the Sonnet loop.

        Saves the per-dispatch Sonnet cost (~$0.30–0.50) AND eliminates the
        degraded-mode runtime drift we saw on the 2026-05-23 VRDN dry-run, where
        Claude — with no Polygon data — invented its own plausible-looking but
        spec-disjoint shape (atm_iv_pct, iv_term_structure, ...). See audit/
        sub_agent_schema_drift_2026-05-23.md §S-3 + §R-3.
        """
        if not os.environ.get("POLYGON_API_KEY"):
            degraded_payload = {
                "schema_version": 1,
                "asset_id": asset_context.get("asset_id", ""),
                "ticker": asset_context.get("ticker", "") or "UNKNOWN",
                "underlying_price": None,
                "event_date": None,
                "straddle_implied_move_pct": None,
                "iv_30d": None,
                "iv_60d": None,
                "iv_term_slope": None,
                "event_window_liquidity_score": 0,
                "position_inferred": "unknown",
                "computed_at": datetime.now(timezone.utc).isoformat(),
                "data_quality": "unavailable",
                "confidence": 0,
                "partial_output": True,
            }
            logger.info(
                "options_microstructure: POLYGON_API_KEY absent — short-circuit "
                "with degraded shape (no Sonnet call)"
            )
            return SubAgentResult(
                role=self.role,
                schema_pass=True,
                schema_retries=0,
                output=degraded_payload,
                tokens_input=0,
                tokens_output=0,
                cost_usd=0.0,
                latency_ms=0,
                tool_call_log=[],
            )
        return super().run(
            question=question,
            asset_context=asset_context,
            budget_token_cap=budget_token_cap,
        )

    def build_handler(self) -> ToolHandler:
        def handle(name: str, inp: Dict[str, Any]) -> Dict[str, Any]:
            if name == "polygon_get_chain":
                p = _p()
                if p is None:
                    return _degraded({
                        "ticker": inp["ticker"],
                        "expiry": inp.get("expiry"),
                        "count": 0,
                        "chain": [],
                    })
                expiry = _date.fromisoformat(inp["expiry"]) if inp.get("expiry") else None
                try:
                    rows = p.get_chain(inp["ticker"], expiry=expiry)
                except Exception as exc:  # noqa: BLE001
                    return _degraded({
                        "ticker": inp["ticker"],
                        "expiry": inp.get("expiry"),
                        "count": 0,
                        "chain": [],
                        "error": str(exc),
                    })
                return {
                    "ticker": inp["ticker"],
                    "expiry": inp.get("expiry"),
                    "count": len(rows or []),
                    "chain": rows or [],
                }
            if name == "polygon_straddle_implied_move":
                p = _p()
                if p is None:
                    return _degraded({
                        "ticker": inp["ticker"],
                        "event_date": inp["event_date"],
                        "straddle_implied_move_pct": None,
                    })
                try:
                    res = p.get_straddle_implied_move(
                        inp["ticker"], _date.fromisoformat(inp["event_date"])
                    )
                except Exception as exc:  # noqa: BLE001
                    return _degraded({
                        "ticker": inp["ticker"],
                        "event_date": inp["event_date"],
                        "straddle_implied_move_pct": None,
                        "error": str(exc),
                    })
                return res or {
                    "ticker": inp["ticker"],
                    "event_date": inp["event_date"],
                    "straddle_implied_move_pct": None,
                }
            if name == "polygon_event_window_liquidity":
                p = _p()
                if p is None:
                    return _degraded({
                        "ticker": inp["ticker"],
                        "event_date": inp["event_date"],
                        "score": 0,
                    })
                try:
                    res = p.get_event_window_liquidity(
                        inp["ticker"], _date.fromisoformat(inp["event_date"])
                    )
                except Exception as exc:  # noqa: BLE001
                    return _degraded({
                        "ticker": inp["ticker"],
                        "event_date": inp["event_date"],
                        "score": 0,
                        "error": str(exc),
                    })
                return res or {
                    "ticker": inp["ticker"],
                    "event_date": inp["event_date"],
                    "score": 0,
                }
            raise ValueError(f"unknown tool: {name}")

        return handle


ROLE_REGISTRY["options_microstructure"] = OptionsMicrostructureRunner  # type: ignore[assignment]
