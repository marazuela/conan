"""Phase 7 tests: per-category accuracy aggregation + feedback retrospective skill.

Locks down the three Phase 7 deliverables:

1. Migration adds feedback_category_metrics + rubric_proposals tables.
2. modal_workers/feedback/category_accuracy.py: pure aggregator over
   resolved post_mortem_queue rows (HIT/MISS verdicts → Brier + MAE +
   hit_rate per category), plus DB load + persist hooks.
3. .claude/skills/feedback_retrospective.md: Cowork skill that reads the
   metrics + active rubric, proposes weight changes, writes to
   rubric_proposals with status='pending_operator_review' (never applies).

Plan: ~/.claude/plans/proud-booping-seal.md (Phase 7).
Run: python3 -m pytest modal_workers/tests/test_orchestrator_v4_phase7.py -v
"""
from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List

os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")


REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Migration shape
# ---------------------------------------------------------------------------

def _migration_path() -> Path:
    return (
        REPO_ROOT / "supabase" / "migrations"
        / "20260613008000_v4_feedback_retrospective_schema.sql"
    )


def test_phase7_migration_exists():
    assert _migration_path().exists(), (
        f"Phase 7 migration missing at {_migration_path()}"
    )


def test_migration_creates_feedback_category_metrics():
    sql = _migration_path().read_text()
    assert "CREATE TABLE IF NOT EXISTS public.feedback_category_metrics" in sql

    # Load-bearing columns the aggregator persists.
    for col in (
        "snapshot_date",
        "profile",
        "signal_category",
        "horizon_days",
        "n_cases",
        "hit_rate",
        "brier_score",
        "mean_prediction_error",
        "cohort_window_start",
        "cohort_window_end",
    ):
        assert col in sql, f"feedback_category_metrics missing column: {col}"

    # Unique constraint for daily idempotent upserts.
    assert "UNIQUE (snapshot_date, profile, signal_category, horizon_days)" in sql


def test_migration_creates_rubric_proposals():
    sql = _migration_path().read_text()
    assert "CREATE TABLE IF NOT EXISTS public.rubric_proposals" in sql

    # Load-bearing columns for the proposal flow.
    for col in (
        "proposed_weights",
        "current_weights",
        "rationale",
        "cohort_window_start",
        "cohort_size",
        "status",
        "approved_by",
        "rejected_reason",
        "applied_rubric_id",
    ):
        assert col in sql, f"rubric_proposals missing column: {col}"

    # Status check constraint.
    assert "status IN ('pending_operator_review', 'approved', 'rejected', 'superseded')" in sql

    # Partial index for the dashboard's pending-proposals view.
    assert "idx_rubric_proposals_pending" in sql


# ---------------------------------------------------------------------------
# Aggregator — pure function
# ---------------------------------------------------------------------------

def _row(signal_category: str, predicted: float, horizon_to_verdict: Dict[int, str]) -> Dict[str, Any]:
    """Build a minimal post_mortem_queue+convergence row for the aggregator."""
    return {
        "profile": "binary_catalyst",
        "signal_category": signal_category,
        "predicted_conviction_pct": predicted,
        "realized_outcome": {
            "horizons": {
                str(h): {"verdict": v} for h, v in horizon_to_verdict.items()
            }
        },
    }


def test_aggregator_returns_one_cell_per_category_horizon():
    from modal_workers.feedback.category_accuracy import (
        aggregate_by_category,
    )

    # 5 cases per category — minimum n for metrics to populate (default 5).
    rows = [
        _row("insider_activity", 75.0, {30: "HIT"}),
        _row("insider_activity", 72.0, {30: "HIT"}),
        _row("insider_activity", 80.0, {30: "MISS"}),
        _row("insider_activity", 65.0, {30: "HIT"}),
        _row("insider_activity", 70.0, {30: "MISS"}),
    ]
    metrics = aggregate_by_category(rows, horizons=(30,), min_n=5)

    assert len(metrics) == 1
    m = metrics[0]
    assert m.key.signal_category == "insider_activity"
    assert m.key.horizon_days == 30
    assert m.n_cases == 5
    assert m.hit_count == 3
    assert m.miss_count == 2
    assert m.hit_rate == 0.6


def test_aggregator_computes_brier_score():
    """Brier = mean((p - y)^2) where p = conviction/100, y ∈ {0,1}.
    For a perfectly-confident correct prediction p=1, y=1 → 0. For
    over-confident wrong p=1, y=0 → 1. Mid-range mixed → small fraction."""
    from modal_workers.feedback.category_accuracy import (
        aggregate_by_category,
    )

    # All five HIT, all predicted 80% → Brier = mean((0.8 - 1)^2) = 0.04.
    rows = [_row("insider_activity", 80.0, {30: "HIT"}) for _ in range(5)]
    metrics = aggregate_by_category(rows, horizons=(30,), min_n=5)
    assert metrics[0].brier_score == 0.04


def test_aggregator_below_min_n_leaves_metrics_null():
    """Categories with fewer than min_n scored cases must keep metrics
    at None so dashboards don't display noise. n_cases / counts still
    populate so the cohort presence is visible."""
    from modal_workers.feedback.category_accuracy import (
        aggregate_by_category,
    )

    rows = [
        _row("insider_activity", 75.0, {30: "HIT"}),
        _row("insider_activity", 72.0, {30: "MISS"}),
        # Only 2 scored cases. min_n=5 → metrics stay None.
    ]
    metrics = aggregate_by_category(rows, horizons=(30,), min_n=5)
    assert len(metrics) == 1
    m = metrics[0]
    assert m.n_cases == 2
    assert m.hit_count == 1
    assert m.miss_count == 1
    assert m.hit_rate is None, "below min_n must leave hit_rate null"
    assert m.brier_score is None
    assert m.mean_prediction_error is None


def test_aggregator_handles_no_outcome_separately():
    """When realized_outcome is missing or verdict is neither HIT nor MISS,
    the case counts toward n_cases via no_outcome_count, never confused
    with hit or miss."""
    from modal_workers.feedback.category_accuracy import (
        aggregate_by_category,
    )

    rows = [
        _row("insider_activity", 75.0, {30: "HIT"}),
        _row("insider_activity", 72.0, {30: "HIT"}),
        # Stale row — no realized_outcome.
        {
            "profile": "binary_catalyst",
            "signal_category": "insider_activity",
            "predicted_conviction_pct": 70.0,
            "realized_outcome": None,
        },
    ]
    metrics = aggregate_by_category(rows, horizons=(30,), min_n=2)
    m = metrics[0]
    assert m.n_cases == 3
    assert m.hit_count == 2
    assert m.no_outcome_count == 1
    assert m.hit_rate == 1.0


def test_aggregator_skips_rows_missing_required_fields():
    """Defensive — rows without profile / signal_category / predicted_conviction_pct
    are silently dropped (shouldn't appear in production but the join could
    surface NULLs)."""
    from modal_workers.feedback.category_accuracy import (
        aggregate_by_category,
    )

    rows = [
        _row("insider_activity", 75.0, {30: "HIT"}),
        {"profile": None, "signal_category": "insider_activity",
         "predicted_conviction_pct": 80.0},  # missing profile
        {"profile": "binary_catalyst", "signal_category": None,
         "predicted_conviction_pct": 80.0},  # missing category
        {"profile": "binary_catalyst", "signal_category": "insider_activity",
         "predicted_conviction_pct": None},  # missing prediction
    ]
    metrics = aggregate_by_category(rows, horizons=(30,), min_n=1)
    assert len(metrics) == 1
    assert metrics[0].n_cases == 1


def test_aggregator_separates_horizons():
    """Each (category, horizon) tuple gets its own metric cell."""
    from modal_workers.feedback.category_accuracy import (
        aggregate_by_category,
    )

    rows = [
        _row("insider_activity", 75.0, {30: "HIT", 60: "HIT", 90: "MISS"})
        for _ in range(5)
    ]
    metrics = aggregate_by_category(rows, horizons=(30, 60, 90), min_n=5)
    assert len(metrics) == 3
    by_h = {m.key.horizon_days: m for m in metrics}
    assert by_h[30].hit_rate == 1.0
    assert by_h[60].hit_rate == 1.0
    assert by_h[90].hit_rate == 0.0


# ---------------------------------------------------------------------------
# Persistence smoke (stubbed Supabase)
# ---------------------------------------------------------------------------

class _StubSupabase:
    def __init__(self, response=None):
        self.calls: List[Dict[str, Any]] = []
        self._response = response

    def _rest(self, method, path, params=None, json_body=None, **kw):
        self.calls.append({
            "method": method, "path": path,
            "params": params, "json_body": json_body, "prefer": kw.get("prefer"),
        })
        return self._response


def test_persist_writes_one_row_per_metric_with_upsert_prefer():
    from modal_workers.feedback.category_accuracy import (
        CategoryKey,
        CategoryMetrics,
        persist_category_metrics,
    )

    sb = _StubSupabase(response=[{"id": "x"}])
    metrics = [
        CategoryMetrics(
            key=CategoryKey(
                profile="binary_catalyst",
                signal_category="insider_activity",
                horizon_days=30,
            ),
            n_cases=10, hit_count=6, miss_count=4,
            hit_rate=0.6, brier_score=0.21,
            mean_prediction_error=2.5, mae=15.3, mean_conviction_pct=68.0,
        )
    ]
    n = persist_category_metrics(
        sb,
        snapshot_date=date(2026, 5, 25),
        cohort_window_start=date(2026, 4, 25),
        cohort_window_end=date(2026, 5, 25),
        metrics=metrics,
    )
    assert n == 1
    assert len(sb.calls) == 1
    call = sb.calls[0]
    assert call["method"] == "POST"
    assert call["path"] == "feedback_category_metrics"
    # merge-duplicates is what makes the daily snapshot idempotent.
    assert call["prefer"] == "resolution=merge-duplicates"

    body = call["json_body"]
    assert isinstance(body, list)
    assert body[0]["signal_category"] == "insider_activity"
    assert body[0]["horizon_days"] == 30
    assert body[0]["snapshot_date"] == "2026-05-25"
    assert body[0]["brier_score"] == 0.21


def test_persist_skips_db_call_when_no_metrics():
    """Empty cohort → don't fire an empty INSERT."""
    from modal_workers.feedback.category_accuracy import (
        persist_category_metrics,
    )

    sb = _StubSupabase()
    n = persist_category_metrics(
        sb,
        snapshot_date=date(2026, 5, 25),
        cohort_window_start=date(2026, 4, 25),
        cohort_window_end=date(2026, 5, 25),
        metrics=[],
    )
    assert n == 0
    assert sb.calls == []


# ---------------------------------------------------------------------------
# Cowork skill markdown invariants
# ---------------------------------------------------------------------------

def _skill_path() -> Path:
    return REPO_ROOT / ".claude" / "skills" / "feedback_retrospective.md"


def test_feedback_retrospective_skill_exists():
    assert _skill_path().exists(), (
        f"feedback_retrospective skill missing at {_skill_path()}. "
        f"Must be committed in the sibling conan-cowork-skills repo."
    )


def test_skill_writes_only_pending_proposals_never_applies():
    """The whole point of human-in-the-loop: this skill must not mutate
    rubrics directly. It writes to rubric_proposals with the default
    status — operator approval gates the actual change."""
    body = _skill_path().read_text().lower()

    assert "rubric_proposals" in body
    assert "pending_operator_review" in body
    # Must explicitly disclaim direct rubric mutation.
    assert "does not apply rubric changes directly" in body or \
           "does not modify" in body or \
           "never applies" in body


def test_skill_enforces_no_price_gate_covenant():
    """Phase 5's price-gate lint applies here too — proposals must not
    introduce price-based dimensions."""
    body = _skill_path().read_text().lower()

    assert "no-price-gate" in body or "stock-price" in body or \
           "stock_price" in body or "market_cap" in body, (
        "skill must reference the v4 no-price-gate covenant"
    )


def test_skill_enforces_magnitude_discipline():
    """Weight changes per proposal are bounded. Without this guardrail,
    one over-confident retro could swing the rubric dramatically."""
    body = _skill_path().read_text().lower()

    assert "magnitude discipline" in body or "±0.5" in body or \
           "0.5" in body, "skill must document weight-change magnitude bounds"


def test_skill_documents_cohort_thinness_skip():
    """Thin cohorts produce noisy proposals — skill must skip with a
    clear reason instead of running anyway."""
    body = _skill_path().read_text().lower()

    assert "cohort_too_thin" in body or "cohort too thin" in body or \
           "thin cohort" in body or "thin, skip" in body or \
           "below" in body.split("\n")[20:40].__str__()  # near the invariants list


# ---------------------------------------------------------------------------
# Outcome flag resolution
# ---------------------------------------------------------------------------

def test_outcome_flag_resolves_per_horizon_block():
    from modal_workers.feedback.category_accuracy import _outcome_flag

    realized = {"horizons": {"30": {"verdict": "HIT"}, "60": {"verdict": "MISS"}}}
    assert _outcome_flag(realized, 30) == 1
    assert _outcome_flag(realized, 60) == 0
    assert _outcome_flag(realized, 90) is None


def test_outcome_flag_falls_back_to_top_level_verdict_for_30d():
    """Older post_mortem rows used a flat verdict shape — 30d is the
    legacy default horizon, so we accept that shape only at horizon=30."""
    from modal_workers.feedback.category_accuracy import _outcome_flag

    legacy = {"verdict": "HIT"}
    assert _outcome_flag(legacy, 30) == 1
    # Other horizons can't infer from the legacy flat shape — stay None.
    assert _outcome_flag(legacy, 60) is None


def test_outcome_flag_returns_none_for_unknown_shape():
    from modal_workers.feedback.category_accuracy import _outcome_flag

    assert _outcome_flag(None, 30) is None
    assert _outcome_flag({}, 30) is None
    assert _outcome_flag({"verdict": "UNKNOWN"}, 30) is None
    assert _outcome_flag({"horizons": {"30": {"verdict": "PENDING"}}}, 30) is None
