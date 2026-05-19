"""v3 closed feedback loop — nightly isotonic calibration refit (Stream 2).

Reads all post_mortem_complete rows + their original raw_conviction_pct
from convergence_assessments, fits a fresh isotonic curve via
modal_workers.shared.compute.fit_isotonic_curve, and gates the swap
against D-103's paired-bootstrap criterion.

D-103 gate (`eval_runs.passed_gate=true` iff ALL of):
  - brier_delta_vs_prod > 0           (new curve has lower Brier)
  - paired_bootstrap_p < 0.05         (paired-bootstrap on Brier delta)
  - n_eval_cases >= 200
  - ranking_auc_delta_vs_prod >= 0.05
  - max_single_asset_contribution_pct <= 5.0

When the gate passes (and ENABLE_PROMOTION=true is set), this script:
  1. Inserts the new curve into calibration_curves (is_active=false at first).
  2. Snapshots the prior active curve's prompt_versions row (D-104 policy)
     by leaving the prior curve in place but flipping is_active.
  3. Atomically: UPDATE calibration_curves SET is_active=false WHERE is_active;
     UPDATE calibration_curves SET is_active=true WHERE version=<new>.
  4. Inserts an eval_runs row recording the gate inputs + decision.
  5. Returns a structured summary suitable for Modal logging.

When the gate fails: writes the eval_runs row with passed_gate=false and
gate_reason set; does NOT activate the new curve.

CLI: python -m modal_workers.scripts.nightly_calibration_refit \\
       --min-n 200 --bootstrap-resamples 10000
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import random
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from modal_workers.shared.compute import (
    apply_isotonic_calibration,
    fit_isotonic_curve,
    get_active_calibration_curve,
)
from modal_workers.shared.fda_calibration_math import brier_score
from modal_workers.shared.supabase_client import SupabaseClient

logger = logging.getLogger(__name__)

# D-103 thresholds — keep here for clarity.
GATE_MIN_N = 200
GATE_MAX_P_VALUE = 0.05
GATE_MIN_AUC_DELTA = 0.05
GATE_MAX_SINGLE_ASSET_PCT = 5.0
DEFAULT_BOOTSTRAP_RESAMPLES = 10000

# When False, refit ALWAYS writes eval_runs but never flips is_active —
# operator manually promotes via fda_calibration_activate(p_version).
DEFAULT_ENABLE_PROMOTION = (
    os.environ.get("ENABLE_PROMOTION", "false").lower() == "true"
)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class GateEvaluation:
    """The per-D-103 gate decision."""
    passed: bool
    gate_reason: str
    n_eval_cases: int
    brier_prod: Optional[float]
    brier_new: Optional[float]
    brier_delta: Optional[float]
    paired_bootstrap_p: Optional[float]
    ranking_auc_prod: Optional[float]
    ranking_auc_new: Optional[float]
    ranking_auc_delta: Optional[float]
    max_single_asset_contribution_pct: Optional[float]


@dataclass
class RefitResult:
    n_training: int
    new_curve_version: Optional[str]
    new_curve_data: Dict[str, Any]
    activated: bool
    gate: GateEvaluation


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_nightly_refit(
    *,
    sb: Optional[SupabaseClient] = None,
    min_n: int = GATE_MIN_N,
    bootstrap_resamples: int = DEFAULT_BOOTSTRAP_RESAMPLES,
    enable_promotion: Optional[bool] = None,
    rng_seed: Optional[int] = None,
) -> RefitResult:
    """Pull resolved post-mortems, fit new isotonic, gate vs prod, optionally promote."""
    sb = sb or SupabaseClient()
    if enable_promotion is None:
        enable_promotion = DEFAULT_ENABLE_PROMOTION

    raw, realized, asset_ids = _fetch_training_pairs(sb)
    n = len(raw)
    if n == 0:
        return RefitResult(
            n_training=0,
            new_curve_version=None,
            new_curve_data={},
            activated=False,
            gate=GateEvaluation(
                passed=False, gate_reason="no_baseline",
                n_eval_cases=0,
                brier_prod=None, brier_new=None, brier_delta=None,
                paired_bootstrap_p=None,
                ranking_auc_prod=None, ranking_auc_new=None, ranking_auc_delta=None,
                max_single_asset_contribution_pct=None,
            ),
        )

    # Fit new curve.
    new_curve_data = fit_isotonic_curve(raw, realized)

    # Compute current vs new Brier on the same set (paired) — this is the gate.
    prod_curve = get_active_calibration_curve(sb)
    prod_curve_data = (prod_curve or {}).get("curve_data")

    pred_prod = [apply_isotonic_calibration(p, prod_curve_data) for p in raw]
    pred_new = [apply_isotonic_calibration(p, new_curve_data) for p in raw]

    gate = evaluate_gate(
        raw=list(raw),
        realized=list(realized),
        asset_ids=list(asset_ids),
        pred_prod=pred_prod,
        pred_new=pred_new,
        min_n=min_n,
        bootstrap_resamples=bootstrap_resamples,
        rng_seed=rng_seed,
    )

    # Persist a candidate curve regardless of gate outcome — it lets operators
    # manually promote via fda_calibration_activate even when auto-gate fails.
    version = _make_curve_version(new_curve_data, n)
    activated = False
    if gate.passed and enable_promotion:
        _insert_curve(sb, version=version, curve_data=new_curve_data,
                      n_training=n, brier=gate.brier_new, activate=True)
        activated = True
    else:
        _insert_curve(sb, version=version, curve_data=new_curve_data,
                      n_training=n, brier=gate.brier_new, activate=False)

    # Always log the eval run for audit.
    _insert_eval_run(sb, version=version, gate=gate, activated=activated)

    return RefitResult(
        n_training=n,
        new_curve_version=version,
        new_curve_data=new_curve_data,
        activated=activated,
        gate=gate,
    )


# ---------------------------------------------------------------------------
# Pure helpers (testable)
# ---------------------------------------------------------------------------

def evaluate_gate(
    *,
    raw: Sequence[float],
    realized: Sequence[int],
    asset_ids: Sequence[str],
    pred_prod: Sequence[float],
    pred_new: Sequence[float],
    min_n: int = GATE_MIN_N,
    bootstrap_resamples: int = DEFAULT_BOOTSTRAP_RESAMPLES,
    rng_seed: Optional[int] = None,
) -> GateEvaluation:
    """Evaluate D-103 paired-bootstrap promotion gate.

    All sequences must be the same length. Returns a GateEvaluation
    capturing the inputs to the gate decision so eval_runs can persist
    the audit trail. The five conditions are AND-ed: any failure surfaces
    its specific gate_reason.
    """
    n = len(raw)
    if not (n == len(realized) == len(pred_prod) == len(pred_new) == len(asset_ids)):
        raise ValueError("evaluate_gate: input lengths mismatch")

    if n == 0:
        return GateEvaluation(
            passed=False, gate_reason="no_baseline", n_eval_cases=0,
            brier_prod=None, brier_new=None, brier_delta=None,
            paired_bootstrap_p=None,
            ranking_auc_prod=None, ranking_auc_new=None, ranking_auc_delta=None,
            max_single_asset_contribution_pct=None,
        )

    brier_prod = brier_score(list(pred_prod), list(realized))
    brier_new = brier_score(list(pred_new), list(realized))
    brier_delta = brier_prod - brier_new  # positive = improvement

    auc_prod = ranking_auc(list(pred_prod), list(realized))
    auc_new = ranking_auc(list(pred_new), list(realized))
    auc_delta = auc_new - auc_prod

    # Per-asset contribution to the Brier delta. Identifies whether one asset's
    # pathological case is dragging the win.
    per_asset = _per_asset_brier_contribution(
        asset_ids, pred_prod, pred_new, realized
    )
    if per_asset and brier_delta != 0:
        # |contribution| / |total delta| as a percentage.
        total = sum(abs(v) for v in per_asset.values())
        max_contrib = (max(abs(v) for v in per_asset.values()) / total * 100.0
                       if total > 0 else 0.0)
    else:
        max_contrib = 0.0

    p_value = paired_bootstrap_p_value(
        list(pred_prod), list(pred_new), list(realized),
        n_resamples=bootstrap_resamples,
        rng_seed=rng_seed,
    )

    # Apply gate criteria in order — surface the first failing reason.
    if n < min_n:
        reason = "n_too_low"
    elif brier_delta <= 0:
        reason = "brier_regression"
    elif p_value >= GATE_MAX_P_VALUE:
        reason = "p_above_threshold"
    elif auc_delta < GATE_MIN_AUC_DELTA:
        reason = "auc_delta_below"
    elif max_contrib > GATE_MAX_SINGLE_ASSET_PCT:
        reason = "asset_concentration"
    else:
        reason = "pass"

    return GateEvaluation(
        passed=(reason == "pass"),
        gate_reason=reason,
        n_eval_cases=n,
        brier_prod=round(brier_prod, 6),
        brier_new=round(brier_new, 6),
        brier_delta=round(brier_delta, 6),
        paired_bootstrap_p=round(p_value, 6),
        ranking_auc_prod=round(auc_prod, 4),
        ranking_auc_new=round(auc_new, 4),
        ranking_auc_delta=round(auc_delta, 4),
        max_single_asset_contribution_pct=round(max_contrib, 2),
    )


def paired_bootstrap_p_value(
    pred_prod: Sequence[float],
    pred_new: Sequence[float],
    realized: Sequence[int],
    *,
    n_resamples: int = DEFAULT_BOOTSTRAP_RESAMPLES,
    rng_seed: Optional[int] = None,
) -> float:
    """Paired-bootstrap one-sided p-value for H0: brier_new >= brier_prod.

    Resamples indices with replacement, recomputes the Brier delta on each
    sample, and returns the fraction of resamples where the new model did
    NOT beat prod (delta <= 0). With n=0 returns 1.0 (cannot reject).
    """
    n = len(pred_prod)
    if n == 0:
        return 1.0

    rng = random.Random(rng_seed)
    observed = brier_score(list(pred_prod), list(realized)) - brier_score(list(pred_new), list(realized))
    if observed <= 0:
        # Trivially fails — return the null-friendly p value upper-bound.
        return 1.0

    indices = list(range(n))
    fail_count = 0
    for _ in range(n_resamples):
        sample = [rng.choice(indices) for _ in range(n)]
        bp = brier_score([pred_prod[i] for i in sample], [realized[i] for i in sample])
        bn = brier_score([pred_new[i] for i in sample], [realized[i] for i in sample])
        if (bp - bn) <= 0:
            fail_count += 1

    return fail_count / n_resamples


def ranking_auc(predictions: Sequence[float], realized: Sequence[int]) -> float:
    """ROC AUC via the rank-sum form. O(n log n). Returns 0.5 with no
    discriminating signal (all same class or all same prediction).
    """
    pos = [p for p, y in zip(predictions, realized) if y == 1]
    neg = [p for p, y in zip(predictions, realized) if y == 0]
    if not pos or not neg:
        return 0.5
    # Use the U statistic / (n_pos * n_neg) form.
    paired = sorted(zip(predictions, realized), key=lambda t: t[0])
    rank_sum_pos = 0.0
    # Assign average ranks for ties.
    i = 0
    n = len(paired)
    while i < n:
        j = i
        while j < n and paired[j][0] == paired[i][0]:
            j += 1
        avg_rank = (i + j + 1) / 2.0  # 1-indexed average
        for k in range(i, j):
            if paired[k][1] == 1:
                rank_sum_pos += avg_rank
        i = j
    n_pos = len(pos)
    n_neg = len(neg)
    u = rank_sum_pos - n_pos * (n_pos + 1) / 2.0
    return u / (n_pos * n_neg)


def _per_asset_brier_contribution(
    asset_ids: Sequence[str],
    pred_prod: Sequence[float],
    pred_new: Sequence[float],
    realized: Sequence[int],
) -> Dict[str, float]:
    """Per-asset summed (brier_prod - brier_new) contribution. Positive
    = asset helped the new model win on that asset's rows.
    """
    result: Dict[str, float] = {}
    for aid, pp, pn, y in zip(asset_ids, pred_prod, pred_new, realized):
        contrib = (pp - y) ** 2 - (pn - y) ** 2
        result[aid] = result.get(aid, 0.0) + contrib
    return result


def _make_curve_version(curve: Dict[str, Any], n: int) -> str:
    """Stable version string from the curve content + training-set size +
    UTC date. Format: iso_<YYYYMMDD>_n<N>_<hash6>.
    """
    blob = repr(curve.get("knots") or []).encode()
    h = hashlib.sha256(blob).hexdigest()[:6]
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"iso_{today}_n{n}_{h}"


# ---------------------------------------------------------------------------
# DB I/O
# ---------------------------------------------------------------------------

def _fetch_training_pairs(
    sb: SupabaseClient,
) -> Tuple[List[float], List[int], List[str]]:
    """Pull (raw_conviction_pct/100, hit_int, asset_id) tuples for every
    post_mortem_complete row whose convergence_assessment exists.
    """
    pms = sb._rest("GET", "post_mortem_queue", params={
        "select": "assessment_id,asset_id,realized_outcome,predicted_direction",
        "status": "eq.post_mortem_complete",
        "limit": "10000",
    }) or []

    if not pms:
        return [], [], []

    assessment_ids = [p["assessment_id"] for p in pms]
    in_filter = f"in.({','.join(assessment_ids)})"
    assessments = sb._rest("GET", "convergence_assessments", params={
        "select": "id,raw_conviction_pct,conviction_pct,thesis_direction",
        "id": in_filter,
        "limit": "10000",
    }) or []
    asmt_by_id = {a["id"]: a for a in assessments}

    raw: List[float] = []
    realized: List[int] = []
    asset_ids: List[str] = []
    for p in pms:
        ro = p.get("realized_outcome") or {}
        hit = ro.get("hit")
        if hit is None:
            continue  # no_outcome path
        a = asmt_by_id.get(p["assessment_id"])
        if not a:
            continue
        raw_pct = a.get("raw_conviction_pct") or a.get("conviction_pct")
        if raw_pct is None:
            continue
        # Direction-aligned realized: we calibrate the conviction probability
        # of the predicted direction being correct. Map the raw asymmetry into
        # 0/1 outcomes so the curve learns "X% predicted → Y% empirical."
        realized_int = _direction_aligned_outcome(p.get("predicted_direction"), bool(hit))
        raw.append(float(raw_pct) / 100.0)
        realized.append(realized_int)
        asset_ids.append(p["asset_id"])

    return raw, realized, asset_ids


def _direction_aligned_outcome(direction: Optional[str], hit: bool) -> int:
    """Map (direction, hit) → 0/1. Mirrors realized_outcome_score in
    post_mortem_runner but yields a binary for isotonic fitting:
      - long + hit  → 1
      - long + miss → 0
      - short + hit → 0   (HIT means stock UP — short was wrong)
      - short + miss → 1
      - neutral/straddle → use hit unchanged (binary thesis = move occurred)
    """
    d = (direction or "").lower()
    if d == "long":
        return 1 if hit else 0
    if d == "short":
        return 0 if hit else 1
    return 1 if hit else 0


def _insert_curve(
    sb: SupabaseClient,
    *,
    version: str,
    curve_data: Dict[str, Any],
    n_training: int,
    brier: Optional[float],
    activate: bool,
) -> None:
    """Insert calibration_curves row. When activate=True, atomically flip
    is_active on the prior winner first.

    Note: PostgREST transaction semantics over REST mean this isn't a single
    transaction. Race window is tiny (sub-second) and a follow-up read will
    self-heal — only one row may be is_active=true (no constraint enforces
    that, but the orchestrator's get_active_calibration_curve uses limit=1
    so even mid-race it picks one curve, never zero).
    """
    sb._rest_with_retry("POST", "calibration_curves", json_body={
        "version": version,
        "curve_data": curve_data,
        "n_training_samples": n_training,
        "brier_score": brier,
        "is_active": False,
    }, prefer="resolution=ignore-duplicates,return=minimal")

    if activate:
        # Flip the prior active curve.
        sb._rest_with_retry("PATCH", "calibration_curves",
                            params={"is_active": "eq.true"},
                            json_body={"is_active": False},
                            prefer="return=minimal")
        sb._rest_with_retry("PATCH", "calibration_curves",
                            params={"version": f"eq.{version}"},
                            json_body={"is_active": True},
                            prefer="return=minimal")


def _insert_eval_run(
    sb: SupabaseClient,
    *,
    version: str,
    gate: GateEvaluation,
    activated: bool,
) -> None:
    body = {
        "orchestrator_version": os.environ.get("ORCHESTRATOR_VERSION", "calibration_refit"),
        "prompt_hash": version,  # we're refitting calibration, not a prompt; reuse field for traceability
        "brier_score": gate.brier_new,
        "brier_delta_vs_prod": gate.brier_delta,
        "paired_bootstrap_p": gate.paired_bootstrap_p,
        "ranking_auc": gate.ranking_auc_new,
        "ranking_auc_delta_vs_prod": gate.ranking_auc_delta,
        "n_eval_cases": gate.n_eval_cases,
        "max_single_asset_contribution_pct": gate.max_single_asset_contribution_pct,
        "passed_gate": gate.passed,
        "gate_reason": gate.gate_reason,
        "per_assessment_results": {
            "calibration_curve_version": version,
            "activated": activated,
            "gate_inputs": asdict(gate),
        },
    }
    sb._rest_with_retry("POST", "eval_runs", json_body=body,
                        prefer="return=minimal")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Phase 4B — Tier-2 quality gate
#
# Compares 30-day Brier between Tier-1 (full pipeline) and Tier-2 (Cowork
# bulk single-shot) assessments. Surfaces an operator_flag with
# source='tier2_quality' when Tier-2 is materially worse than Tier-1
# (default threshold: 0.15 absolute Brier delta). Also tracks both Brier
# values + sample counts in evidence so operators can sanity-check.
#
# Gate semantics (per bulk_orchestrator.md §Verification): Tier-2 should
# stay within 0.15 Brier of Tier-1. We fire when
# (tier2_brier - tier1_brier) > 0.15 — Tier-2 demonstrably worse. We do
# NOT fire when Tier-2 is BETTER than Tier-1; that's a happy surprise
# but not an alert condition. Insufficient samples (< MIN_PER_TIER per
# side) → resolve any open flag and exit (no comparison possible).
# ---------------------------------------------------------------------------

TIER2_QUALITY_THRESHOLD = 0.15
TIER2_QUALITY_MIN_PER_TIER = 30   # below this, statistical power is too low
TIER2_QUALITY_LOOKBACK_DAYS = 30
TIER2_QUALITY_FLAG_SOURCE = "tier2_quality"
TIER2_QUALITY_FLAG_KIND = "tier2_brier_drift"


@dataclass
class TierBrierGate:
    """Result of a Tier-1 vs Tier-2 Brier comparison sweep."""
    n_tier1: int
    n_tier2: int
    brier_tier1: Optional[float]
    brier_tier2: Optional[float]
    delta: Optional[float]            # tier2 - tier1; positive = Tier-2 worse
    threshold: float
    flagged: bool
    skip_reason: Optional[str]        # set when comparison was skipped

    def to_evidence(self) -> Dict[str, Any]:
        return {
            "n_tier1": self.n_tier1,
            "n_tier2": self.n_tier2,
            "brier_tier1": (
                round(self.brier_tier1, 4)
                if self.brier_tier1 is not None else None
            ),
            "brier_tier2": (
                round(self.brier_tier2, 4)
                if self.brier_tier2 is not None else None
            ),
            "delta": (
                round(self.delta, 4) if self.delta is not None else None
            ),
            "threshold": self.threshold,
            "skip_reason": self.skip_reason,
        }


def _fetch_tier_brier_pairs(
    sb: SupabaseClient,
    *,
    lookback_days: int,
) -> Tuple[List[Tuple[float, int]], List[Tuple[float, int]]]:
    """Pull (conviction_pct/100, hit_int) tuples from post_mortem_complete
    rows whose underlying convergence_assessments row was created in the
    last `lookback_days`, partitioned by tier (1, 2). Returns
    (tier1_pairs, tier2_pairs).
    """
    pms = sb._rest("GET", "post_mortem_queue", params={
        "select": "assessment_id,realized_outcome,predicted_direction",
        "status": "eq.post_mortem_complete",
        "limit": "10000",
    }) or []
    if not pms:
        return [], []

    assessment_ids = [p["assessment_id"] for p in pms]
    in_filter = f"in.({','.join(assessment_ids)})"
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=lookback_days)
    ).isoformat()
    assessments = sb._rest("GET", "convergence_assessments", params={
        "select": "id,tier,conviction_pct,thesis_direction,created_at",
        "id": in_filter,
        "created_at": f"gte.{cutoff}",
        "limit": "10000",
    }) or []
    asmt_by_id = {a["id"]: a for a in assessments}

    tier1: List[Tuple[float, int]] = []
    tier2: List[Tuple[float, int]] = []
    for p in pms:
        ro = p.get("realized_outcome") or {}
        hit = ro.get("hit")
        if hit is None:
            continue
        a = asmt_by_id.get(p["assessment_id"])
        if not a:
            continue
        pct = a.get("conviction_pct")
        if pct is None:
            continue
        realized_int = _direction_aligned_outcome(
            p.get("predicted_direction"), bool(hit),
        )
        pair = (float(pct) / 100.0, realized_int)
        tier = a.get("tier")
        if tier == 1:
            tier1.append(pair)
        elif tier == 2:
            tier2.append(pair)
        # tier=3 (backtest) is not part of the production-quality gate.

    return tier1, tier2


def evaluate_tier_brier_gate(
    tier1_pairs: List[Tuple[float, int]],
    tier2_pairs: List[Tuple[float, int]],
    *,
    threshold: float = TIER2_QUALITY_THRESHOLD,
    min_per_tier: int = TIER2_QUALITY_MIN_PER_TIER,
) -> TierBrierGate:
    """Pure helper: given two lists of (predicted, realized) pairs, decide
    whether the Tier-2 quality flag should fire."""
    n1, n2 = len(tier1_pairs), len(tier2_pairs)

    if n1 < min_per_tier or n2 < min_per_tier:
        return TierBrierGate(
            n_tier1=n1, n_tier2=n2,
            brier_tier1=None, brier_tier2=None,
            delta=None, threshold=threshold,
            flagged=False,
            skip_reason=(
                f"insufficient_samples(min_per_tier={min_per_tier}, "
                f"n_tier1={n1}, n_tier2={n2})"
            ),
        )

    b1 = brier_score([p for p, _ in tier1_pairs],
                     [r for _, r in tier1_pairs])
    b2 = brier_score([p for p, _ in tier2_pairs],
                     [r for _, r in tier2_pairs])
    delta = b2 - b1
    flagged = delta > threshold

    return TierBrierGate(
        n_tier1=n1, n_tier2=n2,
        brier_tier1=b1, brier_tier2=b2,
        delta=delta, threshold=threshold,
        flagged=flagged, skip_reason=None,
    )


def run_tier_quality_gate(
    *,
    sb: Optional[SupabaseClient] = None,
    lookback_days: int = TIER2_QUALITY_LOOKBACK_DAYS,
    threshold: float = TIER2_QUALITY_THRESHOLD,
    min_per_tier: int = TIER2_QUALITY_MIN_PER_TIER,
) -> TierBrierGate:
    """Phase 4B: compare Tier-1 vs Tier-2 30-day Brier, raise/resolve the
    `tier2_quality` operator_flag accordingly. Safe to run nightly even
    when there's no Tier-2 production data — it'll just resolve any prior
    flag and skip with `insufficient_samples`."""
    sb = sb or SupabaseClient()
    from modal_workers.observability import _resolve_flag, _upsert_flag

    tier1, tier2 = _fetch_tier_brier_pairs(sb, lookback_days=lookback_days)
    gate = evaluate_tier_brier_gate(
        tier1, tier2, threshold=threshold, min_per_tier=min_per_tier,
    )

    if gate.flagged:
        _upsert_flag(
            sb,
            severity="warn",
            source=TIER2_QUALITY_FLAG_SOURCE,
            kind=TIER2_QUALITY_FLAG_KIND,
            title=(
                f"Tier-2 Brier {gate.brier_tier2:.3f} is "
                f"{gate.delta:+.3f} worse than Tier-1 "
                f"{gate.brier_tier1:.3f} over {lookback_days}d "
                f"(threshold {threshold:.2f})"
            ),
            evidence=gate.to_evidence(),
        )
    else:
        _resolve_flag(
            sb,
            source=TIER2_QUALITY_FLAG_SOURCE,
            kind=TIER2_QUALITY_FLAG_KIND,
            note=(
                f"auto-resolved: " + (
                    gate.skip_reason
                    if gate.skip_reason
                    else f"delta={gate.delta:+.3f} <= {threshold}"
                )
            ),
        )

    logger.info(
        "tier2_quality_gate: n_tier1=%d n_tier2=%d brier1=%s brier2=%s "
        "delta=%s flagged=%s skip=%s",
        gate.n_tier1, gate.n_tier2,
        f"{gate.brier_tier1:.3f}" if gate.brier_tier1 is not None else "—",
        f"{gate.brier_tier2:.3f}" if gate.brier_tier2 is not None else "—",
        f"{gate.delta:+.3f}" if gate.delta is not None else "—",
        gate.flagged, gate.skip_reason or "",
    )
    return gate


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Nightly isotonic calibration refit (D-103).")
    parser.add_argument("--min-n", type=int, default=GATE_MIN_N)
    parser.add_argument("--bootstrap-resamples", type=int, default=DEFAULT_BOOTSTRAP_RESAMPLES)
    parser.add_argument("--enable-promotion", action="store_true",
                        help="Override env ENABLE_PROMOTION; if set, gate-passing curves auto-promote.")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--skip-tier-quality-gate", action="store_true",
                        help="Skip the Phase 4B Tier-1 vs Tier-2 Brier comparison.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    result = run_nightly_refit(
        min_n=args.min_n,
        bootstrap_resamples=args.bootstrap_resamples,
        enable_promotion=(args.enable_promotion or DEFAULT_ENABLE_PROMOTION),
        rng_seed=args.seed,
    )
    logger.info(
        "nightly_calibration_refit: n=%d gate=%s reason=%s activated=%s version=%s",
        result.n_training, result.gate.passed, result.gate.gate_reason,
        result.activated, result.new_curve_version,
    )

    if not args.skip_tier_quality_gate:
        # Run independently of the calibration refit's outcome — even if the
        # refit short-circuited on no_baseline, we still want to flag any
        # pre-existing Tier-2 quality regression.
        try:
            run_tier_quality_gate()
        except Exception:  # noqa: BLE001
            # Quality gate is observability-only; never let it fail the
            # calibration refit's exit code.
            logger.exception("tier2_quality_gate: errored, continuing")

    return 0 if (result.gate.passed or result.n_training == 0) else 0


if __name__ == "__main__":
    sys.exit(main())
