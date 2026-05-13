"""Shared cost-budget helpers for Stream 6 step 4.

  - PER_RUN_HARD_KILL_USD = 15.0  (used by orchestrator_drain_queue)
  - ASSET_24H_SOFT_USD    = 20.0  (per-asset soft alert)
  - GLOBAL_24H_SOFT_USD   = 500.0 (global soft alert)

The hard kill is enforced inside OrchestratorClient.attach_budget(); this
module owns the *soft* 24h rollup that fires operator_flags after a run
completes. Soft alerts are reactive — they fire on the *next* run after a
breach, which is acceptable because the partial unique index on
operator_flags collapses parallel inserts at the same (source, kind, asset)
into one open flag.

The operator_flags table CHECK on `source` is extended to include
'orchestrator_cost' in migration 20260510000010_v3_stream6_safety_and_cleanup.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

PER_RUN_HARD_KILL_USD = 15.0
ASSET_24H_SOFT_USD = 20.0
GLOBAL_24H_SOFT_USD = 500.0
# Per-component HARD halt for asset_linker. Soft alerts (above) only emit
# operator_flag rows; this ceiling causes the linker run loop to return
# early and the */15 cron will idle until the 24h window rolls forward.
# Raised 2026-05-12 from $10 to $20 after PR #34 moved pass-1 to Haiku 4.5.
# Sonnet pass-1 burned $40+ in 8h on 2026-05-11; Haiku is ~3× cheaper, so
# $20/24h is the expected steady-state ceiling with headroom for bursts.
ASSET_LINKER_24H_HARD_USD = 20.0
# Per-component HARD halt for the orchestrator drain. Sized as 6× the
# observed healthy daily rate (~$8/day for ~22 runs at ~$0.36/run avg) to
# allow legit expansion but cap a runaway scenario (e.g. ensemble_n stuck
# at 7 or extractor stage in an infinite loop) at $50 instead of letting
# 12 drain ticks × 5 runs × $2 = $120/hour leak unbounded.
ORCHESTRATOR_24H_HARD_USD = 50.0

OPERATOR_FLAG_SOURCE = "orchestrator_cost"
ASSET_FLAG_KIND = "asset_24h_budget_breached"
GLOBAL_FLAG_KIND = "global_24h_budget_breached"
ASSET_LINKER_FLAG_KIND = "asset_linker_24h_hard_halt"
ORCHESTRATOR_FLAG_KIND = "orchestrator_24h_hard_halt"


def asset_24h_cost_usd(sb, asset_id: str) -> float:
    """SUM(cost_usd) over convergence_assessments rows for asset_id whose
    parent orchestrator_runs row completed in the last 24h. Reads from
    convergence_assessments because cost_usd is already populated there."""
    rpc_payload = {"p_asset_id": asset_id}
    try:
        rows = sb._rest(
            "POST", "rpc/orchestrator_24h_asset_cost",
            json_body=rpc_payload,
        ) or []
        if rows and isinstance(rows[0], dict):
            return float(rows[0].get("total_cost_usd") or 0.0)
        if rows and isinstance(rows[0], (int, float)):
            return float(rows[0])
    except Exception:  # noqa: BLE001
        # RPC may not exist yet on prod; fall back to direct read.
        pass
    rows = sb._rest(
        "GET", "convergence_assessments",
        params={
            "asset_id": f"eq.{asset_id}",
            "select": "cost_usd,created_at",
            "order": "created_at.desc",
            "limit": "200",
        },
    ) or []
    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    total = 0.0
    for r in rows:
        try:
            ts = datetime.fromisoformat(
                (r.get("created_at") or "").replace("Z", "+00:00"))
            if ts < cutoff:
                break
            total += float(r.get("cost_usd") or 0.0)
        except Exception:  # noqa: BLE001
            continue
    return total


def global_24h_cost_usd(sb) -> float:
    """SUM(cost_usd) over the last 24h across both orchestrator and
    asset_linker spend. Bounded by select limit; for accurate accounting
    beyond ~5000 rows/day add a SQL RPC."""
    rpc_payload: Dict[str, Any] = {}
    try:
        rows = sb._rest(
            "POST", "rpc/orchestrator_24h_global_cost",
            json_body=rpc_payload,
        ) or []
        if rows and isinstance(rows[0], dict):
            return float(rows[0].get("total_cost_usd") or 0.0)
    except Exception:  # noqa: BLE001
        pass
    from datetime import datetime, timedelta, timezone
    cutoff_iso = (datetime.now(timezone.utc)
                  - timedelta(hours=24)).isoformat()
    orch_rows = sb._rest(
        "GET", "convergence_assessments",
        params={
            "created_at": f"gte.{cutoff_iso}",
            "select": "cost_usd",
            "limit": "5000",
        },
    ) or []
    orch_total = sum(float(r.get("cost_usd") or 0.0) for r in orch_rows)

    # asset_linker spend is logged separately in asset_linker_runs.
    # Missing table (pre-migration deploys) → treat as zero.
    try:
        linker_rows = sb._rest(
            "GET", "asset_linker_runs",
            params={
                "started_at": f"gte.{cutoff_iso}",
                "select": "cost_usd",
                "limit": "5000",
            },
        ) or []
        linker_total = sum(float(r.get("cost_usd") or 0.0) for r in linker_rows)
    except Exception:  # noqa: BLE001
        linker_total = 0.0
    return orch_total + linker_total


def asset_linker_24h_cost_usd(sb) -> float:
    """SUM(cost_usd) over asset_linker_runs rows started in the last 24h.
    Pure-table read; no RPC. Missing-table → zero (pre-migration safety)."""
    from datetime import datetime, timedelta, timezone
    cutoff_iso = (datetime.now(timezone.utc)
                  - timedelta(hours=24)).isoformat()
    try:
        rows = sb._rest(
            "GET", "asset_linker_runs",
            params={
                "started_at": f"gte.{cutoff_iso}",
                "select": "cost_usd",
                "limit": "5000",
            },
        ) or []
    except Exception:  # noqa: BLE001
        return 0.0
    return sum(float(r.get("cost_usd") or 0.0) for r in rows)


def orchestrator_24h_cost_usd(sb) -> float:
    """SUM(cost_usd) over convergence_assessments rows created in the last
    24h. Pure-table read; no RPC. Missing-table → zero (pre-migration
    safety, mirrors asset_linker_24h_cost_usd)."""
    from datetime import datetime, timedelta, timezone
    cutoff_iso = (datetime.now(timezone.utc)
                  - timedelta(hours=24)).isoformat()
    try:
        rows = sb._rest(
            "GET", "convergence_assessments",
            params={
                "created_at": f"gte.{cutoff_iso}",
                "select": "cost_usd",
                "limit": "5000",
            },
        ) or []
    except Exception:  # noqa: BLE001
        return 0.0
    return sum(float(r.get("cost_usd") or 0.0) for r in rows)


def check_orchestrator_hard_halt(sb) -> Dict[str, Any]:
    """Return {"halt": bool, "total_24h_usd": float}. Caller (the
    orchestrator drain loop) MUST check this BEFORE pulling pending
    orchestrator_runs rows and bail out if halt=True. On halt, also opens
    an operator_flag so the breach is visible in the dashboard. Pending
    rows stay pending — they'll be picked up by the next drain tick once
    the rolling 24h drops back below the ceiling."""
    total = orchestrator_24h_cost_usd(sb)
    halt = total >= ORCHESTRATOR_24H_HARD_USD
    if halt:
        upsert_cost_flag(
            sb,
            severity="critical",
            kind=ORCHESTRATOR_FLAG_KIND,
            title=(
                f"orchestrator 24h spend ${total:.2f} ≥ hard halt "
                f"${ORCHESTRATOR_24H_HARD_USD:.0f}"
            ),
            body=(
                f"convergence_assessments cost in the last 24h is "
                f"${total:.2f}, ≥ the ${ORCHESTRATOR_24H_HARD_USD:.0f} "
                "HARD halt. Drain ticks return early without dispatching "
                "pending runs until the rolling 24h falls back below the "
                "ceiling. Pending runs stay queued. Investigate the spike "
                "(stuck ensemble_n, extractor infinite loop, asset thrash) "
                "before raising the ceiling."
            ),
            evidence={
                "orchestrator_24h_usd": round(total, 4),
                "hard_halt_usd": ORCHESTRATOR_24H_HARD_USD,
            },
        )
    return {"halt": halt, "total_24h_usd": round(total, 4)}


def check_asset_linker_hard_halt(sb) -> Dict[str, Any]:
    """Return {"halt": bool, "total_24h_usd": float}. Caller (asset_linker
    pass-1 main) MUST check this before fetching docs and bail out if
    halt=True. On halt, also opens an operator_flag so the breach is visible
    in the dashboard."""
    total = asset_linker_24h_cost_usd(sb)
    halt = total >= ASSET_LINKER_24H_HARD_USD
    if halt:
        upsert_cost_flag(
            sb,
            severity="critical",
            kind=ASSET_LINKER_FLAG_KIND,
            title=(
                f"asset_linker 24h spend ${total:.2f} ≥ hard halt "
                f"${ASSET_LINKER_24H_HARD_USD:.0f}"
            ),
            body=(
                f"asset_linker_runs cost in the last 24h is ${total:.2f}, "
                f"≥ the ${ASSET_LINKER_24H_HARD_USD:.0f} HARD halt. New "
                "pass-1 runs return early without enqueuing docs until the "
                "rolling 24h falls back below the ceiling. Investigate the "
                "spike before raising the ceiling."
            ),
            evidence={
                "asset_linker_24h_usd": round(total, 4),
                "hard_halt_usd": ASSET_LINKER_24H_HARD_USD,
            },
        )
    return {"halt": halt, "total_24h_usd": round(total, 4)}


def upsert_cost_flag(
    sb, severity: str, kind: str, title: str, body: str,
    asset_id: Optional[str] = None,
    evidence: Optional[Dict[str, Any]] = None,
) -> None:
    """INSERT operator_flags with source='orchestrator_cost'. The partial
    unique index `WHERE resolved_at IS NULL` collapses repeat inserts at the
    same (source, kind, asset) into one open flag."""
    payload: Dict[str, Any] = {
        "source": OPERATOR_FLAG_SOURCE,
        "kind": kind,
        "severity": severity,
        "title": title,
        "body": body,
        "evidence": evidence or {},
    }
    if asset_id:
        # operator_flags.entity_id is uuid; we don't have entity_id directly,
        # but the asset_id maps to fda_assets which has entity_id. The flag
        # is keyed by the partial unique index on entity_id+others — using
        # the asset_id as a tag inside `evidence` is sufficient for the
        # cost-budget use case.
        payload.setdefault("evidence", {})
        payload["evidence"]["asset_id"] = asset_id
    try:
        sb._rest(
            "POST", "operator_flags",
            json_body=payload,
            prefer="resolution=ignore-duplicates",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("operator_flags upsert failed: %s", exc)


def check_24h_thresholds(sb, asset_id: str) -> Dict[str, Any]:
    """End-of-run rollup. Fires operator_flag for each breached threshold.
    Returns a small dict for telemetry: {asset_total, global_total,
    asset_breach, global_breach}."""
    asset_total = asset_24h_cost_usd(sb, asset_id)
    global_total = global_24h_cost_usd(sb)
    asset_breach = asset_total > ASSET_24H_SOFT_USD
    global_breach = global_total > GLOBAL_24H_SOFT_USD
    if asset_breach:
        upsert_cost_flag(
            sb,
            severity="warn",
            kind=ASSET_FLAG_KIND,
            title=f"Asset 24h spend ${asset_total:.2f} > ${ASSET_24H_SOFT_USD:.0f}",
            body=(
                f"Asset {asset_id} convergence_assessments cost in the last "
                f"24h is ${asset_total:.2f}, above the ${ASSET_24H_SOFT_USD:.0f} "
                "soft alert threshold. No runs are blocked; investigate the "
                "cost driver (ensemble_n, retries, asset volume)."
            ),
            asset_id=asset_id,
            evidence={
                "asset_id": asset_id,
                "asset_total_24h_usd": round(asset_total, 4),
                "threshold_usd": ASSET_24H_SOFT_USD,
            },
        )
    if global_breach:
        upsert_cost_flag(
            sb,
            severity="warn",
            kind=GLOBAL_FLAG_KIND,
            title=(
                f"Global 24h spend ${global_total:.2f} > "
                f"${GLOBAL_24H_SOFT_USD:.0f}"
            ),
            body=(
                f"Aggregate orchestrator spend in the last 24h is "
                f"${global_total:.2f}, above the ${GLOBAL_24H_SOFT_USD:.0f} "
                "soft alert threshold. Review the queue depth, ensemble "
                "settings, and any runaway assets."
            ),
            evidence={
                "global_total_24h_usd": round(global_total, 4),
                "threshold_usd": GLOBAL_24H_SOFT_USD,
            },
        )
    return {
        "asset_total_usd": round(asset_total, 4),
        "global_total_usd": round(global_total, 4),
        "asset_breach": asset_breach,
        "global_breach": global_breach,
    }
