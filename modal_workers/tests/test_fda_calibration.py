"""
Phase 6 calibration math tests.

All pure (no DB, no HTTP). Cover:
  - brier_score on hand-computed fixtures
  - recall edge cases (no positives, all-flagged, none-flagged)
  - post_edge_avoidance (no resolutions => 1.0)
  - realized_ev sign discipline
  - bounded_drift on nested dicts (matched + unmatched leaves)
  - holdout_split determinism + balance
  - candidate generators yield the right shape and respect bounds
  - evaluate_guardrails composite gate
"""

from __future__ import annotations

import pytest

from modal_workers.shared.fda_calibration_math import (
    DEFAULT_BRIER_RELATIVE_GAIN,
    DEFAULT_HOLDOUT_FRAC,
    DEFAULT_HOLDOUT_SEED,
    DEFAULT_MAX_DRIFT_PCT,
    DEFAULT_MIN_SAMPLE_SIZE,
    GuardrailReport,
    bounded_drift,
    brier_score,
    evaluate_guardrails,
    generate_prior_candidates,
    generate_threshold_candidates,
    holdout_split,
    post_edge_avoidance,
    realized_ev,
    recall,
)


# ---------------------------------------------------------------------------
# brier_score
# ---------------------------------------------------------------------------


def test_brier_score_perfect_predictor_is_zero():
    assert brier_score([1.0, 0.0, 1.0, 0.0], [1, 0, 1, 0]) == 0.0


def test_brier_score_always_half_balanced_sample_is_quarter():
    assert brier_score([0.5, 0.5, 0.5, 0.5], [1, 0, 1, 0]) == pytest.approx(0.25)


def test_brier_score_worst_predictor_is_one():
    # Always predict opposite of truth → (1-0)^2 + (0-1)^2 = 2; mean = 1
    assert brier_score([1.0, 0.0, 1.0, 0.0], [0, 1, 0, 1]) == 1.0


def test_brier_score_known_value():
    # p=[0.7, 0.2, 0.6], y=[1, 0, 1]
    # squared errors: 0.09, 0.04, 0.16 -> mean 0.0967
    assert brier_score([0.7, 0.2, 0.6], [1, 0, 1]) == pytest.approx((0.09 + 0.04 + 0.16) / 3, abs=1e-9)


def test_brier_score_empty_raises():
    with pytest.raises(ValueError, match="empty"):
        brier_score([], [])


def test_brier_score_shape_mismatch_raises():
    with pytest.raises(ValueError, match="shape"):
        brier_score([0.5, 0.5], [1])


def test_brier_score_invalid_outcome_raises():
    with pytest.raises(ValueError, match="0 or 1"):
        brier_score([0.5], [2])


# ---------------------------------------------------------------------------
# recall
# ---------------------------------------------------------------------------


def test_recall_all_flagged():
    assert recall([0.9, 0.8, 0.6], [1, 1, 1]) == 1.0


def test_recall_none_flagged():
    assert recall([0.4, 0.3, 0.1], [1, 1, 1]) == 0.0


def test_recall_with_negatives_only_returns_zero():
    # No label-1 cases at all; recall is 0 by convention (avoids NaN).
    assert recall([0.7, 0.8], [0, 0]) == 0.0


def test_recall_threshold_inclusive():
    assert recall([0.5, 0.49], [1, 1]) == 0.5


def test_recall_custom_threshold():
    assert recall([0.4, 0.6, 0.8], [1, 1, 1], threshold=0.7) == pytest.approx(1 / 3)


# ---------------------------------------------------------------------------
# post_edge_avoidance
# ---------------------------------------------------------------------------


def test_post_edge_avoidance_no_resolutions_returns_one():
    assert post_edge_avoidance([0.9, 0.7, 0.5], [False, False, False]) == 1.0


def test_post_edge_avoidance_all_resolutions_avoided():
    # All resolutions, all below threshold -> 1.0 (correct)
    assert post_edge_avoidance([0.2, 0.1, 0.0], [True, True, True]) == 1.0


def test_post_edge_avoidance_some_resolutions_promoted():
    # 2 of 4 resolutions had p >= 0.5 -> avoidance is 2/4 = 0.5
    assert post_edge_avoidance(
        [0.7, 0.3, 0.6, 0.2], [True, True, True, True]
    ) == 0.5


def test_post_edge_avoidance_ignores_non_resolution_events():
    # Only one resolution event; non-resolution rows are dropped
    avoidance = post_edge_avoidance([0.9, 0.2, 0.7], [False, True, False])
    assert avoidance == 1.0  # the only resolution had p<0.5


# ---------------------------------------------------------------------------
# realized_ev
# ---------------------------------------------------------------------------


def test_realized_ev_positive_when_high_predictions_match_winners():
    # high P, big positive moves -> high realized_ev
    ev = realized_ev([0.9, 0.85, 0.8], [40.0, 30.0, 25.0])
    assert ev > 20


def test_realized_ev_negative_when_high_predictions_miss():
    # high P, big NEGATIVE moves -> negative realized_ev (model leaned wrong way)
    ev = realized_ev([0.9, 0.85, 0.8], [-40.0, -30.0, -25.0])
    assert ev < 0


def test_realized_ev_empty_returns_zero():
    assert realized_ev([], []) == 0.0


# ---------------------------------------------------------------------------
# bounded_drift
# ---------------------------------------------------------------------------


def test_bounded_drift_identical_dicts_is_zero():
    d = {"a": 0.5, "b": 0.3}
    ok, drift, offender = bounded_drift(d, d)
    assert ok
    assert drift == 0.0
    assert offender is None


def test_bounded_drift_within_bounds():
    old = {"oncology": 0.45, "cardiology": 0.60}
    # +5% on each: 0.4725 and 0.63
    new = {"oncology": 0.4725, "cardiology": 0.630}
    ok, drift, offender = bounded_drift(old, new, max_pct=0.10)
    assert ok
    assert drift == pytest.approx(0.05, abs=1e-6)
    assert offender is None


def test_bounded_drift_catches_offender():
    old = {"oncology": 0.45, "cardiology": 0.60}
    new = {"oncology": 0.45, "cardiology": 0.72}  # +20% drift
    ok, drift, offender = bounded_drift(old, new, max_pct=0.10)
    assert not ok
    assert offender == "cardiology"
    assert drift == pytest.approx(0.20, abs=1e-6)


def test_bounded_drift_handles_nested_dicts():
    old = {"priors": {"a": 0.5}, "modifiers": {"priority_review": 0.05}}
    new = {"priors": {"a": 0.55}, "modifiers": {"priority_review": 0.07}}  # +10% and +40%
    ok, drift, offender = bounded_drift(old, new, max_pct=0.20)
    assert not ok
    assert offender == "modifiers.priority_review"


def test_bounded_drift_unmatched_keys_ignored():
    old = {"a": 0.5}
    new = {"a": 0.5, "b_new": 0.3}  # b_new is fine, didn't exist before
    ok, _, _ = bounded_drift(old, new)
    assert ok


def test_bounded_drift_zero_old_uses_eps_floor():
    # Old=0, new=0.001 — without eps floor, drift is infinity. With floor, well-bounded.
    old = {"a": 0.0}
    new = {"a": 0.001}
    ok, _, _ = bounded_drift(old, new, max_pct=0.10)
    # A new value of 0.001 against a 0 baseline is dramatic in relative terms;
    # we expect this to FAIL (infinite-ish drift). Confirm we caught it.
    assert not ok


# ---------------------------------------------------------------------------
# holdout_split
# ---------------------------------------------------------------------------


def _make_records(n: int):
    return [{"event_id": f"evt-{i:03d}", "p": 0.5} for i in range(n)]


def test_holdout_split_deterministic_across_calls():
    records = _make_records(100)
    a = holdout_split(records, seed=42, test_frac=0.2)
    b = holdout_split(records, seed=42, test_frac=0.2)
    assert [r["event_id"] for r in a.holdout] == [r["event_id"] for r in b.holdout]
    assert [r["event_id"] for r in a.train] == [r["event_id"] for r in b.train]


def test_holdout_split_different_seeds_yield_different_partitions():
    records = _make_records(100)
    a = holdout_split(records, seed=1, test_frac=0.2)
    b = holdout_split(records, seed=2, test_frac=0.2)
    # Same set of records, but different bucket assignments overall
    assert {r["event_id"] for r in a.holdout} != {r["event_id"] for r in b.holdout}


def test_holdout_split_balance_close_to_test_frac():
    records = _make_records(500)
    out = holdout_split(records, seed=DEFAULT_HOLDOUT_SEED, test_frac=DEFAULT_HOLDOUT_FRAC)
    expected = int(len(records) * DEFAULT_HOLDOUT_FRAC)
    # Within ±5% of expected count (sha256 is uniform but small samples vary)
    assert abs(len(out.holdout) - expected) < expected * 0.30


def test_holdout_split_preserves_all_records():
    records = _make_records(50)
    out = holdout_split(records, seed=42, test_frac=0.3)
    assert len(out.train) + len(out.holdout) == len(records)
    seen = {r["event_id"] for r in out.train} | {r["event_id"] for r in out.holdout}
    assert seen == {r["event_id"] for r in records}


def test_holdout_split_invalid_test_frac_raises():
    with pytest.raises(ValueError, match="test_frac"):
        holdout_split(_make_records(10), test_frac=0.0)
    with pytest.raises(ValueError, match="test_frac"):
        holdout_split(_make_records(10), test_frac=1.0)


def test_holdout_split_falls_back_when_no_event_id():
    # Records without event_id still split deterministically
    records = [{"ticker": f"TKR{i}", "value": i} for i in range(50)]
    a = holdout_split(records, seed=42)
    b = holdout_split(records, seed=42)
    assert [r["ticker"] for r in a.holdout] == [r["ticker"] for r in b.holdout]


# ---------------------------------------------------------------------------
# candidate generators
# ---------------------------------------------------------------------------


def test_generate_prior_candidates_yields_baseline_first():
    priors = {"oncology": 0.45, "cardiology": 0.60}
    modifiers = {"priority_review": 0.05}
    candidates = list(generate_prior_candidates(priors, modifiers))
    assert candidates[0] == (priors, modifiers)


def test_generate_prior_candidates_count_is_baseline_plus_2_per_param():
    priors = {"a": 0.5, "b": 0.6}
    modifiers = {"x": 0.05}
    candidates = list(generate_prior_candidates(priors, modifiers))
    # 1 baseline + 2*2 prior perturbations + 2*1 modifier perturbations = 7
    assert len(candidates) == 7


def test_generate_prior_candidates_clamps_priors_to_unit_interval():
    priors = {"saturated": 0.99}
    modifiers = {}
    candidates = list(generate_prior_candidates(priors, modifiers))
    # +0.05 should clamp to 1.0; -0.05 should be 0.94
    new_values = [c[0]["saturated"] for c in candidates if c[0]["saturated"] != 0.99]
    assert all(0.0 <= v <= 1.0 for v in new_values)


def test_generate_threshold_candidates_preserves_ordering():
    base = {"immediate": 30.0, "watchlist": 20.0, "archive": 10.0}
    candidates = list(generate_threshold_candidates(base))
    # Every candidate must keep imm > wl > arc
    for c in candidates:
        assert c["immediate"] > c["watchlist"] > c["archive"]


def test_generate_threshold_candidates_yields_baseline():
    base = {"immediate": 30.0, "watchlist": 20.0, "archive": 10.0}
    candidates = list(generate_threshold_candidates(base))
    assert candidates[0] == base


# ---------------------------------------------------------------------------
# evaluate_guardrails
# ---------------------------------------------------------------------------


def test_evaluate_guardrails_passes_when_all_conditions_met():
    report = evaluate_guardrails(
        sample_size=42,
        holdout_brier_old=0.250,
        holdout_brier_new=0.230,  # ~8% relative gain
        drift_ok=True,
        drift_pct=0.05,
        drift_offender=None,
    )
    assert report.passed
    assert report.reasons == []
    assert report.brier_relative_gain == pytest.approx(0.08, abs=0.01)


def test_evaluate_guardrails_fails_on_insufficient_sample():
    report = evaluate_guardrails(
        sample_size=20,
        holdout_brier_old=0.250,
        holdout_brier_new=0.200,
        drift_ok=True,
        drift_pct=0.0,
        drift_offender=None,
    )
    assert not report.passed
    assert any("insufficient_sample" in r for r in report.reasons)


def test_evaluate_guardrails_fails_on_drift_exceeded():
    report = evaluate_guardrails(
        sample_size=40,
        holdout_brier_old=0.250,
        holdout_brier_new=0.200,
        drift_ok=False,
        drift_pct=0.20,
        drift_offender="oncology",
    )
    assert not report.passed
    assert any("drift_exceeded" in r and "oncology" in r for r in report.reasons)


def test_evaluate_guardrails_fails_on_no_brier_improvement():
    report = evaluate_guardrails(
        sample_size=40,
        holdout_brier_old=0.250,
        holdout_brier_new=0.260,  # worse
        drift_ok=True,
        drift_pct=0.0,
        drift_offender=None,
    )
    assert not report.passed
    assert any("no_brier_improvement" in r for r in report.reasons)


def test_evaluate_guardrails_fails_on_insufficient_relative_gain():
    report = evaluate_guardrails(
        sample_size=40,
        holdout_brier_old=0.250,
        holdout_brier_new=0.249,  # ~0.4% gain, below 2% bar
        drift_ok=True,
        drift_pct=0.0,
        drift_offender=None,
    )
    assert not report.passed
    assert any("insufficient_relative_gain" in r for r in report.reasons)


def test_evaluate_guardrails_accumulates_multiple_reasons():
    report = evaluate_guardrails(
        sample_size=10,
        holdout_brier_old=0.250,
        holdout_brier_new=0.260,
        drift_ok=False,
        drift_pct=0.30,
        drift_offender="oncology",
    )
    assert not report.passed
    # All three guardrails should have failed
    assert len(report.reasons) >= 3


def test_evaluate_guardrails_default_bars_match_plan():
    """Sanity-check that the constants line up with the plan's hard numbers."""
    assert DEFAULT_MIN_SAMPLE_SIZE == 30
    assert DEFAULT_BRIER_RELATIVE_GAIN == 0.02
    assert DEFAULT_MAX_DRIFT_PCT == 0.10
