"""Tests for orchestrator_runtime.runtime — Stage 2/3 integration with the
constitutional check (D-117 gate) + cached system prefix invariants (D-119).

Run: python -m pytest orchestrator_runtime/tests/test_runtime_stage_2_3.py -v
"""
from __future__ import annotations

import os

os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import pytest

from orchestrator_runtime.runtime import (
    build_shared_system_prefix,
    build_system_blocks,
    _build_stage_1_user_content,
)
from orchestrator_runtime.hypothesis import (
    Hypothesis,
    HypothesisFinding,
    HypothesisResult,
    STAGE_2_SYSTEM,
    _build_stage_2_user_content,
)
from orchestrator_runtime.premortem import (
    HypothesisVerdict,
    PreMortemFinding,
    PreMortemResult,
    STAGE_3_SYSTEM,
    _build_stage_3_user_content,
)
from orchestrator_runtime.constitutional import (
    SEMANTIC_SYSTEM_PROMPT,
    check_hypothesis_premortem_citations,
    run_constitutional_check,
)


# ---------------------------------------------------------------------------
# D-119: shared system prefix cache invariants
# ---------------------------------------------------------------------------


def _make_ctx():
    return {
        "asset": {
            "id": "asset-uuid",
            "ticker": "VRDN",
            "drug_name": "Veligrotug",
            "generic_name": "veligrotug",
            "sponsor_name": "Viridian Therapeutics",
            "indication": "Thyroid eye disease",
            "indication_normalized": "tepezza_competitor",
            "reference_class_signature": "phase3_oncology_breakthrough",
            "application_number": "BLA-12345",
            "program_status": "submitted",
        },
        "facts": [
            {"id": "aa11bb22cc33", "document_id": "dd44ee55ff66",
             "fact_type": "trial_result", "fact_text": "Primary endpoint met",
             "evidence_quote": "p<0.001 in pivotal trial",
             "confidence": 0.95},
        ],
        "documents": [],
        "memory_text": None,
        "reference_class_anchor": None,
    }


def test_shared_prefix_is_byte_identical_across_stages():
    """Cache hit requires byte-equal first system block."""
    ctx = _make_ctx()
    prefix = build_shared_system_prefix(ctx)
    s1 = build_system_blocks(prefix, "STAGE_1_RULES")
    s2 = build_system_blocks(prefix, STAGE_2_SYSTEM)
    s3 = build_system_blocks(prefix, STAGE_3_SYSTEM)
    s7 = build_system_blocks(prefix, SEMANTIC_SYSTEM_PROMPT)
    assert s1[0]["text"] == s2[0]["text"] == s3[0]["text"] == s7[0]["text"]
    assert s1[0]["cache_control"] == {"type": "ephemeral"}


def test_shared_prefix_contains_facts_anchor_asset():
    """The cached prefix must contain everything callers expect to find
    'somewhere' so they can drop it from user content."""
    ctx = _make_ctx()
    prefix = build_shared_system_prefix(ctx)
    assert "VRDN" in prefix
    assert "aa11bb22" in prefix
    assert "phase3_oncology_breakthrough" in prefix


def test_stage_1_user_content_omits_facts():
    """D-119: facts moved to system prefix; Stage 1 user must not
    duplicate them (would inflate input tokens + break cache hit)."""
    ctx = _make_ctx()
    user = _build_stage_1_user_content(ctx)
    assert "aa11bb22" not in user
    assert "VRDN" not in user  # asset preamble also moved


def test_stage_2_user_content_omits_facts():
    user = _build_stage_2_user_content(
        cited_prose="P", parsed_json={"thesis_direction": "long"})
    assert "Tracked asset" not in user
    assert "aa11bb22" not in user
    assert "P" in user  # cited prose still present


def test_stage_3_user_content_omits_facts():
    hr = HypothesisResult(
        pass_=True,
        hypotheses=[
            Hypothesis("H1", "bull", "c", "m", "bullish",
                       kill_conditions=["k1", "k2"])
        ],
    )
    user = _build_stage_3_user_content(hypothesis_result=hr)
    assert "Tracked asset" not in user
    assert "aa11bb22" not in user
    assert "H1" in user


# ---------------------------------------------------------------------------
# D-117: Stage 2/3 structural errors gate the assessment
# ---------------------------------------------------------------------------


class _StubClient:
    """Avoid making real API calls in unit tests; semantic check is skipped."""
    pass


def _hypothesis_result_with_error_finding() -> HypothesisResult:
    return HypothesisResult(
        pass_=False,
        hypotheses=[
            Hypothesis("H1", "bull", "c", "m", "bullish",
                       kill_conditions=["k1", "k2"]),
            Hypothesis("H2", "base", "c", "m", "event_specific",
                       kill_conditions=["k1", "k2"]),
            Hypothesis("H3", "bear", "c", "m", "bearish",
                       kill_conditions=["k1", "k2"]),
        ],
        findings=[
            HypothesisFinding(severity="error", check="missing_required_label",
                              detail="bull missing"),
        ],
    )


def _premortem_result_with_error_finding() -> PreMortemResult:
    return PreMortemResult(
        pass_=False,
        overall_verdict="partial",
        surviving_hypothesis_ids=["H1"],
        verdicts=[HypothesisVerdict("H1", "survives")],
        findings=[
            PreMortemFinding(severity="error", check="missing_verdict",
                             detail="H2 missing"),
        ],
    )


def test_d115_stage_2_error_propagates_to_constitutional_pass_false():
    """Stage 2 emitted a structural error finding. Even if all citations
    resolve, constitutional.pass_ must be False — D-117 gate."""
    result = run_constitutional_check(
        _StubClient(),  # type: ignore[arg-type]
        cited_prose="No citations in here.",
        facts=[{"id": "aabbccddeeff"}],
        document_ids=["112233445566"],
        thesis_direction="long",
        conviction_pct=50.0,
        model="claude-sonnet-4-5",
        skip_semantic=True,
        hypothesis_result=_hypothesis_result_with_error_finding(),
        premortem_result=None,
    )
    assert result.pass_ is False
    promoted = [f for f in result.findings
                if f.check.startswith("stage_2_")]
    assert any(f.check == "stage_2_missing_required_label" for f in promoted)


def test_d115_stage_3_error_propagates_to_constitutional_pass_false():
    result = run_constitutional_check(
        _StubClient(),  # type: ignore[arg-type]
        cited_prose="No citations in here.",
        facts=[{"id": "aabbccddeeff"}],
        document_ids=["112233445566"],
        thesis_direction="long",
        conviction_pct=50.0,
        model="claude-sonnet-4-5",
        skip_semantic=True,
        hypothesis_result=None,
        premortem_result=_premortem_result_with_error_finding(),
    )
    assert result.pass_ is False
    promoted = [f for f in result.findings
                if f.check.startswith("stage_3_")]
    assert any(f.check == "stage_3_missing_verdict" for f in promoted)


def test_d115_stage_2_warning_does_not_fail_gate():
    """Warnings/info from Stage 2 must NOT flip pass_ — only severity=error."""
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
        findings=[
            HypothesisFinding(severity="warning", check="missing_direction_for_base",
                              detail="defaulted"),
        ],
    )
    result = run_constitutional_check(
        _StubClient(),  # type: ignore[arg-type]
        cited_prose="No citations.",
        facts=[{"id": "aabbccddeeff"}],
        document_ids=["112233445566"],
        thesis_direction="long",
        conviction_pct=50.0,
        model="claude-sonnet-4-5",
        skip_semantic=True,
        hypothesis_result=hr,
        premortem_result=None,
    )
    assert result.pass_ is True


def test_constitutional_walks_hypothesis_mechanism_citations():
    """Existing behavior: hypothesis mechanism [F:short] must resolve.
    The CITE_FACT_RE only matches [0-9a-f]{6,12} (hex), so the bogus short
    must also be hex-shaped to even be picked up by the walker."""
    hr = HypothesisResult(
        pass_=True,
        hypotheses=[
            Hypothesis("H1", "bull", "c",
                       mechanism="Trial succeeded [F:aabbccdd] but [F:99887766] is unknown.",
                       direction="bullish",
                       kill_conditions=["k1", "k2"]),
        ],
    )
    findings, n_total, n_resolved = check_hypothesis_premortem_citations(
        hypothesis_result=hr, premortem_result=None,
        fact_ids=["aabbccdd1111"], document_ids=[])
    assert n_total >= 2  # both cites picked up
    assert n_resolved == 1  # only aabbccdd resolves
    assert any(f.check == "hypothesis_unresolved_fact_id" for f in findings)
