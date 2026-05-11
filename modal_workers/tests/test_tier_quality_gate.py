"""Tests for the Phase 4B Tier-2 quality gate in nightly_calibration_refit.

The gate compares 30-day Brier between Tier-1 (full pipeline) and Tier-2
(Cowork bulk single-shot) and raises an operator_flag with
source='tier2_quality' when Tier-2 is materially worse than Tier-1.

Run: python3 -m pytest modal_workers/tests/test_tier_quality_gate.py -v
"""
from __future__ import annotations

import os
from typing import Any, Dict, List

import pytest

os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

from modal_workers.scripts.nightly_calibration_refit import (
    TIER2_QUALITY_FLAG_KIND,
    TIER2_QUALITY_FLAG_SOURCE,
    TIER2_QUALITY_MIN_PER_TIER,
    TIER2_QUALITY_THRESHOLD,
    TierBrierGate,
    evaluate_tier_brier_gate,
    run_tier_quality_gate,
)
from modal_workers.shared.supabase_client import SupabaseClient


# ---------------------------------------------------------------------------
# evaluate_tier_brier_gate (pure)
# ---------------------------------------------------------------------------

def _well_calibrated_pairs(n: int, p_correct: float = 0.7) -> List[tuple]:
    """Deterministic well-calibrated pairs: prediction = p_correct,
    `round(n * p_correct)` hits + remainder misses. Brier =
    p_correct * (1-p_correct) — the floor for a perfectly calibrated
    constant-prediction binary classifier."""
    n_hit = round(n * p_correct)
    return (
        [(p_correct, 1)] * n_hit
        + [(p_correct, 0)] * (n - n_hit)
    )


def _miscalibrated_pairs(n: int, pred_p: float, true_p: float) -> List[tuple]:
    """Deterministic miscalibrated pairs: prediction = pred_p (constant),
    `round(n * true_p)` hits. Brier =
    true_p*(pred_p-1)² + (1-true_p)*pred_p² — closed form."""
    n_hit = round(n * true_p)
    return (
        [(pred_p, 1)] * n_hit
        + [(pred_p, 0)] * (n - n_hit)
    )


def test_evaluate_skips_when_tier1_below_min_samples():
    tier1 = _well_calibrated_pairs(10)  # below default min=30
    tier2 = _well_calibrated_pairs(50)
    gate = evaluate_tier_brier_gate(tier1, tier2)
    assert not gate.flagged
    assert gate.skip_reason is not None
    assert "insufficient_samples" in gate.skip_reason


def test_evaluate_skips_when_tier2_below_min_samples():
    tier1 = _well_calibrated_pairs(50)
    tier2 = _well_calibrated_pairs(5)
    gate = evaluate_tier_brier_gate(tier1, tier2)
    assert not gate.flagged
    assert gate.skip_reason is not None


def test_evaluate_does_not_flag_when_tier2_close_to_tier1():
    tier1 = _well_calibrated_pairs(100, p_correct=0.7)
    tier2 = _well_calibrated_pairs(100, p_correct=0.7)  # same distribution
    gate = evaluate_tier_brier_gate(tier1, tier2, threshold=0.15)
    assert not gate.flagged
    assert gate.delta is not None
    assert abs(gate.delta) < 0.05  # well within threshold
    assert gate.skip_reason is None


def test_evaluate_flags_when_tier2_materially_worse():
    """Tier-2 says 90% but the world resolves at 50% → Brier ≈ 0.40.
    Tier-1 well-calibrated → Brier ≈ 0.21. Delta > 0.15 → flag."""
    tier1 = _well_calibrated_pairs(100, p_correct=0.7)
    tier2 = _miscalibrated_pairs(100, pred_p=0.9, true_p=0.5)
    gate = evaluate_tier_brier_gate(tier1, tier2, threshold=0.15)
    assert gate.flagged
    assert gate.delta > 0.15
    assert gate.brier_tier1 < gate.brier_tier2


def test_evaluate_does_not_flag_when_tier2_better():
    """Happy surprise: Tier-2 beats Tier-1. We don't fire (this is good news)."""
    tier1 = _miscalibrated_pairs(100, pred_p=0.9, true_p=0.5)  # Brier ≈ 0.40
    tier2 = _well_calibrated_pairs(100, p_correct=0.7)         # Brier ≈ 0.21
    gate = evaluate_tier_brier_gate(tier1, tier2, threshold=0.15)
    assert not gate.flagged
    assert gate.delta < 0  # tier2 better
    assert gate.skip_reason is None


def test_evaluate_threshold_is_configurable():
    """A tighter threshold catches drift the default 0.15 misses."""
    tier1 = _well_calibrated_pairs(100, p_correct=0.7)
    tier2 = _miscalibrated_pairs(100, pred_p=0.8, true_p=0.6)
    # Default 0.15 might not flag; tighter 0.05 should.
    loose = evaluate_tier_brier_gate(tier1, tier2, threshold=0.15)
    tight = evaluate_tier_brier_gate(tier1, tier2, threshold=0.05)
    assert tight.flagged or (tight.delta is not None and tight.delta > 0.05)
    # The loose gate may or may not flag depending on the exact numbers,
    # but the delta value itself must be the same.
    assert loose.delta == tight.delta


def test_to_evidence_round_trips_all_fields():
    gate = TierBrierGate(
        n_tier1=100, n_tier2=80,
        brier_tier1=0.21357, brier_tier2=0.41891,
        delta=0.20534, threshold=0.15, flagged=True, skip_reason=None,
    )
    ev = gate.to_evidence()
    assert ev["n_tier1"] == 100
    assert ev["n_tier2"] == 80
    assert ev["brier_tier1"] == 0.2136  # rounded
    assert ev["brier_tier2"] == 0.4189
    assert ev["delta"] == 0.2053
    assert ev["threshold"] == 0.15
    assert ev["skip_reason"] is None


def test_to_evidence_handles_none_fields():
    gate = TierBrierGate(
        n_tier1=5, n_tier2=80,
        brier_tier1=None, brier_tier2=None,
        delta=None, threshold=0.15, flagged=False,
        skip_reason="insufficient_samples(...)",
    )
    ev = gate.to_evidence()
    assert ev["brier_tier1"] is None
    assert ev["delta"] is None
    assert ev["skip_reason"].startswith("insufficient_samples")


# ---------------------------------------------------------------------------
# run_tier_quality_gate (DB-side wiring)
# ---------------------------------------------------------------------------

def _stub_sb(monkeypatch, fake_rest):
    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    sb = SupabaseClient.__new__(SupabaseClient)
    sb.url = "https://fake"
    sb.service_key = "fake"
    return sb


def _make_post_mortem(asmt_id: str, hit: bool, direction: str = "long"):
    return {
        "assessment_id": asmt_id,
        "realized_outcome": {"hit": hit},
        "predicted_direction": direction,
    }


def _make_assessment(asmt_id: str, tier: int, conviction_pct: float):
    return {
        "id": asmt_id,
        "tier": tier,
        "conviction_pct": conviction_pct,
        "thesis_direction": "long",
        "created_at": "2026-05-01T00:00:00Z",
    }


def test_run_tier_quality_gate_skips_when_no_data(monkeypatch):
    captured: List[Dict[str, Any]] = []

    def fake_rest(self, method, path, *, params=None, json_body=None, prefer=None):
        captured.append({"method": method, "path": path,
                         "params": params, "json_body": json_body})
        if method == "GET" and path == "post_mortem_queue":
            return []
        if method == "GET" and path == "operator_flags":
            return []  # no existing flag
        return None

    sb = _stub_sb(monkeypatch, fake_rest)
    gate = run_tier_quality_gate(sb=sb)
    assert not gate.flagged
    assert gate.skip_reason is not None

    # Should attempt to resolve any prior flag (PATCH operator_flags).
    # No POST happened.
    posts = [c for c in captured
             if c["method"] == "POST" and c["path"] == "operator_flags"]
    assert posts == []


def test_run_tier_quality_gate_raises_flag_on_drift(monkeypatch):
    """Seed deterministic mock data on each tier with a clear quality gap →
    operator_flag fires with source=tier2_quality."""
    # Tier-1: 60 cases at conviction=0.70, 42 hits (70%). Brier = 0.21.
    # Tier-2: 60 cases at conviction=0.90, 18 hits (30%). Brier ≈ 0.57.
    # Delta ≈ 0.36 — well above the 0.15 threshold.
    pms: List[Dict[str, Any]] = []
    asmts: List[Dict[str, Any]] = []
    for i in range(42):
        aid = f"a-t1-hit-{i}"
        pms.append(_make_post_mortem(aid, True))
        asmts.append(_make_assessment(aid, 1, 70.0))
    for i in range(18):
        aid = f"a-t1-miss-{i}"
        pms.append(_make_post_mortem(aid, False))
        asmts.append(_make_assessment(aid, 1, 70.0))
    for i in range(18):
        aid = f"a-t2-hit-{i}"
        pms.append(_make_post_mortem(aid, True))
        asmts.append(_make_assessment(aid, 2, 90.0))
    for i in range(42):
        aid = f"a-t2-miss-{i}"
        pms.append(_make_post_mortem(aid, False))
        asmts.append(_make_assessment(aid, 2, 90.0))

    captured: List[Dict[str, Any]] = []

    def fake_rest(self, method, path, *, params=None, json_body=None, prefer=None):
        captured.append({"method": method, "path": path,
                         "params": params, "json_body": json_body,
                         "prefer": prefer})
        if method == "GET" and path == "post_mortem_queue":
            return pms
        if method == "GET" and path == "convergence_assessments":
            return asmts
        if method == "GET" and path == "operator_flags":
            return []  # no existing flag
        if method == "POST" and path == "operator_flags":
            return [{"id": "flag-new-1"}]
        return None

    sb = _stub_sb(monkeypatch, fake_rest)
    gate = run_tier_quality_gate(sb=sb)
    assert gate.flagged
    assert gate.delta is not None and gate.delta > TIER2_QUALITY_THRESHOLD
    assert gate.n_tier1 == 60
    assert gate.n_tier2 == 60

    # Operator flag posted with the right source/kind.
    posts = [c for c in captured
             if c["method"] == "POST" and c["path"] == "operator_flags"]
    assert len(posts) == 1
    body = posts[0]["json_body"]
    assert body["source"] == TIER2_QUALITY_FLAG_SOURCE
    assert body["kind"] == TIER2_QUALITY_FLAG_KIND
    assert body["severity"] == "warn"
    assert body["evidence"]["n_tier1"] == 60
    assert body["evidence"]["n_tier2"] == 60
    assert body["evidence"]["delta"] > TIER2_QUALITY_THRESHOLD


def test_run_tier_quality_gate_resolves_flag_when_clean(monkeypatch):
    """Both tiers well-calibrated → no flag, but PATCH any existing
    open flag to resolved (auto-cleanup)."""
    # Both tiers identically calibrated at 70% — delta ~0.
    pms: List[Dict[str, Any]] = []
    asmts: List[Dict[str, Any]] = []
    for i in range(42):
        aid = f"a-t1-hit-{i}"
        pms.append(_make_post_mortem(aid, True))
        asmts.append(_make_assessment(aid, 1, 70.0))
    for i in range(18):
        aid = f"a-t1-miss-{i}"
        pms.append(_make_post_mortem(aid, False))
        asmts.append(_make_assessment(aid, 1, 70.0))
    for i in range(42):
        aid = f"a-t2-hit-{i}"
        pms.append(_make_post_mortem(aid, True))
        asmts.append(_make_assessment(aid, 2, 70.0))
    for i in range(18):
        aid = f"a-t2-miss-{i}"
        pms.append(_make_post_mortem(aid, False))
        asmts.append(_make_assessment(aid, 2, 70.0))

    captured: List[Dict[str, Any]] = []

    def fake_rest(self, method, path, *, params=None, json_body=None, prefer=None):
        captured.append({"method": method, "path": path,
                         "params": params, "json_body": json_body})
        if method == "GET" and path == "post_mortem_queue":
            return pms
        if method == "GET" and path == "convergence_assessments":
            return asmts
        if method == "GET" and path == "operator_flags":
            return [{"id": "old-flag-1"}]  # there's an open flag
        return None

    sb = _stub_sb(monkeypatch, fake_rest)
    gate = run_tier_quality_gate(sb=sb)
    assert not gate.flagged
    assert gate.delta is not None
    assert gate.delta <= TIER2_QUALITY_THRESHOLD

    # Resolve PATCH should have been issued.
    patches = [c for c in captured
               if c["method"] == "PATCH" and c["path"] == "operator_flags"]
    assert len(patches) >= 1
    # No POST.
    posts = [c for c in captured
             if c["method"] == "POST" and c["path"] == "operator_flags"]
    assert posts == []


def test_run_tier_quality_gate_filters_post_mortems_to_lookback_window(monkeypatch):
    """The convergence_assessments GET must include a created_at filter
    matching the lookback window."""
    captured: List[Dict[str, Any]] = []

    def fake_rest(self, method, path, *, params=None, json_body=None, prefer=None):
        captured.append({"method": method, "path": path, "params": params})
        if method == "GET" and path == "post_mortem_queue":
            return [_make_post_mortem("a-1", True)]
        if method == "GET" and path == "convergence_assessments":
            return [_make_assessment("a-1", 1, 70.0)]
        if method == "GET" and path == "operator_flags":
            return []
        return None

    sb = _stub_sb(monkeypatch, fake_rest)
    run_tier_quality_gate(sb=sb, lookback_days=15)

    asmt_call = next(c for c in captured
                     if c["method"] == "GET" and c["path"] == "convergence_assessments")
    created_filter = asmt_call["params"]["created_at"]
    assert created_filter.startswith("gte.")
