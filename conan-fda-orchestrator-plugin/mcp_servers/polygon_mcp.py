"""polygon_mcp — FastMCP server wrapping Polygon options chain.

Tools mirror modal_workers/providers/polygon/options_data.PolygonOptionsData.
Used by the options_microstructure sub-agent.

Degraded mode: if POLYGON_API_KEY is unset (or the provider raises any error
during init), every tool returns {"status": "degraded", "reason": "<msg>",
...} so callers degrade gracefully — the microstructure sub-agent still
emits structured output with low confidence and a documented uncertainty
instead of crashing the whole dispatch loop.
"""

from __future__ import annotations

import logging
import os
from datetime import date as _date
from typing import Any, Dict, List, Optional

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "polygon_mcp requires the `mcp` package. Install with `pip install 'mcp[cli]'`."
    ) from exc

logger = logging.getLogger(__name__)

_provider = None  # PolygonOptionsData | None
_init_error: Optional[str] = None


def _p():
    """Lazy provider getter. Returns None if degraded; populates _init_error."""
    global _provider, _init_error
    if _provider is not None:
        return _provider
    if _init_error is not None:
        return None
    if not os.environ.get("POLYGON_API_KEY"):
        _init_error = "POLYGON_API_KEY env var is unset"
        logger.warning("polygon_mcp degraded: %s", _init_error)
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
        logger.warning("polygon_mcp degraded: %s", _init_error)
        return None


def _degraded(extra: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "status": "degraded",
        "reason": _init_error or "POLYGON_API_KEY unavailable",
        **extra,
    }


mcp = FastMCP(
    name="conan-polygon",
    instructions=(
        "Polygon.io options chain wrapper. Returns straddle-implied move, IV "
        "term structure, and event-window liquidity score for the "
        "options_microstructure sub-agent. Requires POLYGON_API_KEY — when "
        "unavailable, every tool returns {status: 'degraded', reason: ...} "
        "so callers can produce structured_output with explicit low-confidence "
        "uncertainties instead of failing."
    ),
)


def _parse(d: Optional[str]) -> Optional[_date]:
    return _date.fromisoformat(d) if d else None


@mcp.tool()
def get_chain(ticker: str, expiry: Optional[str] = None) -> Dict[str, Any]:
    """Pull the options chain for ticker. expiry as ISO date or None for nearest."""
    p = _p()
    if p is None:
        return _degraded({"ticker": ticker, "expiry": expiry, "count": 0, "chain": []})
    try:
        rows = p.get_chain(ticker, expiry=_parse(expiry))
    except Exception as exc:  # noqa: BLE001
        return _degraded({
            "ticker": ticker, "expiry": expiry, "count": 0, "chain": [],
            "error": str(exc),
        })
    return {"ticker": ticker, "expiry": expiry, "count": len(rows or []), "chain": rows or []}


@mcp.tool()
def get_iv(ticker: str, strike: float, expiry: str) -> Dict[str, Any]:
    """Implied volatility for a specific strike/expiry."""
    p = _p()
    if p is None:
        return _degraded({"ticker": ticker, "strike": strike, "expiry": expiry, "iv": None})
    try:
        iv = p.get_iv(ticker, strike, _date.fromisoformat(expiry))
    except Exception as exc:  # noqa: BLE001
        return _degraded({
            "ticker": ticker, "strike": strike, "expiry": expiry, "iv": None,
            "error": str(exc),
        })
    return {"ticker": ticker, "strike": strike, "expiry": expiry, "iv": iv}


@mcp.tool()
def straddle_implied_move(ticker: str, event_date: str) -> Dict[str, Any]:
    """ATM straddle as a % of underlying for the expiry covering event_date."""
    p = _p()
    if p is None:
        return _degraded({
            "ticker": ticker, "event_date": event_date,
            "straddle_implied_move_pct": None,
        })
    try:
        res = p.get_straddle_implied_move(ticker, _date.fromisoformat(event_date))
    except Exception as exc:  # noqa: BLE001
        return _degraded({
            "ticker": ticker, "event_date": event_date,
            "straddle_implied_move_pct": None, "error": str(exc),
        })
    return res or {"ticker": ticker, "event_date": event_date, "straddle_implied_move_pct": None}


@mcp.tool()
def event_window_liquidity(ticker: str, event_date: str) -> Dict[str, Any]:
    """0–5 score reflecting two-sided liquidity in the event-window expiry."""
    p = _p()
    if p is None:
        return _degraded({"ticker": ticker, "event_date": event_date, "score": 0})
    try:
        res = p.get_event_window_liquidity(ticker, _date.fromisoformat(event_date))
    except Exception as exc:  # noqa: BLE001
        return _degraded({
            "ticker": ticker, "event_date": event_date, "score": 0,
            "error": str(exc),
        })
    return res or {"ticker": ticker, "event_date": event_date, "score": 0}


def main() -> None:  # pragma: no cover
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    main()
