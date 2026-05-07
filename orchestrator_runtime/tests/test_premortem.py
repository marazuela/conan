"""Tests for orchestrator_runtime.premortem — Stage 3 verdict validator +
local-rollup discipline (model output is observed, not trusted).

Run: python -m pytest orchestrator_runtime/tests/test_premortem.py -v
"""
from __future__ import annotations

import os

os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

from orchestrator_runtime.premortem import (
    _validate_and_parse_verdicts,
)


def _fact_set(*shorts):
    return {s.lower() for s in shorts}


def _verdict(hyp_id, verdict, fms=None):
    return {
        "hypothesis_id": hyp_id,
        "verdict": verdict,
        "failure_modes": fms or [],
        "disconfirming_searches": [],
        "update_triggers": [],
    }


def _fm(desc, severity, ev_ids=None, speculative=False):
    return {
        "description": desc,
        "severity": severity,
        "evidence_fact_ids": ev_ids or [],
        "speculative": speculative,
    }


# ---------------------------------------------------------------------------
# Parse failures
# ---------------------------------------------------------------------------


def test_validator_rejects_non_dict():
    verdicts, overall, surviving, findings = _validate_and_parse_verdicts(
        None, ["H1"], _fact_set())
    assert verdicts == []
    assert overall == "all_falsified"
    assert any(f.check == "parse_failure" and f.severity == "error" for f in findings)


def test_validator_rejects_non_list_verdicts():
    parsed = {"verdicts": "not-a-list", "overall_verdict": "all_survive"}
    verdicts, overall, _, findings = _validate_and_parse_verdicts(
        parsed, ["H1"], _fact_set())
    assert verdicts == []
    assert overall == "all_falsified"


# ---------------------------------------------------------------------------
# Local-rollup overrides model claim
# ---------------------------------------------------------------------------


def test_local_rollup_overrides_model_overall_verdict():
    """Model claims all_survive but one hypothesis is actually falsified.
    Local rollup wins; model gets an info-level mismatch finding."""
    parsed = {
        "verdicts": [
            _verdict("H1", "survives"),
            _verdict("H2", "falsified",
                     [_fm("dies", "kill", ev_ids=["aabbccdd"])]),
            _verdict("H3", "survives"),
        ],
        "overall_verdict": "all_survive",
        "surviving_hypothesis_ids": ["H1", "H2", "H3"],
    }
    _, overall, surviving, findings = _validate_and_parse_verdicts(
        parsed, ["H1", "H2", "H3"], _fact_set("aabbccdd"))
    assert overall == "partial"
    assert set(surviving) == {"H1", "H3"}
    assert any(f.check == "overall_verdict_mismatch" and f.severity == "info"
               for f in findings)


def test_local_rollup_all_falsified_when_every_verdict_is_falsified():
    parsed = {
        "verdicts": [
            _verdict("H1", "falsified",
                     [_fm("d", "kill", ev_ids=["aabbccdd"])]),
            _verdict("H2", "falsified",
                     [_fm("d", "kill", ev_ids=["aabbccdd"])]),
            _verdict("H3", "falsified",
                     [_fm("d", "kill", ev_ids=["aabbccdd"])]),
        ],
        "overall_verdict": "partial",  # model lies
        "surviving_hypothesis_ids": ["H1"],
    }
    _, overall, surviving, _ = _validate_and_parse_verdicts(
        parsed, ["H1", "H2", "H3"], _fact_set("aabbccdd"))
    assert overall == "all_falsified"
    assert surviving == []


def test_surviving_ids_mismatch_emits_info_finding():
    parsed = {
        "verdicts": [
            _verdict("H1", "survives"),
            _verdict("H2", "weakened",
                     [_fm("d", "weaken", ev_ids=["aabbccdd"])]),
            _verdict("H3", "survives"),
        ],
        "overall_verdict": "partial",
        "surviving_hypothesis_ids": ["H1", "H2"],  # H2 is weakened, not survived
    }
    _, _, surviving, findings = _validate_and_parse_verdicts(
        parsed, ["H1", "H2", "H3"], _fact_set("aabbccdd"))
    assert set(surviving) == {"H1", "H3"}
    assert any(f.check == "surviving_ids_mismatch" for f in findings)


# ---------------------------------------------------------------------------
# Strict-sourcing on failure modes
# ---------------------------------------------------------------------------


def test_non_speculative_failure_mode_without_evidence_raises_error():
    parsed = {
        "verdicts": [
            _verdict("H1", "weakened",
                     [_fm("hand-wave", "weaken", ev_ids=[],
                          speculative=False)]),  # NOT speculative, NO ev_ids
            _verdict("H2", "survives"),
            _verdict("H3", "survives"),
        ],
        "overall_verdict": "partial",
        "surviving_hypothesis_ids": ["H2", "H3"],
    }
    _, _, _, findings = _validate_and_parse_verdicts(
        parsed, ["H1", "H2", "H3"], _fact_set())
    miss = [f for f in findings if f.check == "missing_citation_non_speculative"]
    assert len(miss) == 1
    assert miss[0].severity == "error"


def test_speculative_failure_mode_without_evidence_is_allowed():
    parsed = {
        "verdicts": [
            _verdict("H1", "survives",
                     [_fm("could happen", "tail", ev_ids=[],
                          speculative=True)]),  # speculative=true, no ev_ids OK
            _verdict("H2", "survives"),
            _verdict("H3", "survives"),
        ],
        "overall_verdict": "all_survive",
        "surviving_hypothesis_ids": ["H1", "H2", "H3"],
    }
    _, _, _, findings = _validate_and_parse_verdicts(
        parsed, ["H1", "H2", "H3"], _fact_set())
    assert not any(f.check == "missing_citation_non_speculative" for f in findings)


def test_unresolved_evidence_fact_id_warns():
    parsed = {
        "verdicts": [
            _verdict("H1", "weakened",
                     [_fm("d", "weaken", ev_ids=["ZZZZZZZZ"])]),
            _verdict("H2", "survives"),
            _verdict("H3", "survives"),
        ],
        "overall_verdict": "partial",
        "surviving_hypothesis_ids": ["H2", "H3"],
    }
    _, _, _, findings = _validate_and_parse_verdicts(
        parsed, ["H1", "H2", "H3"], _fact_set("aabbccdd"))
    assert any(f.check == "unresolved_evidence_fact_id" for f in findings)


# ---------------------------------------------------------------------------
# Missing verdict / unknown hypothesis_id
# ---------------------------------------------------------------------------


def test_missing_verdict_for_known_hypothesis_raises_error():
    parsed = {
        "verdicts": [
            _verdict("H1", "survives"),
            # H2 missing
            _verdict("H3", "survives"),
        ],
        "overall_verdict": "all_survive",
        "surviving_hypothesis_ids": ["H1", "H3"],
    }
    _, _, _, findings = _validate_and_parse_verdicts(
        parsed, ["H1", "H2", "H3"], _fact_set())
    miss = [f for f in findings if f.check == "missing_verdict"]
    assert len(miss) == 1
    assert miss[0].severity == "error"
    assert "H2" in miss[0].detail


def test_unknown_hypothesis_id_skipped_with_warning():
    parsed = {
        "verdicts": [
            _verdict("H1", "survives"),
            _verdict("H2", "survives"),
            _verdict("H3", "survives"),
            _verdict("H99", "survives"),  # unknown
        ],
        "overall_verdict": "all_survive",
        "surviving_hypothesis_ids": ["H1", "H2", "H3"],
    }
    verdicts, _, _, findings = _validate_and_parse_verdicts(
        parsed, ["H1", "H2", "H3"], _fact_set())
    assert len(verdicts) == 3
    assert any(f.check == "unknown_hypothesis_id" for f in findings)


def test_invalid_verdict_value_defaults_to_weakened():
    parsed = {
        "verdicts": [
            _verdict("H1", "totally-invalid"),
            _verdict("H2", "survives"),
            _verdict("H3", "survives"),
        ],
        "overall_verdict": "partial",
        "surviving_hypothesis_ids": ["H2", "H3"],
    }
    verdicts, _, _, findings = _validate_and_parse_verdicts(
        parsed, ["H1", "H2", "H3"], _fact_set())
    h1 = next(v for v in verdicts if v.hypothesis_id == "H1")
    assert h1.verdict == "weakened"
    assert any(f.check == "invalid_verdict" for f in findings)
