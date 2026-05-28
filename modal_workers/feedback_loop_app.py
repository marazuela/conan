"""v3 closed feedback loop — Modal app entry point (Stream 2).

Single scheduled chain (`daily_feedback_loop`) that runs four steps in
order, daily at 02:00 UTC:

  1. post_mortem_drain     — drain post_mortem_queue
  2. rollback_monitor      — D-104 Spearman drift check
  3. calibration_refit     — D-103 paired-bootstrap isotonic refit
  4. category_snapshot     — v4 Phase 7 per-(profile,signal_category,horizon)
                              accuracy aggregator → feedback_category_metrics

Why one cron not three: Modal free tier caps cron jobs at 5; v2's
conan-v2 app already uses all 5. Chaining inside one function preserves
the intended ordering (drain → monitor → refit) without competing for
cron slots. If/when the workspace upgrades, splitting back into 3
@modal.function(@modal.Cron) decorators is trivial.

Kept in its OWN Modal app (`conan-v3-feedback-loop`) so changes to the
feedback loop don't redeploy the orchestrator app.

Each step is a thin wrapper around the corresponding library entry
point; logic + tests live in modal_workers/shared/post_mortem_runner.py
and modal_workers/scripts/{nightly_calibration_refit,rollback_monitor}.py.

Image pins anthropic >= 0.50 + yfinance (D-116 dependency). Anthropic key
lives in scanner-secrets (the v2 secret bundle); when Stream 3 ships the
dedicated `anthropic-orchestrator` secret, swap by updating the
`secrets=[...]` list and redeploying.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

import modal

app = modal.App("conan-v3-feedback-loop")

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install(
        "requests>=2.31",
        "anthropic>=0.50",
        "yfinance>=0.2,<0.3",
        "beautifulsoup4>=4.12,<5",
    )
    .add_local_python_source("modal_workers")
)

supabase_secrets = modal.Secret.from_name("supabase-secrets")
# scanner-secrets bundles third-party API keys (Anthropic for Haiku post-mortem
# text, Polygon for D-116 forward-return labeling). Stream 3 will eventually
# move to a dedicated `anthropic-orchestrator` secret; until that's created,
# scanner-secrets is the secret already in use by v2 thesis-writing functions.
scanner_secrets = modal.Secret.from_name("scanner-secrets")


# ============================================================================
# Daily chain: drain → monitor → refit
#
# Deploys as an on-demand callable (no @modal.Cron) because the Modal
# free-tier limit is 5 cron jobs and conan-v2 already uses all 5. Triggered
# externally — by pg_cron in Supabase via _conan_modal_post helper, by an
# operator one-off, or by upgrading the Modal plan and re-adding the
# `schedule=modal.Cron("0 2 * * *")` argument.
# ============================================================================

@app.function(
    image=image,
    timeout=7200,
    secrets=[supabase_secrets, scanner_secrets],
)
def daily_feedback_loop(
    drain_batch_size: int = 200,
    monitor_window_days: int = 30,
    refit_min_n: int = 200,
    refit_bootstrap_resamples: int = 10000,
    category_cohort_days: int = 90,
) -> Dict[str, Any]:
    """Daily chain — runs all four feedback-loop steps in order.

    Steps:
      1. post_mortem_drain      — drain post_mortem_queue rows past window_end
      2. rollback_monitor       — D-104 Spearman drift check + auto-rollback
      3. calibration_refit      — D-103 isotonic refit + paired-bootstrap gate
      4. category_snapshot      — v4 Phase 7 per-category accuracy snapshot

    Each step's failure is caught and recorded; later steps still attempt to
    run. Returns a structured summary suitable for Modal logging + ad-hoc
    operator inspection.
    """
    from modal_workers.shared.post_mortem_runner import drain_resolved_queue
    from modal_workers.scripts.rollback_monitor import check_drift_and_maybe_rollback
    from modal_workers.scripts.nightly_calibration_refit import run_nightly_refit
    from modal_workers.feedback.category_accuracy import run_daily_snapshot as run_category_snapshot
    from modal_workers.shared.supabase_client import SupabaseClient

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    out: Dict[str, Any] = {}

    # Step 1: drain.
    try:
        results = drain_resolved_queue(batch_size=drain_batch_size)
        by_status: Dict[str, int] = {}
        for r in results:
            by_status[r.status] = by_status.get(r.status, 0) + 1
        out["drain"] = {"drained": len(results), "by_status": by_status}
    except Exception as exc:  # noqa: BLE001 — chain must keep going
        logging.exception("daily_feedback_loop: drain failed")
        out["drain"] = {"error": f"{type(exc).__name__}:{exc}"}

    # Step 2: rollback monitor.
    try:
        snapshot = check_drift_and_maybe_rollback(window_days=monitor_window_days)
        out["monitor"] = {
            "n_resolved_in_window": snapshot.n_resolved_in_window,
            "spearman_corr": snapshot.spearman_corr,
            "delta_from_prior": snapshot.delta_from_prior,
            "rollback_triggered": snapshot.rollback_triggered,
            "rollback_reason": snapshot.rollback_reason,
            "active_curve_version_pre": snapshot.active_curve_version_pre,
            "active_curve_version_post": snapshot.active_curve_version_post,
        }
    except Exception as exc:  # noqa: BLE001
        logging.exception("daily_feedback_loop: monitor failed")
        out["monitor"] = {"error": f"{type(exc).__name__}:{exc}"}

    # Step 3: calibration refit.
    try:
        result = run_nightly_refit(min_n=refit_min_n,
                                   bootstrap_resamples=refit_bootstrap_resamples)
        out["refit"] = {
            "n_training": result.n_training,
            "new_curve_version": result.new_curve_version,
            "activated": result.activated,
            "gate": {
                "passed": result.gate.passed,
                "reason": result.gate.gate_reason,
                "n": result.gate.n_eval_cases,
                "brier_delta": result.gate.brier_delta,
                "paired_bootstrap_p": result.gate.paired_bootstrap_p,
                "auc_delta": result.gate.ranking_auc_delta,
                "max_single_asset_pct": result.gate.max_single_asset_contribution_pct,
            },
        }
    except Exception as exc:  # noqa: BLE001
        logging.exception("daily_feedback_loop: refit failed")
        out["refit"] = {"error": f"{type(exc).__name__}:{exc}"}

    # Step 4: v4 Phase 7 per-category accuracy snapshot. Reads resolved
    # post_mortem_queue rows (joined with convergence_assessments for
    # signal_category) over a trailing cohort window and persists one row per
    # (profile, signal_category, horizon_days) cell to
    # feedback_category_metrics. The weekly feedback_retrospective Cowork
    # skill reads from that table to propose rubric weight adjustments.
    try:
        sb = SupabaseClient()
        snap = run_category_snapshot(sb, cohort_days=category_cohort_days)
        out["category_snapshot"] = snap
    except Exception as exc:  # noqa: BLE001
        logging.exception("daily_feedback_loop: category_snapshot failed")
        out["category_snapshot"] = {"error": f"{type(exc).__name__}:{exc}"}

    return out


# ============================================================================
# Manual triggers — operator-callable to dry-run any of the three
# ============================================================================

@app.function(image=image, timeout=600,
              secrets=[supabase_secrets, scanner_secrets])
def post_mortem_drain_dry_run(batch_size: int = 50) -> Dict[str, Any]:
    """Dry-run the drainer without writing any DB rows. Useful for verifying
    the queue contents and outcome resolution paths against live state."""
    from modal_workers.shared.post_mortem_runner import drain_resolved_queue

    results = drain_resolved_queue(batch_size=batch_size, dry_run=True,
                                   skip_text_generation=True)
    by_status: Dict[str, int] = {}
    payload = []
    for r in results:
        by_status[r.status] = by_status.get(r.status, 0) + 1
        payload.append({
            "queue_id": r.queue_id,
            "assessment_id": r.assessment_id,
            "asset_id": r.asset_id,
            "status": r.status,
            "skipped_reason": r.skipped_reason,
            "prediction_error": r.prediction_error,
            "reference_class": r.reference_class,
        })
    return {"drained": len(results), "by_status": by_status, "rows": payload}


@app.function(image=image, timeout=300,
              secrets=[supabase_secrets, scanner_secrets])
def rollback_monitor_dry_run(window_days: int = 30) -> Dict[str, Any]:
    from modal_workers.scripts.rollback_monitor import check_drift_and_maybe_rollback

    snapshot = check_drift_and_maybe_rollback(window_days=window_days, dry_run=True)
    return {
        "n_resolved_in_window": snapshot.n_resolved_in_window,
        "spearman_corr": snapshot.spearman_corr,
        "delta_from_prior": snapshot.delta_from_prior,
        "would_rollback": snapshot.rollback_triggered,
        "reason": snapshot.rollback_reason,
    }


@app.function(image=image, timeout=600,
              secrets=[supabase_secrets, scanner_secrets])
def category_snapshot_manual(cohort_days: int = 90) -> Dict[str, Any]:
    """Operator-callable manual fire of the v4 Phase 7 category accuracy
    snapshot. Writes one row per (profile, signal_category, horizon_days)
    cell to feedback_category_metrics for the trailing cohort window.

    Idempotent — re-running for the same snapshot_date overwrites prior
    rows via the (snapshot_date, profile, signal_category, horizon_days)
    UNIQUE constraint."""
    from modal_workers.feedback.category_accuracy import run_daily_snapshot
    from modal_workers.shared.supabase_client import SupabaseClient

    sb = SupabaseClient()
    return run_daily_snapshot(sb, cohort_days=cohort_days)
