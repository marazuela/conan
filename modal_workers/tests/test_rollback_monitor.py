"""Tests for rollback_monitor pure helpers (Stream 2, D-104).

Covers:
  - spearman_corr: monotonic mapping → +1, anti-monotonic → -1, random → ~0
  - _ranks: tie-breaking via average ranks
  - _classify_drift: each branch (low / dropped / no_drift)
"""

from __future__ import annotations

import random

import pytest

from modal_workers.scripts.rollback_monitor import (
    CORRELATION_DROP_THRESHOLD,
    LOW_CORRELATION_THRESHOLD,
    _classify_drift,
    _ranks,
    spearman_corr,
)


# ---------------------------------------------------------------------------
# spearman_corr
# ---------------------------------------------------------------------------

def test_spearman_perfect_monotonic_positive():
    xs = [1, 2, 3, 4, 5]
    ys = [10, 20, 30, 40, 50]
    assert spearman_corr(xs, ys) == pytest.approx(1.0)


def test_spearman_perfect_monotonic_negative():
    xs = [1, 2, 3, 4, 5]
    ys = [50, 40, 30, 20, 10]
    assert spearman_corr(xs, ys) == pytest.approx(-1.0)


def test_spearman_random_is_near_zero():
    rng = random.Random(7)
    xs = [rng.random() for _ in range(500)]
    ys = [rng.random() for _ in range(500)]
    corr = spearman_corr(xs, ys)
    assert -0.15 < corr < 0.15, f"random Spearman should be ~0, got {corr}"


def test_spearman_handles_ties():
    # Two ties at ranks 2-3 in xs and 4-5 in ys.
    xs = [1.0, 2.0, 2.0, 3.0, 4.0]
    ys = [10.0, 20.0, 30.0, 40.0, 40.0]
    corr = spearman_corr(xs, ys)
    # Monotone-ish but with ties — should be high but not 1.0.
    assert 0.85 <= corr <= 1.0


def test_spearman_n_below_two_returns_zero():
    assert spearman_corr([], []) == 0.0
    assert spearman_corr([1.0], [2.0]) == 0.0


def test_spearman_length_mismatch_returns_zero():
    assert spearman_corr([1, 2, 3], [1, 2]) == 0.0


def test_spearman_constant_input_returns_zero():
    # Zero variance → undefined; we return 0 as a safe sentinel.
    assert spearman_corr([5, 5, 5, 5], [1, 2, 3, 4]) == 0.0
    assert spearman_corr([1, 2, 3, 4], [5, 5, 5, 5]) == 0.0


# ---------------------------------------------------------------------------
# _ranks
# ---------------------------------------------------------------------------

def test_ranks_assigns_average_rank_for_ties():
    # values [10, 20, 20, 30] → ranks [1, 2.5, 2.5, 4]
    ranks = _ranks([10, 20, 20, 30])
    assert ranks == [1.0, 2.5, 2.5, 4.0]


def test_ranks_strict_monotonic_returns_1_to_n():
    ranks = _ranks([5, 10, 15, 20])
    assert ranks == [1.0, 2.0, 3.0, 4.0]


def test_ranks_unsorted_input_returns_correct_per_position_ranks():
    # values [30, 10, 20] → sorted indices [10:1, 20:2, 30:3] → original ranks [3, 1, 2]
    ranks = _ranks([30, 10, 20])
    assert ranks == [3.0, 1.0, 2.0]


def test_ranks_all_equal_returns_average_of_all_positions():
    ranks = _ranks([7, 7, 7, 7])
    # Average of 1..4 = 2.5
    assert ranks == [2.5, 2.5, 2.5, 2.5]


# ---------------------------------------------------------------------------
# _classify_drift
# ---------------------------------------------------------------------------

def test_classify_drift_low_correlation_fires():
    # corr below LOW_CORRELATION_THRESHOLD (0.20).
    reason = _classify_drift(corr=0.10, delta=0.0)
    assert reason == "low_correlation"


def test_classify_drift_correlation_drop_fires():
    # corr above floor but a sharp drop from prior.
    reason = _classify_drift(corr=0.50, delta=-CORRELATION_DROP_THRESHOLD - 0.01)
    assert reason == "correlation_drop"


def test_classify_drift_low_corr_dominates_when_both_present():
    # When both fire, low_correlation surfaces (more severe — absolute floor breach).
    reason = _classify_drift(corr=0.05, delta=-0.30)
    assert reason == "low_correlation"


def test_classify_drift_no_drift_when_corr_high_and_stable():
    reason = _classify_drift(corr=0.45, delta=0.02)
    assert reason == "no_drift"


def test_classify_drift_no_drift_when_delta_unknown():
    # First-ever monitor pass: delta=None means we have no baseline.
    reason = _classify_drift(corr=0.45, delta=None)
    assert reason == "no_drift"


def test_classify_drift_correlation_drop_at_exact_threshold():
    # delta exactly at -0.15 threshold should fire (≤ check).
    reason = _classify_drift(corr=0.40, delta=-CORRELATION_DROP_THRESHOLD)
    assert reason == "correlation_drop"


def test_classify_drift_low_correlation_at_exact_threshold_does_NOT_fire():
    # corr exactly at 0.20 is not below the threshold (strict <).
    reason = _classify_drift(corr=LOW_CORRELATION_THRESHOLD, delta=0.0)
    assert reason == "no_drift"
