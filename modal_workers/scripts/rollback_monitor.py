"""v3 closed feedback loop — calibration drift rollback monitor (Stream 2, D-104).

Daily Modal scheduled function. Computes Spearman correlation between
realized 30-day forward returns and the calibrated conviction_pct over the
last 30 days of resolved post_mortem_queue rows. Triggers a rollback when:

  - n_resolved_in_window >= MIN_N (currently 30)  [silence window otherwise]
  - AND (
       spearman_corr < LOW_CORRELATION_THRESHOLD (0.20)
       OR
       delta_from_prior <= -CORRELATION_DROP_THRESHOLD (-0.15)
     )

Rollback action:
  1. Find the most recent prior is_active=true calibration_curves row
     (use the calibration_drift_log's active_curve_version_pre history).
  2. PATCH calibration_curves: SET is_active=true on the prior version,
     is_active=false on the current. Atomic via two REST calls (race
     window negligible — drainer reads with limit=1).
  3. INSERT operator_flag(severity=critical, source=rollback_monitor).
  4. INSERT calibration_drift_log row with rollback_triggered=true.

The monitor is conservative by default: low-n short-circuits with
no_baseline; the rollback requires BOTH a low absolute correlation OR a
sharp drop. False positives are recoverable (operator re-activates via
fda_calibration_activate), false negatives just leave drift undetected
for another 24h.

CLI: python -m modal_workers.scripts.rollback_monitor --window-days 30
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from modal_workers.shared.supabase_client import SupabaseClient

logger = logging.getLogger(__name__)

# Thresholds (D-104).
DEFAULT_WINDOW_DAYS = 30
MIN_N_FOR_DRIFT_DECISION = 30
LOW_CORRELATION_THRESHOLD = 0.20
CORRELATION_DROP_THRESHOLD = 0.15  # absolute drop in correlation vs prior window

DRY_RUN_ENV = "ROLLBACK_MONITOR_DRY_RUN"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class DriftSnapshot:
    spearman_corr: Optional[float]
    n_resolved_in_window: int
    delta_from_prior: Optional[float]
    rollback_triggered: bool
    rollback_reason: str  # 'low_correlation'|'correlation_drop'|'below_min_n'|'no_baseline'|'no_drift'
    active_curve_version_pre: Optional[str]
    active_curve_version_post: Optional[str]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_drift_and_maybe_rollback(
    *,
    sb: Optional[SupabaseClient] = None,
    now: Optional[datetime] = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    dry_run: Optional[bool] = None,
) -> DriftSnapshot:
    """Main entry point — daily monitor pass."""
    sb = sb or SupabaseClient()
    now = now or datetime.now(timezone.utc)
    if dry_run is None:
        dry_run = os.environ.get(DRY_RUN_ENV, "false").lower() == "true"

    pairs = _fetch_resolved_pairs(sb, now=now, window_days=window_days)
    n = len(pairs)

    # Read the prior monitor pass for delta_from_prior.
    prior = _fetch_prior_log(sb)
    prior_corr = prior.get("spearman_corr") if prior else None
    active_pre = _fetch_active_curve_version(sb)

    if n < MIN_N_FOR_DRIFT_DECISION:
        snapshot = DriftSnapshot(
            spearman_corr=None,
            n_resolved_in_window=n,
            delta_from_prior=None,
            rollback_triggered=False,
            rollback_reason="below_min_n",
            active_curve_version_pre=active_pre,
            active_curve_version_post=active_pre,
        )
        if not dry_run:
            _insert_drift_log(sb, snapshot)
        return snapshot

    realized, predicted = zip(*pairs)
    corr = spearman_corr(list(predicted), list(realized))
    delta = (corr - prior_corr) if (prior_corr is not None) else None

    rollback_reason = _classify_drift(corr=corr, delta=delta)
    rollback_triggered = rollback_reason in ("low_correlation", "correlation_drop")

    active_post = active_pre
    if rollback_triggered and not dry_run:
        prior_version = _fetch_prior_active_curve_version(sb, current_version=active_pre)
        if prior_version:
            _execute_rollback(sb, current=active_pre, prior=prior_version,
                              reason=rollback_reason, corr=corr, delta=delta)
            active_post = prior_version
        else:
            # Can't rollback — no prior curve. Demote-only: deactivate current.
            _deactivate_current_curve(sb, current=active_pre,
                                      reason=f"{rollback_reason}_no_prior")
            active_post = None
            rollback_reason = f"{rollback_reason}_no_prior_curve"

    snapshot = DriftSnapshot(
        spearman_corr=round(corr, 4) if corr is not None else None,
        n_resolved_in_window=n,
        delta_from_prior=round(delta, 4) if delta is not None else None,
        rollback_triggered=rollback_triggered,
        rollback_reason=rollback_reason,
        active_curve_version_pre=active_pre,
        active_curve_version_post=active_post,
    )
    if not dry_run:
        _insert_drift_log(sb, snapshot)
    return snapshot


# ---------------------------------------------------------------------------
# Pure helpers (testable)
# ---------------------------------------------------------------------------

def spearman_corr(xs: Sequence[float], ys: Sequence[float]) -> float:
    """Spearman rank correlation. Returns 0.0 with degenerate inputs.

    Ties get average ranks. Stable on small n (no scipy dep).
    """
    n = len(xs)
    if n != len(ys) or n < 2:
        return 0.0
    rx = _ranks(xs)
    ry = _ranks(ys)
    mean_x = sum(rx) / n
    mean_y = sum(ry) / n
    num = sum((rx[i] - mean_x) * (ry[i] - mean_y) for i in range(n))
    den_x = sum((rx[i] - mean_x) ** 2 for i in range(n))
    den_y = sum((ry[i] - mean_y) ** 2 for i in range(n))
    if den_x == 0 or den_y == 0:
        return 0.0
    return num / math.sqrt(den_x * den_y)


def _ranks(values: Sequence[float]) -> List[float]:
    """Average-rank assignment for ties. 1-indexed."""
    n = len(values)
    indexed = sorted(enumerate(values), key=lambda t: t[1])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j < n and indexed[j][1] == indexed[i][1]:
            j += 1
        avg = (i + j + 1) / 2.0  # 1-indexed inclusive average
        for k in range(i, j):
            ranks[indexed[k][0]] = avg
        i = j
    return ranks


def _classify_drift(*, corr: float, delta: Optional[float]) -> str:
    """Decide rollback reason given correlation + delta from prior pass.

    Both conditions BOTH-OR-EITHER trigger; we surface the dominant cause:
      - corr < LOW_CORRELATION_THRESHOLD                  → 'low_correlation'
      - delta < -CORRELATION_DROP_THRESHOLD               → 'correlation_drop'
      - else                                              → 'no_drift'
    """
    low = corr < LOW_CORRELATION_THRESHOLD
    dropped = delta is not None and delta <= -CORRELATION_DROP_THRESHOLD
    if low and dropped:
        # Pick the more severe: the absolute floor wins.
        return "low_correlation"
    if low:
        return "low_correlation"
    if dropped:
        return "correlation_drop"
    return "no_drift"


# ---------------------------------------------------------------------------
# DB I/O
# ---------------------------------------------------------------------------

def _fetch_resolved_pairs(
    sb: SupabaseClient, *, now: datetime, window_days: int,
) -> List[Tuple[float, float]]:
    """Return [(realized_30d_return_pct, conviction_pct_calibrated), ...] for
    post_mortem_complete rows whose realized_at is within window_days.
    """
    since = (now - timedelta(days=window_days)).isoformat()
    pms = sb._rest("GET", "post_mortem_queue", params={
        "select": "assessment_id,realized_outcome,realized_at",
        "status": "eq.post_mortem_complete",
        "realized_at": f"gte.{since}",
        "limit": "10000",
    }) or []
    if not pms:
        return []

    assessment_ids = [p["assessment_id"] for p in pms]
    in_filter = f"in.({','.join(assessment_ids)})"
    assessments = sb._rest("GET", "convergence_assessments", params={
        "select": "id,conviction_pct_calibrated,conviction_pct",
        "id": in_filter,
        "limit": "10000",
    }) or []
    asmt = {a["id"]: a for a in assessments}

    pairs: List[Tuple[float, float]] = []
    for p in pms:
        ro = p.get("realized_outcome") or {}
        # Pull T+30 return from windows array.
        windows = ro.get("windows") or []
        w30 = next((w for w in windows if w.get("days") == 30 and w.get("status") == "ok"), None)
        if not w30 or w30.get("return_pct") is None:
            continue
        a = asmt.get(p["assessment_id"])
        if not a:
            continue
        cal = a.get("conviction_pct_calibrated") or a.get("conviction_pct")
        if cal is None:
            continue
        pairs.append((float(w30["return_pct"]), float(cal)))
    return pairs


def _fetch_prior_log(sb: SupabaseClient) -> Optional[Dict[str, Any]]:
    rows = sb._rest("GET", "calibration_drift_log", params={
        "select": "spearman_corr,computed_at,rollback_triggered",
        "order": "computed_at.desc",
        "limit": "1",
    }) or []
    return rows[0] if rows else None


def _fetch_active_curve_version(sb: SupabaseClient) -> Optional[str]:
    rows = sb._rest("GET", "calibration_curves", params={
        "select": "version",
        "is_active": "eq.true",
        "limit": "1",
    }) or []
    return rows[0]["version"] if rows else None


def _fetch_prior_active_curve_version(
    sb: SupabaseClient, *, current_version: Optional[str],
) -> Optional[str]:
    """Find the calibration_curves row most recently fitted before the current
    one. Used as the rollback target.
    """
    rows = sb._rest("GET", "calibration_curves", params={
        "select": "version,fitted_at",
        "order": "fitted_at.desc",
        "limit": "10",
    }) or []
    for r in rows:
        if r["version"] != current_version:
            return r["version"]
    return None


def _execute_rollback(
    sb: SupabaseClient,
    *,
    current: Optional[str],
    prior: str,
    reason: str,
    corr: float,
    delta: Optional[float],
) -> None:
    if current:
        sb._rest_with_retry("PATCH", "calibration_curves",
                            params={"version": f"eq.{current}"},
                            json_body={"is_active": False},
                            prefer="return=minimal")
    sb._rest_with_retry("PATCH", "calibration_curves",
                        params={"version": f"eq.{prior}"},
                        json_body={"is_active": True},
                        prefer="return=minimal")
    # Surface to operators.
    _insert_operator_flag(sb, current=current, prior=prior, reason=reason,
                          corr=corr, delta=delta)


def _deactivate_current_curve(
    sb: SupabaseClient,
    *,
    current: Optional[str],
    reason: str,
) -> None:
    if not current:
        return
    sb._rest_with_retry("PATCH", "calibration_curves",
                        params={"version": f"eq.{current}"},
                        json_body={"is_active": False},
                        prefer="return=minimal")
    _insert_operator_flag(sb, current=current, prior=None, reason=reason,
                          corr=None, delta=None)


def _insert_operator_flag(
    sb: SupabaseClient,
    *,
    current: Optional[str],
    prior: Optional[str],
    reason: str,
    corr: Optional[float],
    delta: Optional[float],
) -> None:
    sb._rest_with_retry("POST", "operator_flags", json_body={
        "severity": "critical",
        "source": "rollback_monitor",
        "kind": "calibration_drift_rollback",
        "title": f"Calibration drift rollback fired ({reason})",
        "evidence": {
            "active_curve_version_pre": current,
            "active_curve_version_post": prior,
            "rollback_reason": reason,
            "spearman_corr": corr,
            "delta_from_prior": delta,
            "computed_at": datetime.now(timezone.utc).isoformat(),
        },
    }, prefer="return=minimal")


def _insert_drift_log(sb: SupabaseClient, snapshot: DriftSnapshot) -> None:
    sb._rest_with_retry("POST", "calibration_drift_log", json_body={
        "spearman_corr": snapshot.spearman_corr,
        "n_resolved_in_window": snapshot.n_resolved_in_window,
        "delta_from_prior": snapshot.delta_from_prior,
        "rollback_triggered": snapshot.rollback_triggered,
        "rollback_reason": snapshot.rollback_reason,
        "active_curve_version_pre": snapshot.active_curve_version_pre,
        "active_curve_version_post": snapshot.active_curve_version_post,
    }, prefer="return=minimal")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Calibration drift rollback monitor (D-104).")
    parser.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    snapshot = check_drift_and_maybe_rollback(
        window_days=args.window_days,
        dry_run=args.dry_run,
    )
    logger.info(
        "rollback_monitor: n=%d corr=%s delta=%s reason=%s rollback=%s",
        snapshot.n_resolved_in_window,
        snapshot.spearman_corr, snapshot.delta_from_prior,
        snapshot.rollback_reason, snapshot.rollback_triggered,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
