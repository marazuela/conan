"""Eval-harness metrics — Brier, calibration curve, ranking AUC.

Wraps the canonical Brier in modal_workers.shared.fda_calibration_math; adds
calibration-curve binning + ranking AUC for orchestrator-conviction evaluation.

All functions are pure (no I/O); callers pass prediction/outcome sequences pulled
from the eval_harness table or from a replay run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List, Sequence, Tuple

from modal_workers.shared.fda_calibration_math import brier_score


# Default calibration buckets covering the orchestrator's conviction range (0-100).
# Tuned to reflect the bands used by downstream alerting:
#   < 25  → discard (skip — bucket too noisy to track)
#   25-50 → archive
#   50-65 → watchlist
#   65-80 → immediate-low
#   80-100 → immediate-high
DEFAULT_CALIBRATION_BUCKETS: Tuple[Tuple[float, float], ...] = (
    (25.0, 35.0),
    (35.0, 50.0),
    (50.0, 65.0),
    (65.0, 80.0),
    (80.0, 95.0),
    (95.0, 100.001),  # inclusive upper bound for 100% conviction
)


@dataclass
class CalibrationBucket:
    lower: float
    upper: float
    n: int
    predicted_mean: float          # mean conviction_pct of predictions in bucket
    actual_rate: float             # realized positive-outcome fraction
    deviation: float = field(init=False)

    def __post_init__(self) -> None:
        # +ve deviation = over-confident (predicted > actual)
        # -ve deviation = under-confident
        self.deviation = self.predicted_mean - (self.actual_rate * 100.0)


@dataclass
class CalibrationCurve:
    """Result of bucketing (prediction, outcome) pairs into conviction bands.
    Used to fit isotonic regression + diagnose miscalibration ranges."""
    buckets: List[CalibrationBucket]
    n_total: int
    overall_brier: float


def calibration_curve(
    predictions: Sequence[float],
    outcomes: Sequence[int],
    *,
    buckets: Sequence[Tuple[float, float]] = DEFAULT_CALIBRATION_BUCKETS,
) -> CalibrationCurve:
    """Bucket predictions by conviction range and compute realized rate per bucket.

    predictions: conviction_pct values 0..100 (orchestrator output)
    outcomes:    1 if direction was correct (long realized positive move, short
                 realized negative move, etc.), 0 otherwise

    Returns a CalibrationCurve with deviation per bucket and overall Brier."""
    if len(predictions) != len(outcomes):
        raise ValueError(
            f"predictions ({len(predictions)}) and outcomes ({len(outcomes)}) "
            "must have the same length")

    out_buckets: List[CalibrationBucket] = []
    for lower, upper in buckets:
        in_bucket = [
            (p, o) for p, o in zip(predictions, outcomes)
            if lower <= p < upper
        ]
        if not in_bucket:
            continue
        preds = [p for p, _ in in_bucket]
        outs = [o for _, o in in_bucket]
        out_buckets.append(CalibrationBucket(
            lower=lower,
            upper=upper,
            n=len(in_bucket),
            predicted_mean=sum(preds) / len(preds),
            actual_rate=sum(outs) / len(outs),
        ))

    # Overall Brier uses the canonical implementation (predictions normalized to 0-1).
    norm_predictions = [p / 100.0 for p in predictions]
    overall = brier_score(norm_predictions, list(outcomes))

    return CalibrationCurve(
        buckets=out_buckets,
        n_total=len(predictions),
        overall_brier=overall,
    )


def ranking_auc(
    predictions: Sequence[float],
    outcomes: Sequence[int],
) -> float:
    """ROC AUC computed via the Mann–Whitney U statistic. No sklearn dep — we
    compute the rank sum directly so the eval harness can run in any modal
    container without extra packages.

    Returns 0.5 when no signal (random ordering), 1.0 for perfect ranking."""
    if len(predictions) != len(outcomes):
        raise ValueError(
            "predictions and outcomes must have the same length")

    pairs = sorted(zip(predictions, outcomes), key=lambda x: x[0])
    n_pos = sum(o for _, o in pairs)
    n_neg = len(pairs) - n_pos
    if n_pos == 0 or n_neg == 0:
        return 0.5  # undefined; return neutral

    # Sum of ranks of positive outcomes (1-indexed; ties broken by averaging
    # would be more correct, but with conviction_pct rarely tying we accept
    # the small bias).
    rank_sum_pos = 0.0
    for rank, (_, outcome) in enumerate(pairs, start=1):
        if outcome == 1:
            rank_sum_pos += rank

    # AUC = (rank_sum_pos - n_pos*(n_pos+1)/2) / (n_pos * n_neg)
    auc = (rank_sum_pos - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)
    return auc


@dataclass
class EvalRunResult:
    """Aggregate result for one eval-harness replay (one orchestrator_version /
    prompt_hash combination over the full held-out set). Persisted to eval_runs."""
    orchestrator_version: str
    prompt_hash: str
    brier_score: float
    calibration: CalibrationCurve
    ranking_auc: float
    n_assessments: int
    per_assessment_results: List[dict]   # raw rows for audit / debugging
    passed_gate: bool                    # True if non-regressive vs reference

    def as_eval_runs_row(self) -> dict:
        """Serialize to the eval_runs table column shape."""
        return {
            "orchestrator_version": self.orchestrator_version,
            "prompt_hash": self.prompt_hash,
            "brier_score": self.brier_score,
            "calibration_curve": [
                {
                    "lower": b.lower,
                    "upper": b.upper,
                    "n": b.n,
                    "predicted_mean": b.predicted_mean,
                    "actual_rate": b.actual_rate,
                    "deviation": b.deviation,
                }
                for b in self.calibration.buckets
            ],
            "ranking_auc": self.ranking_auc,
            "per_assessment_results": self.per_assessment_results,
            "passed_gate": self.passed_gate,
        }


def aggregate(
    orchestrator_version: str,
    prompt_hash: str,
    per_assessment_results: Iterable[dict],
    *,
    reference_brier: float | None = None,
    brier_regression_tolerance: float = 0.005,
) -> EvalRunResult:
    """Aggregate per-assessment results from a replay into an EvalRunResult.

    Each per_assessment_results dict should have at least:
      - conviction_pct (0-100)
      - direction_correct (1 or 0)

    If reference_brier is provided, passed_gate is True when Brier improves OR
    regresses by no more than `brier_regression_tolerance`. Otherwise passed_gate
    defaults to True (no regression check)."""
    rows = list(per_assessment_results)
    if not rows:
        raise ValueError("aggregate() received no per-assessment results")

    predictions = [r["conviction_pct"] for r in rows]
    outcomes = [r["direction_correct"] for r in rows]

    cal = calibration_curve(predictions, outcomes)
    auc = ranking_auc(predictions, outcomes)

    if reference_brier is None:
        passed = True
    else:
        passed = cal.overall_brier <= reference_brier + brier_regression_tolerance

    return EvalRunResult(
        orchestrator_version=orchestrator_version,
        prompt_hash=prompt_hash,
        brier_score=cal.overall_brier,
        calibration=cal,
        ranking_auc=auc,
        n_assessments=len(rows),
        per_assessment_results=rows,
        passed_gate=passed,
    )
