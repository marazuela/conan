"""F1b — temporal directional convergence (capability preservation).

Run: python -m pytest orchestrator_runtime/tests/test_directional_convergence.py -v

Locked spec: plan-all-pf-this-snuggly-galaxy.md, Workstream F1b.
Reuses rubric_engine.convergence_reference unchanged; this suite proves the
temporal adapter builds the right group and maps verdicts to modifiers,
including the persist-path regression guard (zero priors must never raise).
"""
from __future__ import annotations

import os

os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

from orchestrator_runtime.directional_convergence import (
    compute_directional_convergence,
)


def _prior(direction, conv=60.0, **kw):
    row = {"thesis_direction": direction, "conviction_pct": conv}
    row.update(kw)
    return row


# 1. ≥2 priors same direction → same_direction, positive modifier.
def test_repeated_agreement_corroborates():
    out = compute_directional_convergence(
        [_prior("long"), _prior("long")], "long", 70.0, "binary_catalyst")
    assert out["verdict"] == "same_direction"
    assert out["modifier_pp"] > 0
    assert out["contradiction"] is False
    # 3+ unique reads (current + 2 priors) → larger boost than the 2-read case.
    assert out["modifier_pp"] == 5.0


def test_two_reads_smaller_boost():
    out = compute_directional_convergence(
        [_prior("long")], "long", 70.0, "binary_catalyst")
    assert out["verdict"] == "same_direction"
    assert out["modifier_pp"] == 3.0


# 2. recent prior opposes current → contradiction, negative + flagged.
def test_direction_flip_contradiction():
    out = compute_directional_convergence(
        [_prior("short"), _prior("long")], "long", 65.0, "binary_catalyst")
    assert out["verdict"] == "contradiction"
    assert out["modifier_pp"] < 0
    assert out["contradiction"] is True


# 3. 0 priors (new asset) → single, no-op, MUST NOT raise (regression guard).
def test_no_priors_is_safe_noop():
    out = compute_directional_convergence([], "long", 80.0, "binary_catalyst")
    assert out["verdict"] == "single"
    assert out["modifier_pp"] == 0.0
    assert out["contradiction"] is False
    assert out["n_priors"] == 0


def test_none_priors_arg_is_safe():
    # Defensive: ctx.get("prior_assessments") may be None, not [].
    out = compute_directional_convergence(None, "short", 50.0, "binary_catalyst")
    assert out["verdict"] == "single"
    assert out["modifier_pp"] == 0.0


# 4. 1 prior, same direction is still 2 reads → corroborate; but a lone
#    prior with the SAME single read collapsing to one unique → single.
def test_single_read_no_op():
    # current neutral + no priors-equivalent: only the current read counts.
    out = compute_directional_convergence([], "neutral", 40.0, "binary_catalyst")
    assert out["verdict"] == "single"
    assert out["modifier_pp"] == 0.0


# 5. prior with NULL thesis_direction → ignored, not counted as opposing.
def test_null_prior_direction_ignored():
    out = compute_directional_convergence(
        [_prior(None), _prior("long")], "long", 70.0, "binary_catalyst")
    # No 'short' anywhere → not a contradiction; long agreement stands.
    assert out["verdict"] == "same_direction"
    assert out["contradiction"] is False


def test_neutral_current_does_not_collect_boost():
    # Neutral current riding prior agreement is not actionable signal.
    out = compute_directional_convergence(
        [_prior("long"), _prior("long")], "neutral", 55.0, "binary_catalyst")
    assert out["modifier_pp"] == 0.0


# 6. lookback bound respected (older priors beyond N excluded).
def test_lookback_bound_respected():
    priors = [_prior("long")] * 10
    out = compute_directional_convergence(
        priors, "long", 70.0, "binary_catalyst", lookback_n=3)
    assert out["n_priors"] == 3
    assert len(out["prior_ids"]) == 3


def test_weights_overridable():
    out = compute_directional_convergence(
        [_prior("short")], "long", 60.0, "binary_catalyst",
        weights={"contradiction": -20.0})
    assert out["verdict"] == "contradiction"
    assert out["modifier_pp"] == -20.0
