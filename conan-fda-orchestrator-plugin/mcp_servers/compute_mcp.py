"""compute_mcp — FastMCP server exposing Stage 4 / Stage 8 helpers.

Plan §"compute-mcp" (plugin README, Phase 4.7) lists five tools:
  - base_rate              : empirical FDA approval rate for a reference class
  - similar_resolved_cases : top-k resolved cases sharing the class
  - isotonic_calibrate     : apply the active isotonic curve to a raw conviction
  - brier                  : Brier score for predictions vs binary outcomes
  - verify_claim           : RAG-backed claim check (NotImplemented until
                             internal_rag_mcp ships)

The orchestrator runtime imports the underlying functions directly from
`modal_workers.shared.compute` for in-process Stage 4 (no MCP overhead).
This server is for Cowork bulk + operator-triggered tool use, where the
caller is a Claude session driving the runtime over MCP.

Run:
  pip install "mcp[cli]"   # provides mcp.server.fastmcp
  python -m conan_fda_orchestrator_plugin.mcp_servers.compute_mcp

Or register via .mcp.json (plugin manifest) at orchestrator load time.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

# fastmcp is the runtime dependency for Phase 4.7. Importing at module
# level so a missing dependency surfaces immediately when the plugin is
# loaded rather than on first tool call.
try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "compute_mcp requires the `mcp` package with FastMCP support. "
        "Install with `pip install 'mcp[cli]'` (Phase 4.7 dependency)."
    ) from exc

from modal_workers.shared.compute import (
    apply_isotonic_calibration,
    brier_score,
    compute_base_rate,
    get_active_calibration_curve,
    similar_resolved_cases,
)
from modal_workers.shared.compute import verify_claim as _verify_claim_stub
from modal_workers.shared.supabase_client import SupabaseClient


_sb: Optional[SupabaseClient] = None


def _client() -> SupabaseClient:
    """Lazy SupabaseClient — env vars are read by SupabaseClient itself."""
    global _sb
    if _sb is None:
        _sb = SupabaseClient()
    return _sb


mcp = FastMCP(
    name="conan-compute",
    instructions=(
        "Compute helpers for Conan v3 orchestrator: reference-class base "
        "rates (Stage 4), isotonic calibration (Stage 8), Brier scoring "
        "for post-mortems."
    ),
)


@mcp.tool()
def base_rate(reference_class: str) -> Dict[str, Any]:
    """Look up the empirical FDA approval rate for a reference class.

    Args:
        reference_class: signature string, e.g. 'phase3_oncology_breakthrough_no_prior_crl'.

    Returns a dict with the rate (0–1), n_cases, optional CI, and median
    realized move percent. Returns {"found": false} when the class hasn't
    been refit yet.
    """
    br = compute_base_rate(_client(), reference_class)
    if br is None:
        return {"found": False, "reference_class": reference_class}
    return {
        "found": True,
        "reference_class": br.reference_class,
        "n_cases": br.n_cases,
        "approval_rate": br.approval_rate,
        "approval_rate_ci_low": br.approval_rate_ci_low,
        "approval_rate_ci_high": br.approval_rate_ci_high,
        "median_realized_move_pct": br.median_realized_move_pct,
        "refit_at": br.refit_at,
    }


@mcp.tool()
def similar_cases(
    reference_class: str,
    k: int = 5,
    exclude_asset_id: Optional[str] = None,
    holdout_only: bool = False,
) -> List[Dict[str, Any]]:
    """Top-k resolved cases sharing the reference_class signature.

    Args:
        reference_class: signature string.
        k: max cases (default 5).
        exclude_asset_id: omit this asset (use during backtest replay).
        holdout_only: if true, restrict to is_holdout=true rows.
    """
    cases = similar_resolved_cases(
        _client(),
        reference_class,
        k=k,
        exclude_asset_id=exclude_asset_id,
        holdout_only=holdout_only,
    )
    return [
        {
            "eval_harness_id": c.eval_harness_id,
            "asset_id": c.asset_id,
            "ticker": c.asset_ticker,
            "drug_name": c.asset_drug_name,
            "reference_assessment_date": c.reference_assessment_date,
            "realized_outcome": c.realized_outcome,
            "realized_move_pct": c.realized_move_pct,
            "notes": c.notes,
        }
        for c in cases
    ]


@mcp.tool()
def isotonic_calibrate(
    raw_conviction_pct: float,
    curve_version: Optional[str] = None,
) -> Dict[str, Any]:
    """Apply isotonic calibration to a raw conviction (0–100).

    Args:
        raw_conviction_pct: pre-calibration conviction in percent.
        curve_version: explicit curve to apply; defaults to the active curve.

    Returns calibrated conviction (0–100) and the curve version used. With
    no fitted curve, returns the input unchanged (`curve_version: null`).
    """
    sb = _client()
    if curve_version:
        rows = sb._rest(
            "GET", "calibration_curves",
            params={"select": "version,curve_data",
                    "version": f"eq.{curve_version}", "limit": "1"},
        ) or []
        curve = ({"version": rows[0]["version"], "curve_data": rows[0]["curve_data"]}
                 if rows else None)
    else:
        curve = get_active_calibration_curve(sb)

    raw01 = max(0.0, min(1.0, float(raw_conviction_pct) / 100.0))
    if curve and curve.get("curve_data"):
        cal01 = apply_isotonic_calibration(raw01, curve["curve_data"])
        return {
            "raw_conviction_pct": float(raw_conviction_pct),
            "calibrated_conviction_pct": round(cal01 * 100.0, 2),
            "curve_version": curve.get("version"),
        }
    return {
        "raw_conviction_pct": float(raw_conviction_pct),
        "calibrated_conviction_pct": float(raw_conviction_pct),
        "curve_version": None,
    }


@mcp.tool()
def brier(predictions: List[float], outcomes: List[int]) -> Dict[str, Any]:
    """Brier score (mean squared error) for a batch of predictions vs binary
    outcomes. Lower is better; perfect=0, always-0.5 on balanced=0.25.
    """
    score = brier_score(predictions, outcomes)
    return {
        "brier_score": score,
        "n": len(predictions),
    }


@mcp.tool()
def verify_claim(claim: str, evidence_corpus_id: Optional[str] = None) -> Dict[str, Any]:
    """RAG-backed claim verification. Phase 4.7 — wired through internal_rag_mcp
    + a Sonnet judge. Currently returns inconclusive so callers degrade safely.
    """
    try:
        _verify_claim_stub(claim, evidence_corpus_id)
    except NotImplementedError as exc:
        return {
            "status": "inconclusive",
            "reason": str(exc),
            "claim": claim,
        }
    # Unreachable until Phase 4.7
    return {"status": "inconclusive", "claim": claim}  # pragma: no cover


def main() -> None:  # pragma: no cover
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    main()
