"""B4: Stage 8 isotonic calibration must run BEFORE the Stage 3 all_falsified
cap, not after. Previously stage_10_persist applied the cap to the raw value
via parsed["conviction_pct"] = capped (inside _run_one_inner), then calibrated
the already-capped value. Result: conviction_pct_calibrated was
isotonic(min(raw, 30)/100), not the correct min(isotonic(raw/100), 30).

These tests exercise the full _run_one_inner -> stage_10_persist path with
mocked stages, then inspect the row body POSTed to convergence_assessments
to assert the four conviction columns end up correct:

  - raw_conviction_pct          → pre-cap, pre-calibration (model output)
  - conviction_pct_calibrated   → calibrated raw, then capped if all_falsified
  - conviction_pct              → same as calibrated (band-driving value)
  - evidence_ledger.conviction_capped_by_premortem → boolean flag

Run: python -m pytest orchestrator_runtime/tests/test_stage_8_calibration_order.py -v
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

from orchestrator_runtime.hypothesis import Hypothesis, HypothesisResult
from orchestrator_runtime.premortem import HypothesisVerdict, PreMortemResult


# ---------------------------------------------------------------------------
# Shared fixtures — same pattern as test_runtime_stage_7_gate.py
# ---------------------------------------------------------------------------


def _baseline_ctx() -> Dict[str, Any]:
    return {
        "asset": {
            "id": "asset-uuid-1",
            "ticker": "VRDN",
            "drug_name": "Veligrotug",
            "generic_name": "veligrotug",
            "sponsor_name": "Viridian",
            "indication": "TED",
            "indication_normalized": "ted",
            "reference_class_signature": "phase3_oncology_breakthrough",
            "application_number": "BLA-1",
            "program_status": "submitted",
        },
        "facts": [
            {"id": "aa11bb22cc33dd44", "document_id": "dd44ee55ff66aabb",
             "fact_type": "trial_result", "fact_text": "Endpoint met",
             "evidence_quote": "p<0.001", "confidence": 0.95},
        ],
        "documents": [
            {"id": "dd44ee55ff66aabb", "source": "fixture", "doc_type": "primary"},
        ],
        "memory_text": None,
        "reference_class_anchor": None,
        "asset_doc_links": [
            {"document_id": "dd44ee55ff66aabb", "link_type": "primary", "is_material": True},
        ],
    }


def _stage4_anchor_stub():
    from modal_workers.shared.compute import Stage4Anchor
    from orchestrator_runtime.runtime import StageMetric
    anchor = Stage4Anchor(
        reference_class="phase3_oncology_breakthrough",
        base_rate=None, similar_cases=[],
    )
    return anchor, StageMetric(stage_name="stage_4_reference_class_anchor",
                               model="deterministic")


def _stage1_synth_stub():
    from orchestrator_runtime.runtime import StageMetric
    return (
        "## Conclusion\n- thesis_direction: long\n- conviction_pct: ?",
        StageMetric(stage_name="stage_1_synthesis",
                    model="claude-sonnet-4-5-20250929",
                    input_tokens=100, output_tokens=50, cost_usd=0.001),
    )


def _stage9_extract_stub(raw_conviction_pct: float):
    """Stage 9 returns a parsed payload with the given raw conviction."""
    from orchestrator_runtime.runtime import StageMetric
    parsed = {
        "thesis_direction": "long",
        "conviction_pct": raw_conviction_pct,
        "evidence_quality": 0.7,
        "cited_prose_blocks": [],
        "key_facts": [],
        "uncertainties": [],
        "thesis_summary": "summary",
    }
    metric = StageMetric(
        stage_name="stage_9_extraction",
        model="claude-sonnet-4-5-20250929",
        input_tokens=80, output_tokens=120, cost_usd=0.002,
        status="completed",
    )
    return parsed, metric


def _hypothesis_result_ok() -> HypothesisResult:
    return HypothesisResult(
        pass_=True,
        hypotheses=[
            Hypothesis("H1", "bull", "c", "m", "bullish",
                       kill_conditions=["k1", "k2"]),
            Hypothesis("H2", "base", "c", "m", "event_specific",
                       kill_conditions=["k1", "k2"]),
            Hypothesis("H3", "bear", "c", "m", "bearish",
                       kill_conditions=["k1", "k2"]),
        ],
    )


def _premortem_result(verdict: str) -> PreMortemResult:
    """`verdict` ∈ all_survive | partial | all_falsified."""
    surviving = ["H1", "H2", "H3"] if verdict != "all_falsified" else []
    return PreMortemResult(
        pass_=(verdict != "all_falsified"),
        overall_verdict=verdict,
        surviving_hypothesis_ids=surviving,
        verdicts=[
            HypothesisVerdict("H1", "survives"
                              if verdict == "all_survive" else "falsified"),
        ],
    )


def _constitutional_result_passing():
    from orchestrator_runtime.constitutional import ConstitutionalResult
    return ConstitutionalResult(pass_=True, findings=[],
                                n_citations_checked=0, n_citations_resolved=0)


class _MockSupabase:
    """Captures the POST body sent to convergence_assessments so tests can
    inspect conviction_pct, conviction_pct_calibrated, raw_conviction_pct.

    Wave 4 deep-fix Phase B — Stage 10 now POSTs a single RPC at
    rpc/persist_assessment_v3 with the assessment row nested under
    payload.payload.assessment. We unwrap it here so the existing assertions
    on `posted_assessment` keep working without per-test rewrites."""

    def __init__(self):
        self.posted_assessment: Optional[Dict[str, Any]] = None
        self.post_count: Dict[str, int] = {}
        self.posted_rpc_payload: Optional[Dict[str, Any]] = None

    def _rest(self, method: str, table: str, **kwargs) -> Any:
        # Track every POST so we can assert what's in each table
        if method == "POST":
            self.post_count[table] = self.post_count.get(table, 0) + 1
            if table == "rpc/persist_assessment_v3":
                # Capture the full RPC payload AND unwrap the inner assessment
                # row for backwards compatibility with assertions that target
                # `posted_assessment`.
                body = kwargs.get("json_body") or {}
                self.posted_rpc_payload = body
                inner = body.get("payload") or {}
                self.posted_assessment = inner.get("assessment") or {}
                # RPC returns the new assessment id as a bare uuid scalar.
                return "assessment-uuid-test"
            if table == "convergence_assessments":
                # Legacy path kept for any tests still mocking the direct
                # INSERT route — current Stage 10 uses the RPC above.
                self.posted_assessment = kwargs.get("json_body") or {}
                return [{"id": "assessment-uuid-test"}]
            return [{"id": f"{table}-row-id"}]
        if method == "GET":
            # Stub for the catalyst-event lookup in _resolve_catalyst_window
            if table == "fda_regulatory_events":
                return []
            return []
        return []


# ---------------------------------------------------------------------------
# Curve fixtures — synthetic isotonic curves with known transforms
# ---------------------------------------------------------------------------


def _curve_identity() -> Dict[str, Any]:
    """y = x — calibration is a no-op."""
    return {
        "version": "v-identity",
        "curve_data": {"knots": [{"x": 0.0, "y": 0.0}, {"x": 1.0, "y": 1.0}]},
    }


def _curve_halves() -> Dict[str, Any]:
    """y = x/2 — calibration halves input. raw=0.8 -> calibrated=0.4."""
    return {
        "version": "v-halves",
        "curve_data": {"knots": [{"x": 0.0, "y": 0.0}, {"x": 1.0, "y": 0.5}]},
    }


def _curve_doubles_then_cap() -> Dict[str, Any]:
    """y = 2x clamped at 1.0. raw=0.3 -> calibrated=0.6."""
    return {
        "version": "v-doubles",
        "curve_data": {"knots": [
            {"x": 0.0, "y": 0.0}, {"x": 0.5, "y": 1.0}, {"x": 1.0, "y": 1.0}
        ]},
    }


# ---------------------------------------------------------------------------
# Helpers — drive _run_one_inner end to end with mocks
# ---------------------------------------------------------------------------


def _run_pipeline(raw_conviction_pct: float, premortem_verdict: str,
                  active_curve: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Run _run_one_inner with all stages mocked. Returns the captured POST
    body to convergence_assessments (or raises if Stage 10 wasn't reached)."""
    from orchestrator_runtime import runtime
    from modal_workers.shared import compute as compute_mod

    sb = _MockSupabase()
    a_client = MagicMock()

    parsed_stub, m9 = _stage9_extract_stub(raw_conviction_pct)
    cited_prose, m1 = _stage1_synth_stub()

    with patch.object(runtime, "stage_0_load", return_value=_baseline_ctx()), \
         patch.object(runtime, "stage_4_anchor",
                      return_value=_stage4_anchor_stub()), \
         patch.object(runtime, "stage_1_synthesize",
                      return_value=(cited_prose, m1)), \
         patch.object(runtime, "stage_9_extract",
                      return_value=(parsed_stub, m9)), \
         patch.object(runtime, "run_hypothesis_enumeration",
                      return_value=_hypothesis_result_ok()), \
         patch.object(runtime, "run_premortem",
                      return_value=_premortem_result(premortem_verdict)), \
         patch.object(runtime, "run_constitutional_check",
                      return_value=_constitutional_result_passing()), \
         patch.object(runtime, "get_active_calibration_curve",
                      return_value=active_curve), \
         patch.object(compute_mod, "get_active_calibration_curve",
                      return_value=active_curve):
        aid = runtime._run_one_inner(
            sb, a_client,
            asset_id="asset-uuid-1", trigger_type="manual",
            model="claude-sonnet-4-5-20250929",
            extractor_model="claude-sonnet-4-5-20250929",
            ensemble_n=1, ensemble_mode="streaming",
            run_constitutional=True,
            constitutional_skip_semantic=True,
            enable_premortem=True,
            dry_run=False,
        )
    assert aid == "assessment-uuid-test"
    assert sb.posted_assessment is not None, "stage_10_persist did not POST"
    return sb.posted_assessment


# ---------------------------------------------------------------------------
# Test matrix: {all_survive, all_falsified} × {no curve, identity, halves}
# ---------------------------------------------------------------------------


def test_all_survive_no_curve_persists_raw_unchanged():
    """Baseline: no cap, no curve → raw == calibrated == final."""
    row = _run_pipeline(
        raw_conviction_pct=70.0,
        premortem_verdict="all_survive",
        active_curve=None,
    )
    assert row["raw_conviction_pct"] == 70.0
    assert row["conviction_pct_calibrated"] == 70.0
    assert row["conviction_pct"] == 70.0
    assert row["evidence_ledger"]["conviction_capped_by_premortem"] is False


def test_all_survive_with_halving_curve_applies_calibration_to_raw():
    """No cap, halving curve → calibrated = raw/2."""
    row = _run_pipeline(
        raw_conviction_pct=80.0,
        premortem_verdict="all_survive",
        active_curve=_curve_halves(),
    )
    assert row["raw_conviction_pct"] == 80.0
    # halves curve: 0.8 -> 0.4 -> 40.0
    assert row["conviction_pct_calibrated"] == pytest.approx(40.0, abs=1e-6)
    assert row["conviction_pct"] == row["conviction_pct_calibrated"]
    assert row["evidence_ledger"]["conviction_capped_by_premortem"] is False


def test_all_falsified_no_curve_caps_at_30():
    """all_falsified + no curve: raw flows to raw_conviction_pct;
    calibrated equals raw (identity); cap clamps both
    conviction_pct_calibrated and conviction_pct to 30."""
    row = _run_pipeline(
        raw_conviction_pct=80.0,
        premortem_verdict="all_falsified",
        active_curve=None,
    )
    assert row["raw_conviction_pct"] == 80.0  # pre-cap raw preserved
    assert row["conviction_pct_calibrated"] == 30.0  # capped
    assert row["conviction_pct"] == 30.0
    assert row["evidence_ledger"]["conviction_capped_by_premortem"] is True


def test_all_falsified_identity_curve_calibrates_then_caps():
    """B4 regression guard: with identity curve, the cap is applied AFTER
    calibration. Raw=70 -> isotonic(70)=70 -> cap to 30. Same numeric
    output as the buggy order in this special case, but the order of
    operations differs."""
    row = _run_pipeline(
        raw_conviction_pct=70.0,
        premortem_verdict="all_falsified",
        active_curve=_curve_identity(),
    )
    assert row["raw_conviction_pct"] == 70.0
    assert row["conviction_pct_calibrated"] == 30.0
    assert row["evidence_ledger"]["conviction_capped_by_premortem"] is True


def test_all_falsified_halving_curve_calibrates_below_cap_then_no_cap_binds():
    """The discriminating test: raw=80, halving curve calibrates to 40,
    cap to min(40, 30) = 30. (Under the old buggy order: cap raw first
    to 30, then halve to 15 — calibrated=15, NOT 30. This test would
    have produced 15 in the buggy code.)"""
    row = _run_pipeline(
        raw_conviction_pct=80.0,
        premortem_verdict="all_falsified",
        active_curve=_curve_halves(),
    )
    assert row["raw_conviction_pct"] == 80.0
    # Halves curve on raw 0.8 -> 0.4 -> 40.0, then cap -> 30.0.
    # Old buggy order would have yielded: cap 80->30, halve -> 15.0.
    assert row["conviction_pct_calibrated"] == pytest.approx(30.0, abs=1e-6)
    assert row["conviction_pct"] == pytest.approx(30.0, abs=1e-6)
    assert row["evidence_ledger"]["conviction_capped_by_premortem"] is True


def test_all_falsified_curve_lowers_below_cap_no_cap_needed():
    """raw=50, halving curve -> calibrated=25. Cap is 30; 25 < 30 so cap
    does NOT bind. Calibrated stays at 25.

    Sanity: in the OLD buggy code, this case would have produced:
      - cap raw 50 -> 30 (because all_falsified)
      - halve -> 15.0
    The new behavior — calibrated=25 (NOT capped) — surfaces the genuine
    model probability after calibration. This is the case that
    demonstrates the bug actively suppressed information."""
    row = _run_pipeline(
        raw_conviction_pct=50.0,
        premortem_verdict="all_falsified",
        active_curve=_curve_halves(),
    )
    assert row["raw_conviction_pct"] == 50.0
    # 0.5 -> halves curve linear knot (1.0,0.5) → 0.25 → 25.0
    assert row["conviction_pct_calibrated"] == pytest.approx(25.0, abs=1e-6)
    # Cap was set but didn't bind (25 < 30)
    assert row["evidence_ledger"]["conviction_capped_by_premortem"] is True
    assert row["conviction_pct"] == pytest.approx(25.0, abs=1e-6)


def test_calibration_curve_version_persists():
    """The curve version is stamped on the row regardless of whether the
    cap binds."""
    row = _run_pipeline(
        raw_conviction_pct=80.0,
        premortem_verdict="all_falsified",
        active_curve=_curve_identity(),
    )
    assert row["calibration_curve_version"] == "v-identity"
