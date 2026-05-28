"""v4 Phase 7 wiring tests — confirm daily_feedback_loop invokes
run_daily_snapshot as Step 4, that a Step-4 failure isolates from the
other three steps, and that category_cohort_days flows through the
compute_v3 dispatch path."""

from __future__ import annotations

from typing import Any, Dict

import pytest


# ----------------------------------------------------------------------
# daily_feedback_loop Step 4 happy path + failure isolation
# ----------------------------------------------------------------------


@pytest.fixture
def patched_loop(monkeypatch):
    """Stub steps 1-3 + the SupabaseClient + category snapshot, and return
    a callable that invokes the unwrapped daily_feedback_loop function so
    we can assert on its return dict.

    Modal's @app.function wrapper exposes the original Python callable via
    `.local()` when running outside the cluster; we rely on that.
    """
    # Stub the three previously-existing pipeline steps so we can drive
    # them independently from the category snapshot wiring.
    def fake_drain(batch_size=200):
        class _R:
            status = "outcome_recorded"
        return [_R(), _R()]

    class _MonitorSnap:
        n_resolved_in_window = 12
        spearman_corr = 0.81
        delta_from_prior = 0.03
        rollback_triggered = False
        rollback_reason = None
        active_curve_version_pre = "v1"
        active_curve_version_post = "v1"

    def fake_monitor(window_days=30):
        return _MonitorSnap()

    class _Gate:
        passed = True
        gate_reason = "ok"
        n_eval_cases = 250
        brier_delta = 0.02
        paired_bootstrap_p = 0.01
        ranking_auc_delta = 0.06
        max_single_asset_contribution_pct = 3.0

    class _Refit:
        n_training = 250
        new_curve_version = "v2"
        activated = True
        gate = _Gate()

    def fake_refit(min_n=200, bootstrap_resamples=10000):
        return _Refit()

    import modal_workers.shared.post_mortem_runner as pmr
    import modal_workers.scripts.rollback_monitor as rm
    import modal_workers.scripts.nightly_calibration_refit as nrf
    monkeypatch.setattr(pmr, "drain_resolved_queue", fake_drain)
    monkeypatch.setattr(rm, "check_drift_and_maybe_rollback", fake_monitor)
    monkeypatch.setattr(nrf, "run_nightly_refit", fake_refit)

    # Patch SupabaseClient so we don't try to read env vars / hit network.
    class _FakeSB:
        def __init__(self, *a, **kw):
            pass

    import modal_workers.shared.supabase_client as sbmod
    monkeypatch.setattr(sbmod, "SupabaseClient", _FakeSB)

    from modal_workers import feedback_loop_app
    return feedback_loop_app


def test_step_4_invokes_run_daily_snapshot(patched_loop, monkeypatch):
    """Happy path: Step 4 calls run_daily_snapshot with SupabaseClient +
    cohort_days, and the dict it returns is surfaced as
    out['category_snapshot']."""
    captured: Dict[str, Any] = {}

    def fake_snapshot(sb, *, cohort_days=90, **_):
        captured["sb"] = sb
        captured["cohort_days"] = cohort_days
        return {
            "snapshot_date": "2026-05-28",
            "cohort_window_start": "2026-02-27",
            "cohort_window_end": "2026-05-28",
            "metric_cells_total": 4,
            "rows_persisted": 4,
            "input_rows": 17,
        }

    import modal_workers.feedback.category_accuracy as ca
    monkeypatch.setattr(ca, "run_daily_snapshot", fake_snapshot)

    out = patched_loop.daily_feedback_loop.local(category_cohort_days=42)
    assert "category_snapshot" in out
    snap = out["category_snapshot"]
    assert snap["metric_cells_total"] == 4
    assert snap["rows_persisted"] == 4
    # Cohort knob flows through.
    assert captured["cohort_days"] == 42
    # All four steps populated; Step 4 didn't blow away the others.
    assert "drain" in out and "by_status" in out["drain"]
    assert "monitor" in out and out["monitor"]["spearman_corr"] == 0.81
    assert "refit" in out and out["refit"]["activated"] is True


def test_step_4_failure_does_not_break_earlier_steps(patched_loop, monkeypatch):
    """If category_accuracy raises, Step 4 logs the error into
    out['category_snapshot']['error'] but Steps 1-3 still report normally
    (the chain is failure-isolated by design — same pattern as the existing
    drain/monitor/refit try-except blocks)."""

    def boom(*a, **kw):
        raise RuntimeError("postgrest 500: feedback_category_metrics not reachable")

    import modal_workers.feedback.category_accuracy as ca
    monkeypatch.setattr(ca, "run_daily_snapshot", boom)

    out = patched_loop.daily_feedback_loop.local()
    assert "category_snapshot" in out
    assert "error" in out["category_snapshot"]
    assert "RuntimeError" in out["category_snapshot"]["error"]
    # Earlier steps remained intact.
    assert out["drain"]["drained"] == 2
    assert out["monitor"]["rollback_triggered"] is False
    assert out["refit"]["activated"] is True


# ----------------------------------------------------------------------
# compute_v3 dispatch passes category_cohort_days through to spawn
# ----------------------------------------------------------------------


def test_feedback_loop_kickoff_passes_category_cohort_days(monkeypatch):
    """category_cohort_days must reach the spawned daily_feedback_loop.
    Unknown keys are still filtered out by the dispatch allowlist."""
    spawned: Dict[str, Any] = {}

    class _Handle:
        object_id = "fc-cat-abc"

    class _FakeFn:
        def spawn(self, **kwargs):
            spawned["kwargs"] = kwargs
            return _Handle()

    import modal as _modal
    monkeypatch.setattr(_modal.Function, "from_name",
                        staticmethod(lambda *a, **kw: _FakeFn()))
    from modal_workers.orchestrator_app import _dispatch_compute_v3_action

    out = _dispatch_compute_v3_action("feedback_loop_kickoff", {
        "category_cohort_days": 30,
        "drain_batch_size": 99,
        "ignored_extra_key": "should_not_pass_through",
    })
    assert out["spawned"] is True
    assert spawned["kwargs"] == {
        "drain_batch_size": 99,
        "category_cohort_days": 30,
    }
    assert "ignored_extra_key" not in spawned["kwargs"]
