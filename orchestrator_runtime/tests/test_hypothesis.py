"""Tests for orchestrator_runtime.hypothesis — Stage 2 enumeration validator
+ D-118 prior renormalization.

Run: python -m pytest orchestrator_runtime/tests/test_hypothesis.py -v
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import pytest

# Tests don't make API calls but module imports require these env vars to be
# set (some downstream modules read SUPABASE_URL etc. at import time).
os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

from modal_workers.shared.compute import BaseRateResult, Stage4Anchor
from orchestrator_runtime.hypothesis import (
    Hypothesis,
    MIN_ANCHOR_WEIGHT,
    _validate_and_parse_hypotheses,
    renormalize_priors,
)


# ---------------------------------------------------------------------------
# Validator: malformed JSON / missing fields / required labels
# ---------------------------------------------------------------------------


def _fact_set(*shorts: str) -> set[str]:
    return {s.lower() for s in shorts}


def test_validator_rejects_non_dict():
    hyps, findings = _validate_and_parse_hypotheses(None, _fact_set())
    assert hyps == []
    assert any(f.check == "parse_failure" and f.severity == "error" for f in findings)


def test_validator_rejects_non_list_hypotheses_field():
    hyps, findings = _validate_and_parse_hypotheses(
        {"hypotheses": "not a list"}, _fact_set())
    assert hyps == []
    assert any(f.check == "parse_failure" for f in findings)


def test_validator_requires_bull_base_bear():
    """Missing one of the required {bull, base, bear} labels → error."""
    parsed = {"hypotheses": [
        {"hypothesis_id": "H1", "label": "bull", "claim": "c", "mechanism": "m",
         "direction": "bullish", "kill_conditions": ["k1", "k2"],
         "prior_estimate_pct": 50},
        {"hypothesis_id": "H2", "label": "base", "claim": "c", "mechanism": "m",
         "direction": "event_specific", "kill_conditions": ["k1", "k2"],
         "prior_estimate_pct": 30},
        # NO 'bear' — should fail
    ]}
    _, findings = _validate_and_parse_hypotheses(parsed, _fact_set())
    miss = [f for f in findings if f.check == "missing_required_label"]
    assert len(miss) == 1
    assert "bear" in miss[0].detail


def test_validator_requires_at_least_3_hypotheses():
    parsed = {"hypotheses": [
        {"hypothesis_id": "H1", "label": "bull", "claim": "c", "mechanism": "m",
         "direction": "bullish", "kill_conditions": ["k1", "k2"],
         "prior_estimate_pct": 50},
    ]}
    _, findings = _validate_and_parse_hypotheses(parsed, _fact_set())
    too_few = [f for f in findings if f.check == "too_few_hypotheses"]
    assert len(too_few) == 1


def test_validator_requires_2_kill_conditions():
    """A hypothesis with <2 kill_conditions should emit a severity=error."""
    parsed = {"hypotheses": [
        {"hypothesis_id": "H1", "label": "bull", "claim": "c", "mechanism": "m",
         "direction": "bullish", "kill_conditions": ["k1"],  # only 1
         "prior_estimate_pct": 50},
        {"hypothesis_id": "H2", "label": "base", "claim": "c", "mechanism": "m",
         "direction": "event_specific", "kill_conditions": ["k1", "k2"],
         "prior_estimate_pct": 30},
        {"hypothesis_id": "H3", "label": "bear", "claim": "c", "mechanism": "m",
         "direction": "bearish", "kill_conditions": ["k1", "k2"],
         "prior_estimate_pct": 20},
    ]}
    _, findings = _validate_and_parse_hypotheses(parsed, _fact_set())
    missing_kc = [f for f in findings if f.check == "missing_kill_conditions"]
    assert len(missing_kc) == 1
    assert missing_kc[0].severity == "error"
    assert missing_kc[0].affected_id == "H1"


def test_validator_clamps_prior_out_of_bounds():
    parsed = {"hypotheses": [
        {"hypothesis_id": "H1", "label": "bull", "claim": "c", "mechanism": "m",
         "direction": "bullish", "kill_conditions": ["k1", "k2"],
         "prior_estimate_pct": 250},  # OOB high
        {"hypothesis_id": "H2", "label": "base", "claim": "c", "mechanism": "m",
         "direction": "event_specific", "kill_conditions": ["k1", "k2"],
         "prior_estimate_pct": -50},  # OOB low
        {"hypothesis_id": "H3", "label": "bear", "claim": "c", "mechanism": "m",
         "direction": "bearish", "kill_conditions": ["k1", "k2"],
         "prior_estimate_pct": "not-a-number"},  # type error
    ]}
    hyps, _ = _validate_and_parse_hypotheses(parsed, _fact_set())
    assert hyps[0].prior_estimate_pct == 100
    assert hyps[1].prior_estimate_pct == 0
    assert hyps[2].prior_estimate_pct == 50  # default on parse failure


def test_validator_d115_base_missing_direction_does_not_default_bullish():
    """D-117: when label='base' has no valid direction, default to
    'event_specific' (NOT 'bullish'); emit a warning finding."""
    parsed = {"hypotheses": [
        {"hypothesis_id": "H1", "label": "bull", "claim": "c", "mechanism": "m",
         "direction": "bullish", "kill_conditions": ["k1", "k2"],
         "prior_estimate_pct": 50},
        {"hypothesis_id": "H2", "label": "base", "claim": "c", "mechanism": "m",
         "direction": "INVALID",  # missing — must coerce
         "kill_conditions": ["k1", "k2"], "prior_estimate_pct": 30},
        {"hypothesis_id": "H3", "label": "bear", "claim": "c", "mechanism": "m",
         "direction": "bearish", "kill_conditions": ["k1", "k2"],
         "prior_estimate_pct": 20},
    ]}
    hyps, findings = _validate_and_parse_hypotheses(parsed, _fact_set())
    base = next(h for h in hyps if h.label == "base")
    assert base.direction == "event_specific", \
        "D-117: silent base→bullish coercion is forbidden"
    coerce_findings = [f for f in findings if f.check == "missing_direction_for_base"]
    assert len(coerce_findings) == 1


def test_validator_unresolved_fact_id_warns():
    """A supporting/contradicting fact_id not in the assessment's fact set
    emits a warning (Stage 7 promotes to error)."""
    parsed = {"hypotheses": [
        {"hypothesis_id": "H1", "label": "bull", "claim": "c", "mechanism": "m",
         "direction": "bullish", "kill_conditions": ["k1", "k2"],
         "supporting_fact_ids": ["aabbccdd", "ZZZZZZZZ"],  # second won't resolve
         "prior_estimate_pct": 50},
        {"hypothesis_id": "H2", "label": "base", "claim": "c", "mechanism": "m",
         "direction": "event_specific", "kill_conditions": ["k1", "k2"],
         "prior_estimate_pct": 30},
        {"hypothesis_id": "H3", "label": "bear", "claim": "c", "mechanism": "m",
         "direction": "bearish", "kill_conditions": ["k1", "k2"],
         "prior_estimate_pct": 20},
    ]}
    _, findings = _validate_and_parse_hypotheses(parsed, _fact_set("aabbccdd"))
    assert any(f.check == "unresolved_supporting_fact_id" for f in findings)


def test_validator_caps_at_5_hypotheses():
    parsed = {"hypotheses": [
        {"hypothesis_id": f"H{i}", "label": "bull" if i == 1 else
         ("base" if i == 2 else ("bear" if i == 3 else "event_specific")),
         "claim": "c", "mechanism": "m",
         "direction": "bullish" if i == 1 else
         ("event_specific" if i == 2 else
          ("bearish" if i == 3 else "event_specific")),
         "kill_conditions": ["k1", "k2"], "prior_estimate_pct": 20}
        for i in range(1, 8)  # 7 hypotheses
    ]}
    hyps, _ = _validate_and_parse_hypotheses(parsed, _fact_set())
    assert len(hyps) == 5  # capped


def test_validator_records_pre_anchor_prior():
    """D-118: prior_estimate_pct_pre_anchor snapshots the model output before
    renormalize_priors runs."""
    parsed = {"hypotheses": [
        {"hypothesis_id": "H1", "label": "bull", "claim": "c", "mechanism": "m",
         "direction": "bullish", "kill_conditions": ["k1", "k2"],
         "prior_estimate_pct": 70},
        {"hypothesis_id": "H2", "label": "base", "claim": "c", "mechanism": "m",
         "direction": "event_specific", "kill_conditions": ["k1", "k2"],
         "prior_estimate_pct": 20},
        {"hypothesis_id": "H3", "label": "bear", "claim": "c", "mechanism": "m",
         "direction": "bearish", "kill_conditions": ["k1", "k2"],
         "prior_estimate_pct": 10},
    ]}
    hyps, _ = _validate_and_parse_hypotheses(parsed, _fact_set())
    for h in hyps:
        assert h.prior_estimate_pct_pre_anchor == h.prior_estimate_pct


# ---------------------------------------------------------------------------
# D-118: renormalize_priors
# ---------------------------------------------------------------------------


def _make_hyps(b: int, m: int, r: int) -> List[Hypothesis]:
    return [
        Hypothesis("H1", "bull", "c", "m", "bullish",
                   prior_estimate_pct=b, prior_estimate_pct_pre_anchor=b),
        Hypothesis("H2", "base", "c", "m", "event_specific",
                   prior_estimate_pct=m, prior_estimate_pct_pre_anchor=m),
        Hypothesis("H3", "bear", "c", "m", "bearish",
                   prior_estimate_pct=r, prior_estimate_pct_pre_anchor=r),
    ]


def _anchor(rate: float) -> Stage4Anchor:
    return Stage4Anchor(
        reference_class="x",
        base_rate=BaseRateResult(reference_class="x", n_cases=100,
                                 approval_rate=rate),
        similar_cases=[],
    )


def test_renormalize_pulls_bull_down_when_base_rate_is_low():
    hyps = _make_hyps(70, 20, 10)
    _, dbg = renormalize_priors(hyps, _anchor(0.30), evidence_quality=0.5)
    assert dbg["applied"] is True
    assert dbg["post_priors"][0] < dbg["pre_priors"][0], "bull should drop"
    assert dbg["post_priors"][2] > dbg["pre_priors"][2], "bear should rise"


def test_renormalize_pulls_bull_up_when_base_rate_is_high():
    hyps = _make_hyps(30, 20, 50)
    _, dbg = renormalize_priors(hyps, _anchor(0.80), evidence_quality=0.5)
    assert dbg["post_priors"][0] > dbg["pre_priors"][0], "bull should rise"
    assert dbg["post_priors"][2] < dbg["pre_priors"][2], "bear should drop"


def test_renormalize_sum_is_near_100():
    hyps = _make_hyps(70, 20, 10)
    _, dbg = renormalize_priors(hyps, _anchor(0.30), evidence_quality=0.5)
    assert 95 <= sum(dbg["post_priors"]) <= 105


def test_renormalize_high_evidence_quality_floors_at_min_weight():
    """High evidence_quality should still apply ≥ MIN_ANCHOR_WEIGHT pull."""
    hyps = _make_hyps(70, 20, 10)
    _, dbg = renormalize_priors(hyps, _anchor(0.30), evidence_quality=0.95)
    assert dbg["blend_weight"] == MIN_ANCHOR_WEIGHT
    # Bull still moves toward 30 even with high evidence quality
    assert dbg["post_priors"][0] < dbg["pre_priors"][0]


def test_renormalize_no_anchor_is_identity():
    hyps = _make_hyps(70, 20, 10)
    out_hyps, dbg = renormalize_priors(hyps, None, evidence_quality=0.5)
    assert dbg["applied"] is False
    assert all(h.prior_estimate_pct == h.prior_estimate_pct_pre_anchor
               for h in out_hyps)


def test_renormalize_no_base_rate_is_identity():
    hyps = _make_hyps(70, 20, 10)
    no_br = Stage4Anchor(reference_class="x", base_rate=None, similar_cases=[])
    out_hyps, dbg = renormalize_priors(hyps, no_br, evidence_quality=0.5)
    assert dbg["applied"] is False
    assert all(h.prior_estimate_pct == h.prior_estimate_pct_pre_anchor
               for h in out_hyps)


def test_renormalize_evidence_quality_none_uses_default():
    """evidence_quality=None should fall back to a sane default (0.5)."""
    hyps = _make_hyps(70, 20, 10)
    _, dbg = renormalize_priors(hyps, _anchor(0.30), evidence_quality=None)
    assert dbg["applied"] is True
    assert dbg["evidence_quality"] == 0.5


def test_renormalize_evidence_quality_invalid_falls_back():
    hyps = _make_hyps(70, 20, 10)
    _, dbg = renormalize_priors(hyps, _anchor(0.30),
                                evidence_quality="not-a-float")  # type: ignore
    assert dbg["applied"] is True
    assert dbg["evidence_quality"] == 0.5


def test_renormalize_empty_hypotheses_safe():
    out_hyps, dbg = renormalize_priors([], _anchor(0.30), 0.5)
    assert out_hyps == []
    assert dbg["applied"] is False
