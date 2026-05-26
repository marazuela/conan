"""Per-signal-category accuracy aggregation for v4 Phase 7.

Reads resolved post_mortem_queue rows (status='post_mortem_complete') over
a trailing cohort window, groups by (profile, signal_category, horizon_days),
and emits a structured metrics dict suitable for persistence to
feedback_category_metrics.

What the daily snapshot looks like (per group):
  - n_cases, hit_count, miss_count, no_outcome_count
  - hit_rate                 = hits / (hits + misses)
  - mean_prediction_error    = mean(predicted_pct - realized_pct) where
                               realized_pct = 100*HIT-flag
  - mae                      = mean absolute error
  - brier_score              = mean((conviction/100 - HIT-flag)^2)
  - mean_conviction_pct      = mean(predicted_conviction_pct)

The aggregator is intentionally a pure function over `rows` so it can be
unit-tested without a Supabase round trip. The loader + persister are thin
wrappers that the daily_feedback_loop calls.

Plan: ~/.claude/plans/proud-booping-seal.md (Phase 7).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

from modal_workers.shared.supabase_client import SupabaseClient

logger = logging.getLogger(__name__)


# Default horizons to break out — match the existing post_mortem_runner
# label horizons (label_forward_returns.label_event computes T+30/60/90/180).
DEFAULT_HORIZONS = (30, 60, 90, 180)

# Default cohort window. 90 days is the Phase 7 default — long enough for
# the slow horizons to mature, short enough to detect category drift.
DEFAULT_COHORT_DAYS = 90

# Minimum n per (profile, signal_category, horizon) cell before we report
# metrics. Below this, the cell is recorded with n_cases but metrics=NULL
# so dashboards don't display noise.
MIN_N_FOR_METRICS = 5


@dataclass(frozen=True)
class CategoryKey:
    profile: str
    signal_category: str
    horizon_days: int


@dataclass
class CategoryMetrics:
    """One row's worth of per-category accuracy. Maps to
    feedback_category_metrics columns."""

    key: CategoryKey
    n_cases: int = 0
    hit_count: int = 0
    miss_count: int = 0
    no_outcome_count: int = 0
    hit_rate: Optional[float] = None
    mean_prediction_error: Optional[float] = None
    mae: Optional[float] = None
    brier_score: Optional[float] = None
    mean_conviction_pct: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


def _outcome_flag(realized_outcome: Optional[Dict[str, Any]], horizon: int) -> Optional[int]:
    """Resolve a HIT/MISS flag from realized_outcome for the given horizon.

    `realized_outcome` is the jsonb from post_mortem_runner. Shape varies by
    label_rule, but the common convention is a per-horizon block with
    `verdict in {"HIT", "MISS"}`. Returns 1 / 0 / None.
    """
    if not isinstance(realized_outcome, dict):
        return None

    # Common shape: {"horizons": {"30": {"verdict": "HIT"}, "60": {...}}}
    horizons = realized_outcome.get("horizons")
    if isinstance(horizons, dict):
        bucket = horizons.get(str(horizon)) or horizons.get(horizon)
        if isinstance(bucket, dict):
            verdict = (bucket.get("verdict") or "").upper()
            if verdict == "HIT":
                return 1
            if verdict == "MISS":
                return 0

    # Fallback: top-level "verdict" applies to the default horizon (30d).
    if horizon == 30:
        verdict = (realized_outcome.get("verdict") or "").upper()
        if verdict == "HIT":
            return 1
        if verdict == "MISS":
            return 0

    return None


def aggregate_by_category(
    rows: Iterable[Dict[str, Any]],
    *,
    horizons: Iterable[int] = DEFAULT_HORIZONS,
    min_n: int = MIN_N_FOR_METRICS,
) -> List[CategoryMetrics]:
    """Pure aggregator. Inputs are post_mortem_queue rows (joined with
    convergence_assessments to carry `profile` + `signal_category`). Output
    is one CategoryMetrics per non-empty (profile, signal_category, horizon)
    cell.

    Rows missing profile, signal_category, or predicted_conviction_pct are
    silently skipped — they shouldn't appear in production but defensive
    code matters at the aggregation layer.
    """
    horizons = tuple(horizons)
    buckets: Dict[CategoryKey, Dict[str, Any]] = defaultdict(
        lambda: {
            "predicted": [],
            "realized": [],
            "no_outcome": 0,
        }
    )

    for row in rows:
        profile = row.get("profile") or row.get("scoring_profile")
        category = row.get("signal_category")
        predicted_pct = row.get("predicted_conviction_pct")
        if profile is None or category is None or predicted_pct is None:
            continue
        try:
            predicted_pct = float(predicted_pct)
        except (TypeError, ValueError):
            continue
        realized_outcome = row.get("realized_outcome")

        for horizon in horizons:
            key = CategoryKey(profile=profile, signal_category=category, horizon_days=horizon)
            bucket = buckets[key]
            flag = _outcome_flag(realized_outcome, horizon)
            if flag is None:
                bucket["no_outcome"] += 1
                continue
            bucket["predicted"].append(predicted_pct)
            bucket["realized"].append(flag)

    out: List[CategoryMetrics] = []
    for key, bucket in buckets.items():
        n_cases = len(bucket["predicted"]) + bucket["no_outcome"]
        hit_count = sum(1 for r in bucket["realized"] if r == 1)
        miss_count = sum(1 for r in bucket["realized"] if r == 0)
        m = CategoryMetrics(
            key=key,
            n_cases=n_cases,
            hit_count=hit_count,
            miss_count=miss_count,
            no_outcome_count=bucket["no_outcome"],
        )
        scored_n = hit_count + miss_count
        if scored_n >= min_n:
            m.hit_rate = round(hit_count / scored_n, 4)
            errors = [
                p - (r * 100.0)
                for p, r in zip(bucket["predicted"], bucket["realized"])
            ]
            m.mean_prediction_error = round(sum(errors) / scored_n, 4)
            m.mae = round(sum(abs(e) for e in errors) / scored_n, 4)
            brier_terms = [
                (p / 100.0 - r) ** 2
                for p, r in zip(bucket["predicted"], bucket["realized"])
            ]
            m.brier_score = round(sum(brier_terms) / scored_n, 4)
            m.mean_conviction_pct = round(
                sum(bucket["predicted"]) / scored_n, 2,
            )
        out.append(m)
    return out


def load_post_mortem_rows(
    sb: SupabaseClient,
    *,
    cohort_window_start: date,
    cohort_window_end: date,
) -> List[Dict[str, Any]]:
    """Read resolved post_mortem_queue rows joined with convergence_assessments
    so each row carries the profile + signal_category needed for grouping.

    PostgREST embedded-resource syntax: `convergence_assessments(...)` pulls
    the parent row's fields into a nested object. We flatten on the client
    side.
    """
    rows = sb._rest(
        "GET",
        "post_mortem_queue",
        params={
            "select": (
                "id,assessment_id,predicted_conviction_pct,predicted_direction,"
                "realized_outcome,prediction_error,signal_category,realized_at,"
                "convergence_assessments(asset_id,thesis_direction,band)"
            ),
            "status": "eq.post_mortem_complete",
            "realized_at": (
                f"gte.{cohort_window_start.isoformat()}T00:00:00Z,"
                f"lte.{cohort_window_end.isoformat()}T23:59:59Z"
            ),
            "signal_category": "not.is.null",
            "order": "realized_at.desc",
        },
    ) or []

    # PostgREST returns the embedded parent as a list/dict depending on
    # FK direction. We need the profile from convergence_assessments — but
    # convergence_assessments has no `profile` column directly; profile
    # lives on the originating signal. For Phase 7 MVP we infer from
    # `band` (binary_catalyst defaults; refine when profile flows down to
    # convergence_assessments in a follow-up migration).
    flat: List[Dict[str, Any]] = []
    for r in rows:
        ca = r.get("convergence_assessments")
        if isinstance(ca, list):
            ca = ca[0] if ca else {}
        ca = ca or {}
        flat.append({
            "id": r.get("id"),
            "assessment_id": r.get("assessment_id"),
            "predicted_conviction_pct": r.get("predicted_conviction_pct"),
            "predicted_direction": r.get("predicted_direction"),
            "realized_outcome": r.get("realized_outcome"),
            "prediction_error": r.get("prediction_error"),
            "signal_category": r.get("signal_category"),
            "realized_at": r.get("realized_at"),
            # Profile placeholder — see note above; binary_catalyst is the
            # only active profile under v4, so this defaults safely.
            "profile": "binary_catalyst",
            "asset_id": ca.get("asset_id"),
            "thesis_direction": ca.get("thesis_direction"),
            "band": ca.get("band"),
        })
    return flat


def persist_category_metrics(
    sb: SupabaseClient,
    *,
    snapshot_date: date,
    cohort_window_start: date,
    cohort_window_end: date,
    metrics: List[CategoryMetrics],
    aggregation_version: str = "category_accuracy_v0",
) -> int:
    """Upsert one row per CategoryMetrics into feedback_category_metrics.

    Uses the (snapshot_date, profile, signal_category, horizon_days) UNIQUE
    constraint for idempotency — re-running the aggregator for the same
    snapshot_date overwrites the previous metrics rather than appending
    stale duplicates.

    Returns the number of rows written.
    """
    rows = []
    for m in metrics:
        rows.append({
            "snapshot_date": snapshot_date.isoformat(),
            "profile": m.key.profile,
            "signal_category": m.key.signal_category,
            "horizon_days": m.key.horizon_days,
            "n_cases": m.n_cases,
            "hit_count": m.hit_count,
            "miss_count": m.miss_count,
            "no_outcome_count": m.no_outcome_count,
            "hit_rate": m.hit_rate,
            "mean_prediction_error": m.mean_prediction_error,
            "mae": m.mae,
            "brier_score": m.brier_score,
            "mean_conviction_pct": m.mean_conviction_pct,
            "cohort_window_start": cohort_window_start.isoformat(),
            "cohort_window_end": cohort_window_end.isoformat(),
            "metadata": {
                "aggregation_version": aggregation_version,
                **m.metadata,
            },
        })
    if not rows:
        return 0
    sb._rest(
        "POST",
        "feedback_category_metrics",
        json_body=rows,
        prefer="resolution=merge-duplicates",
    )
    return len(rows)


def run_daily_snapshot(
    sb: SupabaseClient,
    *,
    today: Optional[date] = None,
    cohort_days: int = DEFAULT_COHORT_DAYS,
    horizons: Iterable[int] = DEFAULT_HORIZONS,
) -> Dict[str, Any]:
    """End-to-end: load → aggregate → persist. Hook for daily_feedback_loop.

    Returns a small dict suitable for logging:
      {snapshot_date, cohort_window, rows_persisted, metric_cells_total}
    """
    today = today or datetime.now(timezone.utc).date()
    cohort_window_end = today
    cohort_window_start = today - timedelta(days=cohort_days)

    rows = load_post_mortem_rows(
        sb,
        cohort_window_start=cohort_window_start,
        cohort_window_end=cohort_window_end,
    )
    metrics = aggregate_by_category(rows, horizons=horizons)
    rows_persisted = persist_category_metrics(
        sb,
        snapshot_date=today,
        cohort_window_start=cohort_window_start,
        cohort_window_end=cohort_window_end,
        metrics=metrics,
    )
    return {
        "snapshot_date": today.isoformat(),
        "cohort_window_start": cohort_window_start.isoformat(),
        "cohort_window_end": cohort_window_end.isoformat(),
        "metric_cells_total": len(metrics),
        "rows_persisted": rows_persisted,
        "input_rows": len(rows),
    }
