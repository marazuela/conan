"""Stage 7 gate + Stage 9 parse-error abort + cache token propagation tests.

Covers:
  - B1: ConstitutionalFailure raised when constitutional_result.pass_=False;
        Stage 9 parse failure now raises Stage9ParseError instead of returning
        None (which the orchestrator_app drain loop silently marked
        'completed' with assessment_id=null).
  - B3: HypothesisResult / PreMortemResult propagate cache_read_tokens and
        cache_creation_tokens from the upstream OrchestratorClient call so
        StageMetric.cache_* rows in assessment_stage_metrics carry the real
        cache footprint.

Run: python -m pytest orchestrator_runtime/tests/test_runtime_stage_7_gate.py -v
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

from orchestrator_runtime.client import CallResult
from orchestrator_runtime.constitutional import (
    ConstitutionalFinding,
    ConstitutionalResult,
)
from orchestrator_runtime.hypothesis import (
    Hypothesis,
    HypothesisResult,
    run_hypothesis_enumeration,
)
from orchestrator_runtime.premortem import (
    HypothesisVerdict,
    PreMortemResult,
    run_premortem,
)
from orchestrator_runtime.runtime import (
    ConstitutionalFailure,
    Stage9ParseError,
)


# ---------------------------------------------------------------------------
# B1: Exception class shape
# ---------------------------------------------------------------------------


def test_constitutional_failure_is_exception_subclass():
    assert issubclass(ConstitutionalFailure, Exception)


def test_constitutional_failure_carries_findings():
    findings = [
        ConstitutionalFinding(severity="error", check="unresolved_fact_id",
                              detail="F:deadbeef not in fact set"),
    ]
    exc = ConstitutionalFailure(findings)
    assert exc.findings is findings
    assert "1 error finding" in str(exc)


def test_constitutional_failure_with_no_findings():
    exc = ConstitutionalFailure()
    assert exc.findings == []
    assert "0 error finding" in str(exc)


def test_constitutional_failure_custom_message():
    exc = ConstitutionalFailure(message="explicit message wins")
    assert str(exc) == "explicit message wins"


def test_stage_9_parse_error_is_exception_subclass():
    assert issubclass(Stage9ParseError, Exception)


def test_stage_9_parse_error_message():
    exc = Stage9ParseError("garbage payload")
    assert "garbage payload" in str(exc)


# ---------------------------------------------------------------------------
# B3: Cache tokens flow CallResult → HypothesisResult
# ---------------------------------------------------------------------------


def _mock_orchestrator_client_returning(call_result: CallResult) -> MagicMock:
    """Build a mock OrchestratorClient whose .call() returns call_result."""
    client = MagicMock()
    client.call.return_value = call_result
    return client


def _valid_hypothesis_response() -> str:
    """Minimum JSON to pass _validate_and_parse_hypotheses."""
    import json
    return json.dumps({
        "hypotheses": [
            {"hypothesis_id": "H1", "label": "bull", "claim": "c",
             "mechanism": "m", "direction": "bullish",
             "kill_conditions": ["k1", "k2"], "prior_estimate_pct": 50},
            {"hypothesis_id": "H2", "label": "base", "claim": "c",
             "mechanism": "m", "direction": "event_specific",
             "kill_conditions": ["k1", "k2"], "prior_estimate_pct": 30},
            {"hypothesis_id": "H3", "label": "bear", "claim": "c",
             "mechanism": "m", "direction": "bearish",
             "kill_conditions": ["k1", "k2"], "prior_estimate_pct": 20},
        ]
    })


def test_hypothesis_result_propagates_cache_tokens():
    """B3: CallResult.cache_read_tokens / cache_creation_tokens MUST surface
    on HypothesisResult so StageMetric.cache_* persists to the DB."""
    call_result = CallResult(
        text=_valid_hypothesis_response(),
        model="claude-sonnet-4-5-20250929",
        input_tokens=1000,
        output_tokens=500,
        thinking_tokens=0,
        cache_read_tokens=12345,
        cache_creation_tokens=678,
        cost_usd=0.012345,
        latency_ms=234,
    )
    client = _mock_orchestrator_client_returning(call_result)

    result = run_hypothesis_enumeration(
        client,
        cited_prose="P",
        parsed_json={"thesis_direction": "long"},
        ctx={"facts": []},
        model="claude-sonnet-4-5-20250929",
    )

    assert result.cache_read_tokens == 12345
    assert result.cache_creation_tokens == 678
    # Sanity: existing fields still wired
    assert result.input_tokens == 1000
    assert result.output_tokens == 500


def _valid_premortem_response_for(hypothesis_ids: List[str]) -> str:
    import json
    return json.dumps({
        "verdicts": [
            {"hypothesis_id": hid, "verdict": "survives",
             "failure_modes": [], "disconfirming_searches": [],
             "update_triggers": []}
            for hid in hypothesis_ids
        ],
        "overall_verdict": "all_survive",
        "surviving_hypothesis_ids": list(hypothesis_ids),
    })


def test_premortem_result_propagates_cache_tokens():
    """B3 parallel for Stage 3."""
    call_result = CallResult(
        text=_valid_premortem_response_for(["H1", "H2", "H3"]),
        model="claude-sonnet-4-5-20250929",
        input_tokens=800,
        output_tokens=400,
        thinking_tokens=0,
        cache_read_tokens=9999,
        cache_creation_tokens=111,
        cost_usd=0.005,
        latency_ms=180,
    )
    client = _mock_orchestrator_client_returning(call_result)

    hr = HypothesisResult(
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

    result = run_premortem(
        client,
        hypothesis_result=hr,
        ctx={"facts": []},
        model="claude-sonnet-4-5-20250929",
    )

    assert result.cache_read_tokens == 9999
    assert result.cache_creation_tokens == 111
    assert result.input_tokens == 800


# ---------------------------------------------------------------------------
# B1: Stage 7 gate end-to-end — heavy mock of _run_one_inner dependencies
# ---------------------------------------------------------------------------


def _baseline_ctx() -> Dict[str, Any]:
    """Minimal ctx satisfying Stage 0/4/1 helpers without DB access."""
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
        "documents": [],
        "memory_text": None,
        "reference_class_anchor": None,
        "asset_doc_links": [],
    }


def _stage4_anchor_stub():
    """Return (anchor, metric) stub for stage_4_anchor patch.
    has_signal is a @property on Stage4Anchor (computed from base_rate +
    similar_cases), so don't pass it as a constructor kwarg."""
    from modal_workers.shared.compute import Stage4Anchor
    from orchestrator_runtime.runtime import StageMetric
    anchor = Stage4Anchor(
        reference_class="phase3_oncology_breakthrough",
        base_rate=None,
        similar_cases=[],
    )
    return anchor, StageMetric(stage_name="stage_4_reference_class_anchor",
                               model="deterministic")


def _stage1_synth_stub():
    """Return (cited_prose, metric) for stage_1_synthesize patch."""
    from orchestrator_runtime.runtime import StageMetric
    cited_prose = "## Conclusion\n- thesis_direction: long\n- conviction_pct: 70"
    return cited_prose, StageMetric(
        stage_name="stage_1_synthesis",
        model="claude-sonnet-4-5-20250929",
        input_tokens=100, output_tokens=50, cost_usd=0.001,
    )


def _stage9_extract_stub_ok():
    """Return (parsed, metric) for stage_9_extract patch — valid payload."""
    from orchestrator_runtime.runtime import StageMetric
    parsed = {
        "thesis_direction": "long",
        "conviction_pct": 70.0,
        "evidence_quality": 0.7,
        "cited_prose_blocks": [],
        "key_facts": [],
        "uncertainties": [],
        "thesis_summary": "summary",
    }
    return parsed, StageMetric(
        stage_name="stage_9_extraction",
        model="claude-sonnet-4-5-20250929",
        input_tokens=80, output_tokens=120, cost_usd=0.002,
        status="completed",
    )


def _stage9_extract_stub_fail():
    """Return (None, metric) — what Stage 9 emits on parse failure."""
    from orchestrator_runtime.runtime import StageMetric
    return None, StageMetric(
        stage_name="stage_9_extraction",
        model="claude-sonnet-4-5-20250929",
        input_tokens=80, output_tokens=120, cost_usd=0.002,
        status="failed",
    )


def _constitutional_result_failing() -> ConstitutionalResult:
    return ConstitutionalResult(
        pass_=False,
        findings=[
            ConstitutionalFinding(
                severity="error", check="unresolved_fact_id",
                detail="F:cafebabe not in fact set",
            ),
        ],
        n_citations_checked=3, n_citations_resolved=2,
    )


def _constitutional_result_passing() -> ConstitutionalResult:
    return ConstitutionalResult(
        pass_=True, findings=[],
        n_citations_checked=3, n_citations_resolved=3,
    )


def test_stage_7_pass_proceeds_to_persist():
    """When constitutional.pass_=True, _run_one_inner reaches Stage 10."""
    from orchestrator_runtime import runtime

    sb_mock = MagicMock()
    client_mock = MagicMock()

    with patch.object(runtime, "stage_0_load", return_value=_baseline_ctx()), \
         patch.object(runtime, "stage_4_anchor", return_value=_stage4_anchor_stub()), \
         patch.object(runtime, "stage_1_synthesize", return_value=_stage1_synth_stub()), \
         patch.object(runtime, "stage_9_extract", return_value=_stage9_extract_stub_ok()), \
         patch.object(runtime, "run_hypothesis_enumeration",
                      return_value=HypothesisResult(pass_=True, hypotheses=[])), \
         patch.object(runtime, "run_constitutional_check",
                      return_value=_constitutional_result_passing()), \
         patch.object(runtime, "stage_10_persist", return_value="assessment-id-xyz") \
            as stage_10_mock:
        aid = runtime._run_one_inner(
            sb_mock, client_mock,
            asset_id="asset-uuid-1", trigger_type="manual",
            model="claude-sonnet-4-5-20250929",
            extractor_model="claude-sonnet-4-5-20250929",
            ensemble_n=1, ensemble_mode="streaming",
            run_constitutional=True,
            constitutional_skip_semantic=True,
            enable_premortem=False,
            dry_run=False,
        )

    assert aid == "assessment-id-xyz"
    assert stage_10_mock.call_count == 1


def test_stage_7_fail_raises_constitutional_failure_and_blocks_persist():
    """When constitutional.pass_=False, ConstitutionalFailure is raised
    BEFORE Stage 10. The DB row must NOT be inserted (no Stage 10 call)."""
    from orchestrator_runtime import runtime

    sb_mock = MagicMock()
    client_mock = MagicMock()

    with patch.object(runtime, "stage_0_load", return_value=_baseline_ctx()), \
         patch.object(runtime, "stage_4_anchor", return_value=_stage4_anchor_stub()), \
         patch.object(runtime, "stage_1_synthesize", return_value=_stage1_synth_stub()), \
         patch.object(runtime, "stage_9_extract", return_value=_stage9_extract_stub_ok()), \
         patch.object(runtime, "run_hypothesis_enumeration",
                      return_value=HypothesisResult(pass_=True, hypotheses=[])), \
         patch.object(runtime, "run_constitutional_check",
                      return_value=_constitutional_result_failing()), \
         patch.object(runtime, "stage_10_persist", return_value="should-never-fire") \
            as stage_10_mock:
        with pytest.raises(ConstitutionalFailure) as exc_info:
            runtime._run_one_inner(
                sb_mock, client_mock,
                asset_id="asset-uuid-1", trigger_type="manual",
                model="claude-sonnet-4-5-20250929",
                extractor_model="claude-sonnet-4-5-20250929",
                ensemble_n=1, ensemble_mode="streaming",
                run_constitutional=True,
                constitutional_skip_semantic=True,
                enable_premortem=False,
                dry_run=False,
            )
    # Stage 10 was never invoked — the gate worked
    assert stage_10_mock.call_count == 0
    # The exception carries the offending findings for downstream logging
    assert len(exc_info.value.findings) >= 1
    assert exc_info.value.findings[0].check == "unresolved_fact_id"


# ---------------------------------------------------------------------------
# B2: Streaming ensemble routes through a_client.call() (not the raw SDK
# client) so it inherits the wrapper's transient-retry/backoff + Opus
# interleaved-thinking beta header. The wrapper already prices cache tokens
# (covered by test_pricing / test_client_headers); the ensemble-level
# guarantee verified here is: it calls the wrapper, forwards temperature for
# Stage-1 diversity only, and faithfully aggregates s1+s9 tokens/cost.
# ---------------------------------------------------------------------------


def _call_result(text: str, *, input_tokens: int, output_tokens: int,
                  cache_read: int = 0, cache_create: int = 0,
                  cost_usd: float = 0.0, latency_ms: int = 0,
                  thinking_tokens: int = 0) -> CallResult:
    return CallResult(
        text=text,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        thinking_tokens=thinking_tokens,
        cache_read_tokens=cache_read,
        cache_creation_tokens=cache_create,
        cost_usd=cost_usd,
        latency_ms=latency_ms,
        model="claude-sonnet-4-5-20250929",
        raw_message=None,
    )


def _client_returning(s1: CallResult, s9: CallResult) -> MagicMock:
    a_client = MagicMock()
    a_client.call.side_effect = [s1, s9]
    return a_client


def test_streaming_ensemble_routes_through_call_wrapper():
    """B2: _run_one_streaming MUST go through a_client.call() (the retry/header
    wrapper) and NOT the raw a_client._client.messages.create — calling the raw
    client is exactly the regression that made one immediate APIError kill every
    ensemble slot in seconds with no retry."""
    from orchestrator_runtime import ensemble

    extract_valid = (
        '{"thesis_direction": "long", "conviction_pct": 70.0, '
        '"evidence_quality": 0.7, "cited_prose_blocks": [], '
        '"key_facts": [], "uncertainties": []}'
    )
    s1 = _call_result("## Conclusion\nthesis_direction: long",
                       input_tokens=1000, output_tokens=500,
                       cache_read=2000, cache_create=400, cost_usd=0.10)
    s9 = _call_result(extract_valid,
                       input_tokens=200, output_tokens=300,
                       cache_read=800, cache_create=0, cost_usd=0.02)
    a_client = _client_returning(s1, s9)

    result = ensemble._run_one_streaming(
        a_client,
        stage_1_system="STAGE_1_SYSTEM",
        stage_1_user_content="user content",
        stage_9_system="STAGE_9_SYSTEM",
        model="claude-sonnet-4-5-20250929",
        extractor_model="claude-sonnet-4-5-20250929",
        run_idx=0, temperature=0.8,
        max_tokens_synth=4096, max_tokens_extract=8192,
    )

    assert result is not None
    # Went through the wrapper, exactly twice (Stage 1 + Stage 9)...
    assert a_client.call.call_count == 2
    # ...and never touched the raw SDK client.
    a_client._client.messages.create.assert_not_called()

    s1_kwargs = a_client.call.call_args_list[0].kwargs
    s9_kwargs = a_client.call.call_args_list[1].kwargs
    # Stage 1 carries the diversity temperature; Stage 9 is deterministic.
    assert s1_kwargs.get("temperature") == 0.8
    assert "temperature" not in s9_kwargs or s9_kwargs.get("temperature") is None

    # EnsembleRun aggregates s1 + s9 cache tokens / cost (not just s1).
    assert result.cache_read_tokens == 2000 + 800
    assert result.cache_creation_tokens == 400 + 0
    assert result.cost_usd == pytest.approx(0.10 + 0.02, abs=1e-9)


def test_streaming_ensemble_sums_callresult_cost_without_loss():
    """B2 numeric: the EnsembleRun cost is exactly s1.cost_usd + s9.cost_usd —
    no double-count, no dropped stage. (Cache-token pricing itself is the
    wrapper's job, asserted in test_pricing / test_client_headers.)"""
    from orchestrator_runtime import ensemble

    extract_valid = (
        '{"thesis_direction": "long", "conviction_pct": 70.0, '
        '"evidence_quality": 0.7, "cited_prose_blocks": [], '
        '"key_facts": [], "uncertainties": []}'
    )
    s1 = _call_result("## Conclusion\nthesis_direction: long",
                       input_tokens=1000, output_tokens=500, cost_usd=0.4242)
    s9 = _call_result(extract_valid,
                       input_tokens=200, output_tokens=300, cost_usd=0.0137)
    a_client = _client_returning(s1, s9)

    run = ensemble._run_one_streaming(
        a_client,
        stage_1_system="S1", stage_1_user_content="user",
        stage_9_system="S9",
        model="claude-sonnet-4-5-20250929",
        extractor_model="claude-sonnet-4-5-20250929",
        run_idx=0, temperature=0.8,
        max_tokens_synth=4096, max_tokens_extract=8192,
    )

    assert run.cost_usd == pytest.approx(0.4242 + 0.0137, abs=1e-9)


def test_stage_9_parse_failure_raises_stage_9_parse_error():
    """When Stage 9 returns parsed=None, _run_one_inner raises
    Stage9ParseError (previously: silently returned None and
    orchestrator_app classified the run as 'completed' with assessment_id
    null)."""
    from orchestrator_runtime import runtime

    sb_mock = MagicMock()
    client_mock = MagicMock()

    with patch.object(runtime, "stage_0_load", return_value=_baseline_ctx()), \
         patch.object(runtime, "stage_4_anchor", return_value=_stage4_anchor_stub()), \
         patch.object(runtime, "stage_1_synthesize", return_value=_stage1_synth_stub()), \
         patch.object(runtime, "stage_9_extract", return_value=_stage9_extract_stub_fail()), \
         patch.object(runtime, "stage_10_persist") as stage_10_mock:
        with pytest.raises(Stage9ParseError):
            runtime._run_one_inner(
                sb_mock, client_mock,
                asset_id="asset-uuid-1", trigger_type="manual",
                model="claude-sonnet-4-5-20250929",
                extractor_model="claude-sonnet-4-5-20250929",
                ensemble_n=1, ensemble_mode="streaming",
                run_constitutional=True,
                constitutional_skip_semantic=True,
                enable_premortem=False,
                dry_run=False,
            )
    assert stage_10_mock.call_count == 0
