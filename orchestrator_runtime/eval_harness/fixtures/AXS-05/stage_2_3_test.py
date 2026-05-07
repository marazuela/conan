"""Unit tests for Stage 2 (hypothesis enumeration) + Stage 3 (pre-mortem).

Run from repo root:
  python3 -m pytest orchestrator_runtime/eval_harness/fixtures/AXS-05/stage_2_3_test.py -v

Tests are pure-function (no API calls). They exercise:
  - hypothesis._validate_and_parse_hypotheses (Stage 2 JSON validator)
  - premortem._validate_and_parse_verdicts (Stage 3 JSON validator)
  - constitutional.check_hypothesis_premortem_citations (Stage 7 extension)
  - The Stage 9 post-hoc cap (ALL_FALSIFIED_CONVICTION_CEILING)

The expected JSON fixtures (stage_2_hypothesis_expected.json,
stage_3_premortem_expected.json) live alongside this file and are loaded as
reference shapes for validators to chew on.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator_runtime.constitutional import (
    check_hypothesis_premortem_citations,
)
from orchestrator_runtime.hypothesis import (
    Hypothesis,
    HypothesisResult,
    REQUIRED_LABELS,
    _validate_and_parse_hypotheses,
)
from orchestrator_runtime.premortem import (
    FailureMode,
    HypothesisVerdict,
    PreMortemResult,
    _validate_and_parse_verdicts,
)
from orchestrator_runtime.runtime import ALL_FALSIFIED_CONVICTION_CEILING


FIXTURE_DIR = Path(__file__).parent
HYP_EXPECTED = json.loads((FIXTURE_DIR / "stage_2_hypothesis_expected.json").read_text())
PRE_EXPECTED = json.loads((FIXTURE_DIR / "stage_3_premortem_expected.json").read_text())


# All fact short-ids used in the expected fixtures. The validator checks that
# cited fact_ids resolve against this set. Hex shorts mirror real fact_id UUIDs
# so they round-trip through Stage 7's CITE_FACT_RE.
FIXTURE_FACT_SHORTS = {
    "a0c1d4f1", "b2e3f456", "c4d5e678", "d6e7f890", "e8f9a012",
    "f0a1b234", "1a2b3c45", "2b3c4d56",
    "3c4d5e67", "4d5e6f78", "5e6f7a89",
}


# ===========================================================================
# Stage 2 validator
# ===========================================================================

def test_stage2_expected_fixture_passes_validator():
    """The hand-curated expected Stage 2 output must validate cleanly:
    3 hypotheses, all required labels present, every fact_id resolves."""
    parsed = {"hypotheses": HYP_EXPECTED["hypotheses"]}
    hypotheses, findings = _validate_and_parse_hypotheses(
        parsed, FIXTURE_FACT_SHORTS,
    )
    assert len(hypotheses) >= 3, f"expected 3+ hypotheses, got {len(hypotheses)}"
    labels = {h.label for h in hypotheses}
    assert REQUIRED_LABELS.issubset(labels), \
        f"required labels missing from {labels}"
    # No error-severity findings on the curated fixture
    errors = [f for f in findings if f.severity == "error"]
    assert errors == [], f"unexpected errors: {[f.detail for f in errors]}"
    # Every hypothesis has 2+ kill_conditions per strict-sourcing
    for h in hypotheses:
        assert len(h.kill_conditions) >= 2, \
            f"{h.hypothesis_id} has <2 kill_conditions: {h.kill_conditions}"


def test_stage2_too_few_hypotheses_raises_error():
    parsed = {"hypotheses": HYP_EXPECTED["hypotheses"][:2]}
    _, findings = _validate_and_parse_hypotheses(parsed, FIXTURE_FACT_SHORTS)
    error_checks = {f.check for f in findings if f.severity == "error"}
    assert "too_few_hypotheses" in error_checks
    assert "missing_required_label" in error_checks


def test_stage2_missing_kill_conditions_raises_error():
    bad = json.loads(json.dumps(HYP_EXPECTED["hypotheses"]))  # deep copy
    bad[0]["kill_conditions"] = []   # zero kill conditions
    parsed = {"hypotheses": bad}
    _, findings = _validate_and_parse_hypotheses(parsed, FIXTURE_FACT_SHORTS)
    assert any(
        f.severity == "error" and f.check == "missing_kill_conditions"
        for f in findings
    )


def test_stage2_unresolved_fact_id_raises_warning():
    bad = json.loads(json.dumps(HYP_EXPECTED["hypotheses"]))
    bad[0]["supporting_fact_ids"] = ["nonexist"]
    parsed = {"hypotheses": bad}
    _, findings = _validate_and_parse_hypotheses(parsed, FIXTURE_FACT_SHORTS)
    assert any(
        f.check == "unresolved_supporting_fact_id" and f.affected_id == "nonexist"
        for f in findings
    )


def test_stage2_parse_failure_on_non_object():
    _, findings = _validate_and_parse_hypotheses(None, FIXTURE_FACT_SHORTS)
    assert any(
        f.severity == "error" and f.check == "parse_failure" for f in findings
    )


# ===========================================================================
# Stage 3 validator
# ===========================================================================

def test_stage3_expected_fixture_validator():
    """The hand-curated expected Stage 3 output must validate as 'partial'
    overall verdict (mix of survives + weakened) with H1 as the surviving id."""
    hypothesis_ids = [h["hypothesis_id"] for h in HYP_EXPECTED["hypotheses"]]
    verdicts, overall, surviving, findings = _validate_and_parse_verdicts(
        PRE_EXPECTED, hypothesis_ids, FIXTURE_FACT_SHORTS,
    )
    errors = [f for f in findings if f.severity == "error"]
    assert errors == [], f"unexpected errors: {[f.detail for f in errors]}"
    assert overall == "partial"
    assert surviving == ["H1"]
    assert {v.hypothesis_id for v in verdicts} == {"H1", "H2", "H3"}


def test_stage3_all_falsified_overrides_model_overall():
    hypothesis_ids = ["H1", "H2", "H3"]
    parsed = {
        "verdicts": [
            {"hypothesis_id": "H1", "verdict": "falsified",
             "failure_modes": [{"description": "x", "severity": "kill",
                                "evidence_fact_ids": ["axsmpv01"],
                                "speculative": False}]},
            {"hypothesis_id": "H2", "verdict": "falsified",
             "failure_modes": [{"description": "y", "severity": "kill",
                                "evidence_fact_ids": ["axsmpv02"],
                                "speculative": False}]},
            {"hypothesis_id": "H3", "verdict": "falsified",
             "failure_modes": [{"description": "z", "severity": "kill",
                                "evidence_fact_ids": ["btdaxs05"],
                                "speculative": False}]},
        ],
        # Even if the model insists otherwise, local rollup wins
        "overall_verdict": "all_survive",
        "surviving_hypothesis_ids": ["H1", "H2", "H3"],
    }
    _, overall, surviving, findings = _validate_and_parse_verdicts(
        parsed, hypothesis_ids, FIXTURE_FACT_SHORTS,
    )
    assert overall == "all_falsified"
    assert surviving == []
    # An info-severity mismatch is logged when the model disagrees
    assert any(f.check == "overall_verdict_mismatch" for f in findings)


def test_stage3_non_speculative_failure_without_citation_raises_error():
    hypothesis_ids = ["H1"]
    parsed = {
        "verdicts": [{
            "hypothesis_id": "H1",
            "verdict": "weakened",
            "failure_modes": [{
                "description": "non-speculative claim with no fact citation",
                "severity": "weaken",
                "evidence_fact_ids": [],
                "speculative": False,
            }],
        }],
        "overall_verdict": "partial",
        "surviving_hypothesis_ids": [],
    }
    _, _, _, findings = _validate_and_parse_verdicts(
        parsed, hypothesis_ids, FIXTURE_FACT_SHORTS,
    )
    assert any(
        f.severity == "error" and f.check == "missing_citation_non_speculative"
        for f in findings
    )


def test_stage3_speculative_failure_without_citation_is_allowed():
    hypothesis_ids = ["H1"]
    parsed = {
        "verdicts": [{
            "hypothesis_id": "H1",
            "verdict": "survives",
            "failure_modes": [{
                "description": "speculative tail risk; no observed evidence yet",
                "severity": "tail",
                "evidence_fact_ids": [],
                "speculative": True,
            }],
        }],
        "overall_verdict": "all_survive",
        "surviving_hypothesis_ids": ["H1"],
    }
    _, overall, surviving, findings = _validate_and_parse_verdicts(
        parsed, hypothesis_ids, FIXTURE_FACT_SHORTS,
    )
    assert overall == "all_survive"
    assert surviving == ["H1"]
    errors = [f for f in findings if f.severity == "error"]
    assert errors == []


# ===========================================================================
# Stage 7 extension — constitutional check on Stage 2/3 outputs
# ===========================================================================

# Real fact_id UUIDs are hex; the existing constitutional CITE_FACT_RE matches
# /\[F:([0-9a-f]{6,12})\]/ only. Constitutional-test fixtures therefore use
# hex-only 8-char shorts (the AXS-05 expected fixture above uses readable
# pseudo-shorts for human review; those go through the validator path which
# is regex-free).
HEX_SHORT_GOOD = "abcdef01"
HEX_SHORT_OTHER = "deadbeef"


def _full_uuid(short: str) -> str:
    """Pad an 8-char short to a faux UUID. The constitutional check only
    compares the first 8 chars, so the rest is filler."""
    return f"{short}-1111-1111-1111-111111111111"


def test_constitutional_walks_hypothesis_mechanism_citations():
    fact_ids = [_full_uuid(HEX_SHORT_GOOD)]
    document_ids = []

    good_hyp = HypothesisResult(
        pass_=True,
        hypotheses=[Hypothesis(
            hypothesis_id="H1",
            label="bull",
            claim="...",
            mechanism=f"Phase 3 positive [F:{HEX_SHORT_GOOD}].",
            direction="bullish",
            supporting_fact_ids=[HEX_SHORT_GOOD],
            contradicting_fact_ids=[],
            kill_conditions=["a", "b"],
            prior_estimate_pct=60,
        )],
    )
    findings, n_total, n_resolved = check_hypothesis_premortem_citations(
        hypothesis_result=good_hyp,
        premortem_result=None,
        fact_ids=fact_ids,
        document_ids=document_ids,
    )
    assert findings == []
    assert n_total == 2  # one inline cite + one supporting_fact_id
    assert n_resolved == 2

    # Now break it — cite an unresolved short
    bad_hyp = HypothesisResult(
        pass_=True,
        hypotheses=[Hypothesis(
            hypothesis_id="H1",
            label="bull",
            claim="...",
            mechanism=f"Phase 3 positive [F:{HEX_SHORT_OTHER}].",
            direction="bullish",
            supporting_fact_ids=[HEX_SHORT_GOOD],
            contradicting_fact_ids=[],
            kill_conditions=["a", "b"],
            prior_estimate_pct=60,
        )],
    )
    findings, _, _ = check_hypothesis_premortem_citations(
        hypothesis_result=bad_hyp,
        premortem_result=None,
        fact_ids=fact_ids,
        document_ids=document_ids,
    )
    error_checks = {f.check for f in findings if f.severity == "error"}
    assert "hypothesis_unresolved_fact_id" in error_checks


def test_constitutional_walks_premortem_failure_modes():
    fact_ids = [_full_uuid(HEX_SHORT_GOOD)]
    document_ids = []

    pre = PreMortemResult(
        pass_=True,
        overall_verdict="partial",
        surviving_hypothesis_ids=["H1"],
        verdicts=[HypothesisVerdict(
            hypothesis_id="H1",
            verdict="weakened",
            failure_modes=[
                FailureMode(
                    description="cited",
                    severity="weaken",
                    evidence_fact_ids=[HEX_SHORT_GOOD],
                    speculative=False,
                ),
                FailureMode(
                    description="non-speculative-no-citation",
                    severity="weaken",
                    evidence_fact_ids=[],
                    speculative=False,
                ),
            ],
        )],
    )
    findings, _, _ = check_hypothesis_premortem_citations(
        hypothesis_result=None,
        premortem_result=pre,
        fact_ids=fact_ids,
        document_ids=document_ids,
    )
    error_checks = {f.check for f in findings if f.severity == "error"}
    assert "premortem_missing_citation_non_speculative" in error_checks


# ===========================================================================
# Stage 9 cap on all_falsified
# ===========================================================================

def test_stage_9_cap_constant_is_30():
    assert ALL_FALSIFIED_CONVICTION_CEILING == 30.0


def test_stage_9_cap_logic_lowers_high_conviction():
    # Simulate the cap path used inline in run_one()
    parsed = {"conviction_pct": 78.5}
    overall = "all_falsified"
    if overall == "all_falsified":
        raw = float(parsed.get("conviction_pct") or 0.0)
        parsed["conviction_pct"] = min(raw, ALL_FALSIFIED_CONVICTION_CEILING)
    assert parsed["conviction_pct"] == 30.0


def test_stage_9_cap_no_op_when_already_below():
    parsed = {"conviction_pct": 18.0}
    overall = "all_falsified"
    if overall == "all_falsified":
        raw = float(parsed.get("conviction_pct") or 0.0)
        parsed["conviction_pct"] = min(raw, ALL_FALSIFIED_CONVICTION_CEILING)
    assert parsed["conviction_pct"] == 18.0


def test_stage_9_cap_skipped_when_partial():
    parsed = {"conviction_pct": 78.5}
    overall = "partial"
    if overall == "all_falsified":
        raw = float(parsed.get("conviction_pct") or 0.0)
        parsed["conviction_pct"] = min(raw, ALL_FALSIFIED_CONVICTION_CEILING)
    assert parsed["conviction_pct"] == 78.5
