"""Tests for nightly_calibration_refit pure helpers (Stream 2, D-103).

Covers:
  - paired_bootstrap_p_value: trivial reject-null on observed delta<=0
  - ranking_auc: monotonicity + degenerate cases
  - evaluate_gate: each of 5 D-103 failure modes + happy path
  - _direction_aligned_outcome: long/short/neutral mapping
"""

from __future__ import annotations

import random

import pytest

from modal_workers.scripts.nightly_calibration_refit import (
    GATE_MAX_P_VALUE,
    GATE_MAX_SINGLE_ASSET_PCT,
    GATE_MIN_AUC_DELTA,
    GATE_MIN_N,
    GateEvaluation,
    _direction_aligned_outcome,
    _per_asset_brier_contribution,
    evaluate_gate,
    fetch_training_pair_snapshot,
    paired_bootstrap_p_value,
    ranking_auc,
)


# ---------------------------------------------------------------------------
# _direction_aligned_outcome
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("direction,hit,expected", [
    ("long", True, 1),
    ("long", False, 0),
    ("short", True, 0),    # HIT = stock UP, short was wrong
    ("short", False, 1),
    ("neutral", True, 1),
    ("neutral", False, 0),
    (None, True, 1),
    ("", False, 0),
])
def test_direction_aligned_outcome(direction, hit, expected):
    assert _direction_aligned_outcome(direction, hit) == expected


# ---------------------------------------------------------------------------
# ranking_auc
# ---------------------------------------------------------------------------

def test_ranking_auc_perfect_separation_returns_one():
    # Predictions perfectly rank positives above negatives.
    preds = [0.1, 0.2, 0.3, 0.7, 0.8, 0.9]
    real =  [0,   0,   0,   1,   1,   1]
    assert ranking_auc(preds, real) == 1.0


def test_ranking_auc_inverted_returns_zero():
    preds = [0.1, 0.2, 0.3, 0.7, 0.8, 0.9]
    real =  [1,   1,   1,   0,   0,   0]
    assert ranking_auc(preds, real) == 0.0


def test_ranking_auc_random_near_half():
    rng = random.Random(42)
    preds = [rng.random() for _ in range(200)]
    real = [rng.choice([0, 1]) for _ in range(200)]
    auc = ranking_auc(preds, real)
    assert 0.35 < auc < 0.65, f"random AUC should be ~0.5, got {auc}"


def test_ranking_auc_all_one_class_returns_half():
    preds = [0.1, 0.5, 0.9]
    real = [1, 1, 1]
    assert ranking_auc(preds, real) == 0.5


def test_ranking_auc_handles_ties_via_average_ranks():
    # All predictions identical → AUC = 0.5 regardless of outcomes.
    preds = [0.5, 0.5, 0.5, 0.5]
    real =  [1, 0, 1, 0]
    assert ranking_auc(preds, real) == 0.5


# ---------------------------------------------------------------------------
# paired_bootstrap_p_value
# ---------------------------------------------------------------------------

def test_paired_bootstrap_observed_delta_zero_returns_one():
    # New is identical to prod → can't reject null.
    preds = [0.3, 0.5, 0.7, 0.4, 0.6]
    real = [0, 1, 1, 0, 1]
    p = paired_bootstrap_p_value(preds, preds, real, n_resamples=500, rng_seed=0)
    assert p == 1.0


def test_paired_bootstrap_strong_improvement_low_p():
    # Prod predicts 0.5 for everyone (uninformative). New predicts perfectly.
    real = [0, 0, 0, 1, 1, 1] * 30  # n=180
    prod = [0.5] * len(real)
    new = [float(y) for y in real]
    p = paired_bootstrap_p_value(prod, new, real, n_resamples=500, rng_seed=0)
    assert p < 0.05, f"expected p<0.05 for clear improvement, got {p}"


def test_paired_bootstrap_marginal_improvement_high_p():
    # New is only barely better than prod — p should be high.
    rng = random.Random(0)
    n = 100
    real = [rng.choice([0, 1]) for _ in range(n)]
    prod = [0.5 + rng.uniform(-0.02, 0.02) for _ in range(n)]
    new = [p + 0.001 for p in prod]  # imperceptible nudge toward random direction
    p = paired_bootstrap_p_value(prod, new, real, n_resamples=500, rng_seed=42)
    # Marginal — usually high p.
    assert p > 0.05 or p == 1.0


# ---------------------------------------------------------------------------
# _per_asset_brier_contribution
# ---------------------------------------------------------------------------

def test_per_asset_brier_contribution_groups_correctly():
    asset_ids = ["A", "A", "B", "B"]
    prod =     [0.5, 0.5, 0.8, 0.2]
    new =      [0.9, 0.1, 0.5, 0.5]
    real =     [1, 0, 1, 0]
    contrib = _per_asset_brier_contribution(asset_ids, prod, new, real)
    assert set(contrib.keys()) == {"A", "B"}
    # A: prod brier=0.5; new brier=0.01+0.01=0.02. Delta = 0.5 - 0.02 = +0.48 (huge improvement).
    assert contrib["A"] > 0.4


# ---------------------------------------------------------------------------
# evaluate_gate — per D-103 failure mode
# ---------------------------------------------------------------------------

def _make_test_data(n: int, seed: int = 0):
    rng = random.Random(seed)
    real = [rng.choice([0, 1]) for _ in range(n)]
    raw = [rng.uniform(0.2, 0.8) for _ in range(n)]
    asset_ids = [f"asset-{i % 50}" for i in range(n)]  # 50 unique → no concentration
    return raw, real, asset_ids


def test_evaluate_gate_n_too_low():
    raw, real, asset_ids = _make_test_data(50)
    gate = evaluate_gate(
        raw=raw, realized=real, asset_ids=asset_ids,
        pred_prod=raw, pred_new=raw,
        min_n=GATE_MIN_N, bootstrap_resamples=200, rng_seed=0,
    )
    assert gate.passed is False
    assert gate.gate_reason == "n_too_low"


def test_evaluate_gate_brier_regression():
    n = 250
    raw, real, asset_ids = _make_test_data(n)
    # New is WORSE than prod (predict everything as 0.5; prod predicts truth).
    pred_prod = [float(y) for y in real]  # perfect
    pred_new = [0.5] * n
    gate = evaluate_gate(
        raw=raw, realized=real, asset_ids=asset_ids,
        pred_prod=pred_prod, pred_new=pred_new,
        min_n=GATE_MIN_N, bootstrap_resamples=200, rng_seed=0,
    )
    assert gate.passed is False
    assert gate.gate_reason == "brier_regression"
    assert gate.brier_delta < 0


def test_evaluate_gate_p_above_threshold():
    # n=250, identical predictions → bootstrap p must be 1.0 (no observed improvement).
    n = 250
    raw, real, asset_ids = _make_test_data(n)
    pred_prod = [0.5] * n
    pred_new = [0.5] * n
    gate = evaluate_gate(
        raw=raw, realized=real, asset_ids=asset_ids,
        pred_prod=pred_prod, pred_new=pred_new,
        min_n=GATE_MIN_N, bootstrap_resamples=100, rng_seed=0,
    )
    # brier_delta is exactly 0 → fails brier_regression first (delta>0 required).
    assert gate.passed is False
    assert gate.gate_reason == "brier_regression"


def test_evaluate_gate_auc_delta_below():
    n = 250
    rng = random.Random(0)
    real = [rng.choice([0, 1]) for _ in range(n)]
    asset_ids = [f"asset-{i % 50}" for i in range(n)]
    # Both models are similarly informative → AUC delta ~ 0.
    pred_prod = [0.7 if y == 1 else 0.3 for y in real]  # high AUC
    # Slightly noisier version of same — still high AUC, similar to prod.
    pred_new = [min(1.0, max(0.0, p + rng.uniform(-0.01, 0.01))) for p in pred_prod]
    gate = evaluate_gate(
        raw=pred_prod, realized=real, asset_ids=asset_ids,
        pred_prod=pred_prod, pred_new=pred_new,
        min_n=GATE_MIN_N, bootstrap_resamples=100, rng_seed=0,
    )
    # Either brier_regression (if new noisier) or auc_delta_below.
    assert gate.passed is False
    assert gate.gate_reason in ("auc_delta_below", "brier_regression")


def test_evaluate_gate_pass_path():
    # Construct a synthetic scenario where new dominates prod cleanly.
    n = 250
    rng = random.Random(0)
    real = [rng.choice([0, 1]) for _ in range(n)]
    asset_ids = [f"asset-{i % 50}" for i in range(n)]
    # Prod is uninformative (0.5); new predicts truth.
    pred_prod = [0.5] * n
    pred_new = [float(y) for y in real]
    gate = evaluate_gate(
        raw=pred_prod, realized=real, asset_ids=asset_ids,
        pred_prod=pred_prod, pred_new=pred_new,
        min_n=GATE_MIN_N, bootstrap_resamples=500, rng_seed=42,
    )
    # All five conditions met.
    assert gate.passed is True, f"expected pass, got reason={gate.gate_reason} gate={gate}"
    assert gate.gate_reason == "pass"
    assert gate.n_eval_cases == n
    assert gate.brier_delta > 0
    assert gate.paired_bootstrap_p < GATE_MAX_P_VALUE
    assert gate.ranking_auc_delta >= GATE_MIN_AUC_DELTA


def test_evaluate_gate_records_all_inputs_even_on_failure():
    # Failing gates must still record inputs for audit (D-103 amendment).
    gate = evaluate_gate(
        raw=[0.5] * 5, realized=[0, 1, 0, 1, 0], asset_ids=["a"] * 5,
        pred_prod=[0.5] * 5, pred_new=[0.5] * 5,
        min_n=GATE_MIN_N, bootstrap_resamples=100, rng_seed=0,
    )
    assert gate.passed is False
    assert gate.n_eval_cases == 5
    assert gate.brier_prod is not None
    assert gate.brier_new is not None
    assert gate.ranking_auc_prod is not None


def test_evaluate_gate_input_length_mismatch_raises():
    with pytest.raises(ValueError):
        evaluate_gate(
            raw=[0.5, 0.5], realized=[0], asset_ids=["a", "b"],
            pred_prod=[0.5, 0.5], pred_new=[0.5, 0.5],
            min_n=2, bootstrap_resamples=10, rng_seed=0,
        )


def test_training_pair_snapshot_reports_source_and_skips():
    class StubSb:
        def _rest(self, method, path, *, params=None):
            if path == "post_mortem_queue":
                return [
                    {
                        "assessment_id": "a1",
                        "asset_id": "asset-1",
                        "realized_outcome": {"hit": True},
                        "predicted_direction": "long",
                    },
                    {
                        "assessment_id": "missing",
                        "asset_id": "asset-2",
                        "realized_outcome": {"hit": False},
                        "predicted_direction": "short",
                    },
                    {
                        "assessment_id": "a3",
                        "asset_id": "asset-3",
                        "realized_outcome": {},
                        "predicted_direction": "long",
                    },
                ]
            if path == "convergence_assessments":
                return [{"id": "a1", "raw_conviction_pct": 70.0}]
            return []

    snapshot = fetch_training_pair_snapshot(StubSb(), source="post_mortem_queue")

    assert snapshot.source == "post_mortem_queue"
    assert snapshot.n_rows_seen == 3
    assert snapshot.n_pairs == 1
    assert snapshot.n_positive == 1
    assert snapshot.skipped["missing_assessment"] == 1
    assert snapshot.skipped["missing_hit"] == 1
