"""F1b — temporal directional convergence (v2 capability preservation).

v2's reactor cross-checked an issuer by classifying *independent same-moment
signals* (different scanners) for agreement/conflict. v3 has no such multi-
source directional feed: the only directional fields are
`convergence_assessments.thesis_direction` (one per run) and intra-run
`hypothesis_enumeration.direction` (scenario spread, already measured by
ensemble dispersion). The only multi-read signal extant without new
extraction infra is *temporal*: the same asset's thesis_direction across
successive runs.

This module reuses the existing, pure `rubric_engine.convergence_reference`
**unchanged**. Same asset over time ⇒ constant scoring_profile ⇒ the verdict
degrades correctly to:

    same_direction  : prior run(s) agree with this run   → corroborate (+)
    contradiction    : a prior run opposed this one        → penalize  (−) + flag
    single           : <2 directional reads / new asset     → no-op (0)

`orthogonal` cannot trigger here (single profile) — that is intentional and
why no algorithm change is needed.

LOCKED by eng review 2026-05-18 (plan-all-pf-this-snuggly-galaxy.md, F1b).
Contradiction PENALIZES + FLAGS; it never hard-caps (mirrors v2 `bonus=0`,
not a Stage-3 all_falsified kill).

Deferred (documented, gated on ORCH_ENABLE_SUB_AGENTS): true cross-source
convergence over per-document / sub-agent directional extraction.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from modal_workers.shared.rubric_engine import convergence_reference

# Modifier magnitudes in conviction percentage-points. Overridable by the
# caller (stage_10_persist may source these from internal_config later — see
# plan F1b "magnitudes a calibration param"). Defaults are deliberately small:
# this is a confidence nudge, not a thesis override.
DEFAULT_WEIGHTS: Dict[str, float] = {
    "corroborate_2": 3.0,   # same_direction, 2 unique reads
    "corroborate_3p": 5.0,  # same_direction, 3+ unique reads
    "contradiction": -8.0,  # a prior run opposed this run's direction
}

_DIRECTIONAL = {"long", "short"}


def compute_directional_convergence(
    prior_assessments: List[Dict[str, Any]],
    current_direction: Optional[str],
    current_conviction: float,
    asset_scoring_profile: str,
    *,
    lookback_n: int = 5,
    weights: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """Classify this run against prior runs on the same asset.

    Pure: no IO. `prior_assessments` are the rows already loaded by Stage 0
    context-load (newest-first, non-superseded). Never raises — a new asset
    (zero priors) returns a no-op verdict; this is a persist-path regression
    guard (the persist path must not fail because an asset has no history).
    """
    w = {**DEFAULT_WEIGHTS, **(weights or {})}

    # Build the convergence group: current run + the N most-recent priors.
    # signal_id is a synthetic per-read key (created_at); source_content_hash
    # is None so convergence_reference treats every read as unique (its
    # __no_hash__<signal_id> path) — no temporal dedup, which is correct.
    group: List[Dict[str, Any]] = [{
        "signal_id": "__current__",
        "scoring_profile": asset_scoring_profile,
        "thesis_direction": current_direction,
        "score": float(current_conviction or 0.0),
        "source_content_hash": None,
    }]
    prior_ids: List[str] = []
    for p in (prior_assessments or [])[:lookback_n]:
        key = str(p.get("id") or p.get("created_at") or len(prior_ids))
        prior_ids.append(key)
        group.append({
            "signal_id": key,
            "scoring_profile": asset_scoring_profile,
            "thesis_direction": p.get("thesis_direction"),
            "score": float(
                p.get("conviction_pct_calibrated")
                or p.get("conviction_pct") or 0.0),
            "source_content_hash": None,
        })

    verdict = convergence_reference(group)
    vtype = verdict["type"]
    n_unique = len(verdict.get("unique_signal_ids") or [])

    contradiction = vtype == "contradiction"
    if contradiction:
        modifier = w["contradiction"]
    elif vtype in ("same_direction", "orthogonal"):
        # Only award corroboration when this run is itself directional —
        # a neutral current read riding a prior's agreement is not signal.
        if current_direction in _DIRECTIONAL:
            modifier = w["corroborate_3p"] if n_unique >= 3 else w["corroborate_2"]
        else:
            modifier = 0.0
    else:  # single
        modifier = 0.0

    return {
        "verdict": vtype,
        "n_priors": len(prior_ids),
        "n_unique": n_unique,
        "modifier_pp": round(float(modifier), 2),
        "contradiction": contradiction,
        "prior_ids": prior_ids,
    }
