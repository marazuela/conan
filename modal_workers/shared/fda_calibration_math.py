"""
Pure math helpers for FDA calibration.

No database I/O. No HTTP. Tests can drive every function with hand-built
fixtures. The script that calls these (modal_workers/scripts/fda_calibration.py)
owns the SQL.

Functions:
  brier_score          (predictions, outcomes) -> float
  recall               (predictions, outcomes, threshold=0.5) -> float
  post_edge_avoidance  (predictions, was_resolution_event) -> float
  realized_ev          (predictions, signed_moves) -> float
  bounded_drift        (old, new, max_pct=0.10) -> (ok, max_drift, offending_path)
  holdout_split        (records, seed=20260505, test_frac=0.2) -> (train, holdout)
  generate_prior_candidates    (current_priors, indication_step, modifier_step) -> List[priors]
  generate_threshold_candidates(current_thresholds, step) -> List[thresholds]
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

# Constants drawn from the plan; centralized here so tests can import them.
DEFAULT_HOLDOUT_SEED = 20260505
DEFAULT_HOLDOUT_FRAC = 0.20
DEFAULT_MIN_SAMPLE_SIZE = 30
DEFAULT_BRIER_RELATIVE_GAIN = 0.02
DEFAULT_MAX_DRIFT_PCT = 0.10


# ---------------------------------------------------------------------------
# Brier / recall / EV
# ---------------------------------------------------------------------------


def brier_score(predictions: Sequence[float], outcomes: Sequence[int]) -> float:
    """Mean squared error between predicted probability and binary outcome.

    Outcome values must be 0 or 1. Lower is better. A perfect predictor scores 0;
    always-0.5 on a balanced sample scores 0.25.

    Raises ValueError on shape mismatch or empty input.
    """
    if len(predictions) != len(outcomes):
        raise ValueError(
            f"brier_score: shape mismatch: predictions={len(predictions)} outcomes={len(outcomes)}"
        )
    if len(predictions) == 0:
        raise ValueError("brier_score: empty input")
    total = 0.0
    for p, y in zip(predictions, outcomes):
        if y not in (0, 1):
            raise ValueError(f"brier_score: outcome must be 0 or 1, got {y!r}")
        diff = float(p) - float(y)
        total += diff * diff
    return total / len(predictions)


def recall(
    predictions: Sequence[float],
    outcomes: Sequence[int],
    *,
    threshold: float = 0.5,
) -> float:
    """Fraction of label-1 cases the model flags at or above threshold.

    Returns 0.0 when there are zero label-1 cases (defining recall as 0 rather
    than NaN keeps the calibration report defensive against tiny samples).
    """
    if len(predictions) != len(outcomes):
        raise ValueError(
            f"recall: shape mismatch: predictions={len(predictions)} outcomes={len(outcomes)}"
        )
    positives = [(p, y) for p, y in zip(predictions, outcomes) if y == 1]
    if not positives:
        return 0.0
    flagged = sum(1 for p, _ in positives if float(p) >= threshold)
    return flagged / len(positives)


def post_edge_avoidance(
    predictions: Sequence[float],
    was_resolution_event: Sequence[bool],
    *,
    immediate_threshold: float = 0.50,
) -> float:
    """% of resolution events the model did NOT score as a new opportunity.

    A 'new opportunity' here means prediction >= immediate_threshold. The bridge
    blocks resolution events at the `is_resolution_event` gate before any
    `signals` row is emitted, so this metric is a sanity check that the
    underlying probability model isn't claiming high P(approval) for an event
    that's already resolved.

    Returns 1.0 when there are no resolution events in the sample.
    """
    if len(predictions) != len(was_resolution_event):
        raise ValueError("post_edge_avoidance: shape mismatch")
    resolutions = [
        (p, was_res)
        for p, was_res in zip(predictions, was_resolution_event)
        if bool(was_res)
    ]
    if not resolutions:
        return 1.0
    avoided = sum(1 for p, _ in resolutions if float(p) < immediate_threshold)
    return avoided / len(resolutions)


def realized_ev(
    predictions: Sequence[float],
    signed_moves: Sequence[float],
) -> float:
    """Average of (prediction × signed realized move).

    `signed_moves` is direction-flipped per `signal_price_snapshots.signed_move_pct`
    (positive = thesis was right). High predictions on big-positive moves score
    high; high predictions on big-negative moves score low. Useful as a
    proxy for "did the model lean into winners and avoid losers?"
    """
    if len(predictions) != len(signed_moves):
        raise ValueError("realized_ev: shape mismatch")
    if not predictions:
        return 0.0
    total = 0.0
    for p, m in zip(predictions, signed_moves):
        total += float(p) * float(m)
    return total / len(predictions)


# ---------------------------------------------------------------------------
# Bounded drift check
# ---------------------------------------------------------------------------


def bounded_drift(
    old: Mapping[str, Any],
    new: Mapping[str, Any],
    *,
    max_pct: float = DEFAULT_MAX_DRIFT_PCT,
) -> Tuple[bool, float, Optional[str]]:
    """Walk both nested dicts; for every numeric leaf that exists in both, check
    that |new - old| / max(|old|, eps) <= max_pct.

    Returns (ok, max_observed_drift, offending_path).
    - ok=True when every paired numeric leaf is within bounds.
    - max_observed_drift is the largest fractional change seen.
    - offending_path is the dotted path of the first leaf to exceed max_pct,
      or None when ok=True.

    Keys that exist in only one side are ignored — adding a new indication or
    modifier is not a "drift" of an existing parameter.
    """
    eps = 1e-12
    max_drift_seen = 0.0
    offending: Optional[str] = None

    def _walk(o: Any, n: Any, path: str) -> bool:
        nonlocal max_drift_seen, offending
        if isinstance(o, Mapping) and isinstance(n, Mapping):
            for key in o.keys() & n.keys():
                child_path = f"{path}.{key}" if path else str(key)
                if not _walk(o[key], n[key], child_path):
                    return False
            return True
        if isinstance(o, (int, float)) and isinstance(n, (int, float)) and not isinstance(o, bool) and not isinstance(n, bool):
            denom = max(abs(float(o)), eps)
            drift = abs(float(n) - float(o)) / denom
            if drift > max_drift_seen:
                max_drift_seen = drift
            if drift > max_pct:
                offending = path
                return False
            return True
        # Non-numeric or unmatched types — skip silently. Callers expecting type
        # conformance should validate elsewhere.
        return True

    ok = _walk(old, new, "")
    return ok, max_drift_seen, offending


# ---------------------------------------------------------------------------
# Deterministic holdout split
# ---------------------------------------------------------------------------


@dataclass
class HoldoutSplit:
    train: List[Any]
    holdout: List[Any]


def _stable_hash(value: Any, seed: int) -> int:
    """Stable hash via sha256 — independent of Python's hash randomization.

    Records are dict-like; we serialize in a canonical order so the split is
    deterministic across processes and Python versions.
    """
    if isinstance(value, Mapping):
        # Use event_id when present (the calibration query's natural key), else
        # fall back to a stable JSON-style serialization of all key/value pairs.
        if "event_id" in value:
            payload = str(value["event_id"])
        else:
            items = sorted(
                (str(k), str(v)) for k, v in value.items()
            )
            payload = "|".join(f"{k}={v}" for k, v in items)
    else:
        payload = str(value)
    digest = hashlib.sha256(f"{seed}:{payload}".encode("utf-8")).hexdigest()
    return int(digest[:16], 16)


def holdout_split(
    records: Sequence[Any],
    *,
    seed: int = DEFAULT_HOLDOUT_SEED,
    test_frac: float = DEFAULT_HOLDOUT_FRAC,
) -> HoldoutSplit:
    """Deterministic 1-(test_frac)/test_frac split.

    Deterministic across runs and Python versions: each record's bucket is
    derived from sha256(seed || stable_repr(record)). Records with `event_id`
    use it as the natural key; otherwise the full sorted-key serialization is
    used. Same seed + same record set → same partition.
    """
    if not (0.0 < test_frac < 1.0):
        raise ValueError(f"holdout_split: test_frac must be in (0, 1), got {test_frac}")
    train: List[Any] = []
    holdout: List[Any] = []
    threshold = int(test_frac * (1 << 64))
    for r in records:
        bucket = _stable_hash(r, seed) & ((1 << 64) - 1)
        if bucket < threshold:
            holdout.append(r)
        else:
            train.append(r)
    return HoldoutSplit(train=train, holdout=holdout)


# ---------------------------------------------------------------------------
# Candidate generation (grid search)
# ---------------------------------------------------------------------------


def generate_prior_candidates(
    priors_by_indication: Mapping[str, float],
    designation_modifiers: Mapping[str, float],
    *,
    prior_step_pp: float = 0.05,
    modifier_step: float = 0.02,
) -> Iterable[Tuple[Dict[str, float], Dict[str, float]]]:
    """Yield (priors, modifiers) candidates with one parameter perturbed at a time.

    Includes the unchanged baseline as the first candidate. Each subsequent
    candidate moves a single value by ±step. With 20 indications + 6 modifiers
    that's 1 + (2 × 20) + (2 × 6) = 53 candidates — well under the 200-cap
    bound the plan calls out. Multi-variable perturbations are out of scope
    (combinatorial explosion).

    All values are clamped to [0, 1] so probabilities stay valid; modifiers can
    range freely (compose_features already clamps the resulting fair_p).
    """
    yield (dict(priors_by_indication), dict(designation_modifiers))

    for indication, value in priors_by_indication.items():
        for delta in (+prior_step_pp, -prior_step_pp):
            new_value = max(0.0, min(1.0, float(value) + delta))
            if new_value == float(value):
                continue
            new_priors = dict(priors_by_indication)
            new_priors[indication] = new_value
            yield (new_priors, dict(designation_modifiers))

    for modifier, value in designation_modifiers.items():
        for delta in (+modifier_step, -modifier_step):
            new_priors = dict(priors_by_indication)
            new_modifiers = dict(designation_modifiers)
            new_modifiers[modifier] = float(value) + delta
            yield (new_priors, new_modifiers)


def generate_threshold_candidates(
    band_thresholds: Mapping[str, float],
    *,
    step_pct: float = 0.05,
) -> Iterable[Dict[str, float]]:
    """Yield candidate band_thresholds with one threshold perturbed at a time.

    Preserves the threshold ordering invariant: immediate > watchlist > archive.
    """
    yield dict(band_thresholds)

    for band, value in band_thresholds.items():
        for delta in (+step_pct, -step_pct):
            new_value = float(value) * (1.0 + delta)
            new_thresholds = dict(band_thresholds)
            new_thresholds[band] = new_value
            if _thresholds_well_ordered(new_thresholds):
                yield new_thresholds


def _thresholds_well_ordered(thresholds: Mapping[str, float]) -> bool:
    """Return True iff immediate > watchlist > archive (when all are present)."""
    imm = thresholds.get("immediate")
    wl = thresholds.get("watchlist")
    arc = thresholds.get("archive")
    if imm is not None and wl is not None and imm <= wl:
        return False
    if wl is not None and arc is not None and wl <= arc:
        return False
    return True


# ---------------------------------------------------------------------------
# Guardrail composite check
# ---------------------------------------------------------------------------


@dataclass
class GuardrailReport:
    passed: bool
    reasons: List[str]
    sample_size: int
    holdout_brier_old: Optional[float]
    holdout_brier_new: Optional[float]
    brier_relative_gain: Optional[float]
    max_param_drift_pct: Optional[float]
    drift_offender: Optional[str]


def evaluate_guardrails(
    *,
    sample_size: int,
    holdout_brier_old: float,
    holdout_brier_new: float,
    drift_ok: bool,
    drift_pct: float,
    drift_offender: Optional[str],
    min_sample: int = DEFAULT_MIN_SAMPLE_SIZE,
    min_relative_gain: float = DEFAULT_BRIER_RELATIVE_GAIN,
) -> GuardrailReport:
    """All-or-nothing pass/fail report on the four Phase 6 guardrails.

    1. sample_size >= min_sample
    2. drift_ok (every parameter ≤ DEFAULT_MAX_DRIFT_PCT vs prior version)
    3. holdout_brier_new < holdout_brier_old (strict improvement)
    4. brier_relative_gain >= min_relative_gain
    """
    reasons: List[str] = []

    if sample_size < min_sample:
        reasons.append(f"insufficient_sample (n={sample_size}, min={min_sample})")

    if not drift_ok:
        reasons.append(
            f"drift_exceeded ({drift_pct:.4f} > {DEFAULT_MAX_DRIFT_PCT}, path={drift_offender!r})"
        )

    if holdout_brier_new >= holdout_brier_old:
        reasons.append(
            f"no_brier_improvement (new={holdout_brier_new:.6f} >= old={holdout_brier_old:.6f})"
        )

    if holdout_brier_old <= 0:
        relative_gain = None
    else:
        relative_gain = (holdout_brier_old - holdout_brier_new) / holdout_brier_old
        if relative_gain < min_relative_gain:
            reasons.append(
                f"insufficient_relative_gain ({relative_gain:.4f} < {min_relative_gain})"
            )

    return GuardrailReport(
        passed=len(reasons) == 0,
        reasons=reasons,
        sample_size=sample_size,
        holdout_brier_old=holdout_brier_old,
        holdout_brier_new=holdout_brier_new,
        brier_relative_gain=relative_gain,
        max_param_drift_pct=drift_pct,
        drift_offender=drift_offender,
    )
