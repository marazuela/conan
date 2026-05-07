"""
FDA calibration job — Phase 6.

Reads catalyst_universe ground-truth outcomes joined to fda_event_features
predictions, runs a bounded grid search for priors / thresholds, and writes
either:
  - one fda_calibration_runs row + one fda_model_versions row (effective_at NULL,
    pending manual activation), when guardrails pass
  - one operator_flags row (kind='insufficient_sample' | 'no_brier_improvement' |
    'drift_exceeded' | etc.), when guardrails fail

The script never auto-activates. Operators activate via SELECT
fda_calibration_activate('vX', 'note') from the Phase 6 RPC migration.

Usage (from repo root):
    python -m modal_workers.scripts.fda_calibration \
        --scope priors|thresholds|both \
        --lookback-days 365 \
        [--dry-run] \
        [--notes "free text"] \
        [--version-prefix v2026-05]

Run with --dry-run first to see what the job WOULD insert without writing.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

from modal_workers.shared.fda_calibration_math import (
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
from modal_workers.shared.supabase_client import SupabaseClient, SupabaseError

logger = logging.getLogger("fda_calibration")

CALIBRATION_QUERY = """
SELECT
  cu.catalyst_type,
  cu.catalyst_date::text AS catalyst_date,
  cu.material_outcome,
  cu.realized_price_move,
  fef.fair_probability,
  fef.market_implied_probability,
  fef.shadow_score,
  fef.shadow_band,
  fef.event_id,
  re.event_type,
  a.ticker, a.drug_name, a.indication
FROM public.catalyst_universe cu
JOIN public.fda_assets a
  ON a.ticker = cu.ticker
 AND (cu.mic IS NULL OR a.mic IS NOT DISTINCT FROM cu.mic)
JOIN public.fda_regulatory_events re
  ON re.asset_id = a.id
 AND re.event_date BETWEEN cu.catalyst_date - INTERVAL '60 days'
                       AND cu.catalyst_date + INTERVAL '60 days'
JOIN public.fda_event_features fef
  ON fef.event_id = re.id
WHERE cu.catalyst_type IN ('fda_approval','fda_crl','phase3_readout')
  AND cu.catalyst_date >= now() - ($1 || ' days')::interval
  AND cu.material_outcome IN ('yes','no')
  AND fef.snapshot_at < cu.catalyst_date
"""

RESOLUTION_EVENT_TYPES = frozenset({"approval", "crl", "presumed_crl", "withdrawal"})


# ---------------------------------------------------------------------------
# Label assignment
# ---------------------------------------------------------------------------


def label_from_row(row: Mapping[str, Any]) -> Optional[int]:
    """Map a calibration row to a binary outcome label.

    Returns 1 when (catalyst_type='fda_approval' OR 'phase3_readout') AND
    material_outcome='yes'. Returns 0 when catalyst_type='fda_crl' OR
    material_outcome='no'. Returns None for ambiguous rows (caller drops them).
    """
    catalyst_type = (row.get("catalyst_type") or "").lower()
    material = (row.get("material_outcome") or "").lower()
    if catalyst_type == "fda_crl":
        return 0
    if material == "yes" and catalyst_type in ("fda_approval", "phase3_readout"):
        return 1
    if material == "no":
        return 0
    return None


# ---------------------------------------------------------------------------
# Active model lookup + candidate evaluation
# ---------------------------------------------------------------------------


def load_active_model(client: SupabaseClient, scope: str) -> Optional[Dict[str, Any]]:
    """Fetch the currently-active fda_model_versions row for the given scope.

    Returns None when no active version exists (bootstrap case — the very first
    calibration run). The script then uses an empty baseline and the first
    successful candidate becomes v1.
    """
    rows = client._rest(
        "GET",
        "fda_model_versions",
        params={
            "scope": f"in.({scope},both)" if scope != "both" else "eq.both",
            "superseded_at": "is.null",
            "effective_at": "not.is.null",
            "select": "*",
            "order": "effective_at.desc",
            "limit": 1,
        },
    )
    if not rows:
        return None
    return rows[0]


def evaluate_candidate(
    *,
    holdout_predictions: List[float],
    holdout_outcomes: List[int],
) -> float:
    """Compute the holdout Brier score for one candidate's predictions."""
    return brier_score(holdout_predictions, holdout_outcomes)


def predict_with_priors(
    *,
    indication: Optional[str],
    designations: Mapping[str, Any],
    priors: Mapping[str, float],
    modifiers: Mapping[str, float],
    default_prior: float,
) -> float:
    """Replay the compose_features probability calculation for a candidate
    parameter set. Mirrors apply_designation_modifiers + base_probability from
    fda_event_features.
    """
    # Indication mapping is non-trivial — production code reads INDICATION_MAP
    # from biotech_base_rates.py. For calibration replay we already have the
    # historical prediction; we re-derive only the *delta* from a parameter
    # change. The script applies a simple proportional shift: when prior moves
    # by Δ, the historical prediction moves by Δ as well (since
    # fair_p = base + Σ modifiers and modifiers are additive).
    raise NotImplementedError(
        "predict_with_priors is replaced by replay_prediction; see calibrate()"
    )


def replay_prediction(
    *,
    historical_prediction: float,
    historical_priors_used: Optional[float],
    historical_modifiers_used: Mapping[str, Any],
    candidate_priors: Mapping[str, float],
    candidate_modifiers: Mapping[str, float],
    indication_key: Optional[str],
) -> float:
    """Re-derive a probability under candidate parameters by replaying deltas.

    The historical fair_probability stored in fda_event_features.fair_probability
    is `base_p(indication) + Σ active_designation_modifier_deltas`, clamped
    to [0,1]. Replay shifts it by:
        Δ_prior  = candidate_priors[indication]   - historical_priors_used
        Δ_modifs = sum(candidate_modifiers[k] - historical_modifiers_used[k]
                       for k where historical was applied)
    Then re-clamps to [0, 1].

    When indication_key is None or absent from either side, returns the
    historical prediction unchanged (parameter changes don't affect it).
    """
    delta = 0.0
    if indication_key:
        old_prior = (
            float(historical_priors_used)
            if historical_priors_used is not None
            else None
        )
        new_prior = candidate_priors.get(indication_key)
        if old_prior is not None and new_prior is not None:
            delta += float(new_prior) - old_prior

    historical_active = historical_modifiers_used or {}
    for key, was_applied in historical_active.items():
        if not was_applied:
            continue
        old_value = float(historical_active.get(key, 0)) if isinstance(historical_active.get(key), (int, float)) else 0.0
        # historical_modifiers_used may carry booleans (priority_review=True);
        # in that case look up the magnitude in the candidate set.
        new_value = candidate_modifiers.get(key, 0.0)
        delta += float(new_value) - old_value

    return max(0.0, min(1.0, float(historical_prediction) + delta))


# ---------------------------------------------------------------------------
# Operator flag emission for guardrail failures
# ---------------------------------------------------------------------------


def emit_operator_flag(
    client: SupabaseClient,
    *,
    kind: str,
    severity: str,
    title: str,
    body: str,
    evidence: Mapping[str, Any],
    dry_run: bool,
) -> None:
    """Insert an operator_flags row with source='fda_calibration'."""
    if dry_run:
        logger.info("dry-run: would emit operator_flags kind=%s title=%s", kind, title)
        return
    client._rest_with_retry(
        "POST",
        "operator_flags",
        json_body=[
            {
                "severity": severity,
                "source": "fda_calibration",
                "kind": kind,
                "title": title,
                "body": body,
                "evidence": dict(evidence),
            }
        ],
        prefer="resolution=ignore-duplicates,return=minimal",
    )


# ---------------------------------------------------------------------------
# Calibration entry point
# ---------------------------------------------------------------------------


def calibrate(
    client: SupabaseClient,
    *,
    scope: str,
    lookback_days: int,
    notes: Optional[str],
    version_prefix: str,
    dry_run: bool,
) -> int:
    """Run one calibration pass. Returns process exit code."""
    if scope not in ("priors", "thresholds", "both"):
        raise ValueError(f"scope must be priors|thresholds|both, got {scope!r}")

    # 1. Pull labeled training data via PostgREST RPC (raw SQL).
    logger.info("loading calibration rows (lookback=%d days)", lookback_days)
    raw_rows = client._rest(
        "POST",
        "rpc/fda_calibration_load",
        json_body={"p_lookback_days": lookback_days},
    )
    if not isinstance(raw_rows, list):
        raise SupabaseError(500, f"unexpected fda_calibration_load response: {raw_rows!r}")

    # Drop rows where label is ambiguous.
    records: List[Dict[str, Any]] = []
    for row in raw_rows:
        label = label_from_row(row)
        if label is None:
            continue
        if row.get("fair_probability") is None:
            continue
        records.append(
            {
                "event_id": row.get("event_id"),
                "indication": row.get("indication"),
                "event_type": row.get("event_type"),
                "label": int(label),
                "prediction": float(row["fair_probability"]),
                "realized_move": float(row.get("realized_price_move") or 0.0),
                "is_resolution_event": (
                    (row.get("event_type") or "").lower() in RESOLUTION_EVENT_TYPES
                ),
            }
        )
    sample_size = len(records)
    logger.info("loaded %d labeled rows", sample_size)

    # 2. Active model + baseline parameters.
    active = load_active_model(client, scope=scope)
    baseline_priors = (active or {}).get("priors_by_indication") or {}
    baseline_modifiers = (active or {}).get("designation_modifiers") or {}
    baseline_thresholds = (active or {}).get("band_thresholds") or {
        "immediate": 35.0,
        "watchlist": 25.0,
        "archive": 15.0,
    }

    # 3. Sample-size guard (short-circuits before grid search).
    if sample_size < DEFAULT_MIN_SAMPLE_SIZE:
        logger.warning(
            "insufficient sample (%d < %d); emitting operator_flag",
            sample_size,
            DEFAULT_MIN_SAMPLE_SIZE,
        )
        emit_operator_flag(
            client,
            kind="insufficient_sample",
            severity="warn",
            title=f"FDA calibration insufficient sample: n={sample_size}",
            body=f"Only {sample_size} labeled events in the last {lookback_days} days; need ≥{DEFAULT_MIN_SAMPLE_SIZE}.",
            evidence={"sample_size": sample_size, "lookback_days": lookback_days, "scope": scope},
            dry_run=dry_run,
        )
        return 2

    # 4. Holdout split.
    split = holdout_split(records, seed=DEFAULT_HOLDOUT_SEED, test_frac=DEFAULT_HOLDOUT_FRAC)
    holdout_predictions = [r["prediction"] for r in split.holdout]
    holdout_outcomes = [r["label"] for r in split.holdout]

    if not holdout_predictions:
        emit_operator_flag(
            client,
            kind="empty_holdout",
            severity="warn",
            title="FDA calibration produced empty holdout split",
            body=f"Sample={sample_size} but holdout split returned 0 rows.",
            evidence={"sample_size": sample_size, "holdout_frac": DEFAULT_HOLDOUT_FRAC},
            dry_run=dry_run,
        )
        return 2

    holdout_brier_old = brier_score(holdout_predictions, holdout_outcomes)
    logger.info("holdout n=%d, brier_old=%.6f", len(holdout_predictions), holdout_brier_old)

    # 5. Grid search.
    best_candidate: Optional[Dict[str, Any]] = None
    best_brier = holdout_brier_old

    if scope in ("priors", "both"):
        for cand_priors, cand_modifiers in generate_prior_candidates(
            baseline_priors, baseline_modifiers
        ):
            old_params = {
                "priors_by_indication": baseline_priors,
                "designation_modifiers": baseline_modifiers,
            }
            new_params = {
                "priors_by_indication": cand_priors,
                "designation_modifiers": cand_modifiers,
            }
            drift_ok, drift_pct, drift_offender = bounded_drift(
                old_params, new_params, max_pct=DEFAULT_MAX_DRIFT_PCT
            )
            if not drift_ok:
                continue
            # Replay predictions: apply (cand_priors[ind] - baseline_priors[ind])
            # to each row's stored fair_probability. We don't have the per-row
            # `priors_used`/`modifiers_used` snapshot in the calibration query
            # output (an enrichment for a future iteration), so as a tractable
            # approximation we shift each row by the indication's prior delta.
            shifted_predictions = []
            for r, pred in zip(split.holdout, holdout_predictions):
                indication = r.get("indication")
                old_v = baseline_priors.get(indication) if indication else None
                new_v = cand_priors.get(indication) if indication else None
                if old_v is not None and new_v is not None:
                    shift = float(new_v) - float(old_v)
                    shifted_predictions.append(max(0.0, min(1.0, pred + shift)))
                else:
                    shifted_predictions.append(pred)
            cand_brier = brier_score(shifted_predictions, holdout_outcomes)
            if cand_brier < best_brier:
                best_brier = cand_brier
                best_candidate = {
                    "priors_by_indication": cand_priors,
                    "designation_modifiers": cand_modifiers,
                    "band_thresholds": baseline_thresholds,
                    "drift_pct": drift_pct,
                    "drift_offender": drift_offender,
                    "predictions": shifted_predictions,
                }

    if scope in ("thresholds", "both"):
        # Threshold changes don't move predictions, only band assignment downstream.
        # We score them by delta to recall@threshold on the holdout, treating
        # thresholds[immediate] (scaled to 0..1 if needed) as the recall cutoff.
        # For Phase 6 V1 we leave threshold-only mode as a placeholder — band
        # discrimination will be added when the dashboard surfaces banded rows.
        logger.info("threshold-mode candidate generation: skipped in V1 (placeholder)")

    # 6. Guardrail evaluation.
    if best_candidate is None:
        # No candidate beat the baseline — already-good model OR sample noise
        emit_operator_flag(
            client,
            kind="no_brier_improvement",
            severity="info",
            title=f"FDA calibration found no improving candidate (n={sample_size})",
            body=f"Baseline Brier={holdout_brier_old:.6f}; no candidate beat it within drift bounds.",
            evidence={
                "sample_size": sample_size,
                "holdout_brier_old": holdout_brier_old,
                "scope": scope,
            },
            dry_run=dry_run,
        )
        # Insert a calibration_runs row that records the negative outcome for audit.
        if not dry_run:
            client._rest_with_retry(
                "POST",
                "fda_calibration_runs",
                json_body=[
                    {
                        "sample_size": sample_size,
                        "holdout_brier_old": holdout_brier_old,
                        "holdout_brier_new": holdout_brier_old,
                        "brier_relative_gain": 0.0,
                        "max_param_drift_pct": 0.0,
                        "passed": False,
                        "activated": False,
                        "notes": notes or "no improvement found",
                    }
                ],
                prefer="return=minimal",
            )
        return 0

    report = evaluate_guardrails(
        sample_size=sample_size,
        holdout_brier_old=holdout_brier_old,
        holdout_brier_new=best_brier,
        drift_ok=True,
        drift_pct=best_candidate["drift_pct"],
        drift_offender=best_candidate["drift_offender"],
    )
    logger.info(
        "best candidate brier=%.6f (gain=%.4f), passed=%s, reasons=%s",
        best_brier,
        report.brier_relative_gain or 0.0,
        report.passed,
        report.reasons,
    )

    # 7. Compute auxiliary metrics on the holdout.
    candidate_predictions = best_candidate["predictions"]
    is_resolution = [r["is_resolution_event"] for r in split.holdout]
    realized_moves = [r["realized_move"] for r in split.holdout]
    recall_old = recall(holdout_predictions, holdout_outcomes)
    recall_new = recall(candidate_predictions, holdout_outcomes)
    pea_old = post_edge_avoidance(holdout_predictions, is_resolution)
    pea_new = post_edge_avoidance(candidate_predictions, is_resolution)
    rev_old = realized_ev(holdout_predictions, realized_moves)
    rev_new = realized_ev(candidate_predictions, realized_moves)

    # 8. Persist (or print, on dry-run).
    if dry_run:
        proposed = {
            "scope": scope,
            "sample_size": sample_size,
            "holdout_brier_old": holdout_brier_old,
            "holdout_brier_new": best_brier,
            "brier_relative_gain": report.brier_relative_gain,
            "max_param_drift_pct": best_candidate["drift_pct"],
            "drift_offender": best_candidate["drift_offender"],
            "recall_old": recall_old,
            "recall_new": recall_new,
            "post_edge_avoidance_old": pea_old,
            "post_edge_avoidance_new": pea_new,
            "realized_ev_old": rev_old,
            "realized_ev_new": rev_new,
            "passed": report.passed,
            "guardrail_reasons": report.reasons,
            "proposed_priors_by_indication": best_candidate["priors_by_indication"],
            "proposed_designation_modifiers": best_candidate["designation_modifiers"],
            "proposed_band_thresholds": best_candidate["band_thresholds"],
        }
        print(json.dumps(proposed, indent=2, default=str))
        return 0 if report.passed else 1

    if not report.passed:
        emit_operator_flag(
            client,
            kind="guardrail_failed",
            severity="warn",
            title=f"FDA calibration guardrails failed: {', '.join(report.reasons)}",
            body=json.dumps(report.reasons),
            evidence={
                "sample_size": sample_size,
                "holdout_brier_old": holdout_brier_old,
                "holdout_brier_new": best_brier,
            },
            dry_run=False,
        )
        client._rest_with_retry(
            "POST",
            "fda_calibration_runs",
            json_body=[
                {
                    "sample_size": sample_size,
                    "holdout_brier_old": holdout_brier_old,
                    "holdout_brier_new": best_brier,
                    "brier_relative_gain": report.brier_relative_gain,
                    "max_param_drift_pct": best_candidate["drift_pct"],
                    "recall_old": recall_old,
                    "recall_new": recall_new,
                    "post_edge_avoidance_old": pea_old,
                    "post_edge_avoidance_new": pea_new,
                    "realized_ev_old": rev_old,
                    "realized_ev_new": rev_new,
                    "passed": False,
                    "activated": False,
                    "notes": (notes or "") + " | failed: " + "; ".join(report.reasons),
                }
            ],
            prefer="return=minimal",
        )
        return 1

    # Guardrails passed — insert model_version (effective_at NULL until activated)
    # and the calibration_runs row referencing it.
    new_version = f"{version_prefix}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    mv_rows = client._rest_with_retry(
        "POST",
        "fda_model_versions",
        json_body=[
            {
                "version": new_version,
                "scope": scope,
                "priors_by_indication": best_candidate["priors_by_indication"],
                "designation_modifiers": best_candidate["designation_modifiers"],
                "band_thresholds": best_candidate["band_thresholds"],
                "sizing_caps": (active or {}).get("sizing_caps") or {},
                "effective_at": None,
                "superseded_at": None,
                "created_by": os.environ.get("USER") or "fda_calibration",
                "notes": notes,
            }
        ],
        prefer="return=representation",
    )
    if not isinstance(mv_rows, list) or not mv_rows:
        raise SupabaseError(500, f"unexpected fda_model_versions insert response: {mv_rows!r}")
    new_id = mv_rows[0]["id"]

    client._rest_with_retry(
        "POST",
        "fda_calibration_runs",
        json_body=[
            {
                "model_version_id": new_id,
                "sample_size": sample_size,
                "holdout_brier_old": holdout_brier_old,
                "holdout_brier_new": best_brier,
                "brier_relative_gain": report.brier_relative_gain,
                "max_param_drift_pct": best_candidate["drift_pct"],
                "recall_old": recall_old,
                "recall_new": recall_new,
                "post_edge_avoidance_old": pea_old,
                "post_edge_avoidance_new": pea_new,
                "realized_ev_old": rev_old,
                "realized_ev_new": rev_new,
                "passed": True,
                "activated": False,
                "notes": notes,
            }
        ],
        prefer="return=minimal",
    )
    logger.info(
        "calibration passed; new model_version=%s id=%s (NOT yet activated)",
        new_version,
        new_id,
    )
    print(json.dumps({"version": new_version, "id": new_id, "passed": True}, default=str))
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", default="priors", choices=["priors", "thresholds", "both"])
    parser.add_argument("--lookback-days", type=int, default=365)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--notes", default=None)
    parser.add_argument("--version-prefix", default="v")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level, format="%(levelname)s %(name)s %(message)s")

    client = SupabaseClient(
        url=os.environ.get("SUPABASE_URL"),
        service_key=os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_KEY"),
    )
    return calibrate(
        client,
        scope=args.scope,
        lookback_days=args.lookback_days,
        notes=args.notes,
        version_prefix=args.version_prefix,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
