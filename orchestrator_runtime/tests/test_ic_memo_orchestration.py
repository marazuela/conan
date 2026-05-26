"""Tests for orchestrator_runtime.ic_memo_runner — Stage-11 orchestration
that wraps the synthesis-only ICMemoRunner with DB-side context loading
and sub_agent_calls persistence.

Run: python3 -m pytest orchestrator_runtime/tests/test_ic_memo_orchestration.py -v
"""
from __future__ import annotations

import os
from typing import Any, Dict, List

import pytest

os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

from modal_workers.shared.supabase_client import SupabaseClient
from modal_workers.sub_agents.runtime import (
    SubAgentResult,
    SubAgentSchemaError,
)
from orchestrator_runtime import ic_memo_runner


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeRunner:
    """Stand-in for ICMemoRunner that records its inputs and returns a
    canned SubAgentResult without calling the model."""

    def __init__(self, *, output: Dict[str, Any], cost_usd: float = 0.05,
                 tokens_input: int = 1500, tokens_output: int = 600,
                 latency_ms: int = 4200):
        self.output = output
        self.cost_usd = cost_usd
        self.tokens_input = tokens_input
        self.tokens_output = tokens_output
        self.latency_ms = latency_ms
        self.calls: List[Dict[str, Any]] = []

    def run(self, *, question: str, asset_context: Dict[str, Any],
            budget_token_cap=None) -> SubAgentResult:
        self.calls.append({
            "question": question,
            "asset_context": asset_context,
        })
        return SubAgentResult(
            role="ic_memo",
            schema_pass=True,
            schema_retries=0,
            output=self.output,
            tokens_input=self.tokens_input,
            tokens_output=self.tokens_output,
            cost_usd=self.cost_usd,
            latency_ms=self.latency_ms,
        )


def _make_assessment_row(**overrides):
    base = {
        "id": "assess-1",
        "asset_id": "asset-1",
        "thesis_direction": "long",
        "conviction_pct": 72.0,
        "thesis_summary": "PDUFA approval likely on safety + efficacy.",
        "reasoning_trace": "Phase-3 GEMINI hit primary endpoint.",
        "reference_class": "psych_NDA",
        "reference_class_base_rate": 0.62,
        "similar_resolved_case_ids": ["s-1", "s-2"],
    }
    base.update(overrides)
    return base


def _make_asset_row(**overrides):
    base = {
        "id": "asset-1",
        "ticker": "AXSM",
        "drug_name": "AXS-05",
        "indication": "MDD",
        "indication_normalized": "mdd",
        "application_number": "NDA-215462",
    }
    base.update(overrides)
    return base


def _make_specialist(role: str, summary: str = "ok") -> Dict[str, Any]:
    return {
        "role": role,
        "output": {"summary": summary, "evidence": []},
        "schema_pass": True,
        "created_at": "2026-05-08T10:00:00Z",
    }


def _stub_client(rest_handler):
    """Wire fake_rest as SupabaseClient._rest via monkeypatching."""
    sb = SupabaseClient.__new__(SupabaseClient)
    sb.url = "https://fake"
    sb.service_key = "fake"
    return sb


# ---------------------------------------------------------------------------
# load_ic_memo_context
# ---------------------------------------------------------------------------

def test_load_context_returns_full_shape(monkeypatch):
    captured: List[Dict[str, Any]] = []

    def fake_rest(self, method, path, *, params=None, json_body=None, prefer=None):
        captured.append({"method": method, "path": path, "params": params})
        if method == "GET" and path == "convergence_assessments":
            return [_make_assessment_row()]
        if method == "GET" and path == "fda_assets":
            return [_make_asset_row()]
        if method == "GET" and path == "sub_agent_calls":
            return [
                _make_specialist("literature", "primary endpoint hit"),
                _make_specialist("competitive", "no near competition"),
                _make_specialist("regulatory_history", "clean AdComm history"),
                _make_specialist("options_microstructure", "low IV"),
            ]
        return []

    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    sb = _stub_client(fake_rest)

    ctx = ic_memo_runner.load_ic_memo_context(sb, "assess-1")

    assert ctx["assessment_id"] == "assess-1"
    assert ctx["asset_id"] == "asset-1"
    assert ctx["asset"]["ticker"] == "AXSM"
    assert ctx["asset"]["drug_name"] == "AXS-05"
    assert set(ctx["specialists"].keys()) == {
        "literature", "competitive", "regulatory_history",
        "options_microstructure",
    }
    assert ctx["thesis"]["direction"] == "long"
    assert ctx["thesis"]["conviction_pct"] == 72.0
    assert "GEMINI" in ctx["thesis"]["text"] or "PDUFA" in ctx["thesis"]["text"]
    assert ctx["reference_class_anchor"]["reference_class"] == "psych_NDA"
    assert ctx["reference_class_anchor"]["base_rate_pct"] == pytest.approx(62.0)

    # Specialist query filtered to schema-passing rows of the four roles
    spec_call = next(c for c in captured
                     if c["method"] == "GET" and c["path"] == "sub_agent_calls")
    assert spec_call["params"]["schema_pass"] == "is.true"
    assert "literature" in spec_call["params"]["role"]
    assert "competitive" in spec_call["params"]["role"]
    assert "ic_memo" not in spec_call["params"]["role"]


def test_load_context_dedupes_by_role_keeping_newest(monkeypatch):
    """Two literature rows (re-run of one specialist) → newest kept."""
    def fake_rest(self, method, path, *, params=None, json_body=None, prefer=None):
        if method == "GET" and path == "convergence_assessments":
            return [_make_assessment_row()]
        if method == "GET" and path == "fda_assets":
            return [_make_asset_row()]
        if method == "GET" and path == "sub_agent_calls":
            # NOTE: returned in created_at desc order per the SELECT's order=
            return [
                {"role": "literature",
                 "output": {"summary": "NEW evidence + revised reading"},
                 "schema_pass": True,
                 "created_at": "2026-05-08T12:00:00Z"},
                {"role": "literature",
                 "output": {"summary": "OLD initial reading"},
                 "schema_pass": True,
                 "created_at": "2026-05-08T08:00:00Z"},
            ]
        return []

    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    sb = _stub_client(fake_rest)

    ctx = ic_memo_runner.load_ic_memo_context(sb, "assess-1")
    assert ctx["specialists"]["literature"]["summary"].startswith("NEW")


def test_load_context_skips_empty_specialist_outputs(monkeypatch):
    """A schema-pass row with empty output dict is dropped from the
    specialists map (don't seed the LLM with empties)."""
    def fake_rest(self, method, path, *, params=None, json_body=None, prefer=None):
        if method == "GET" and path == "convergence_assessments":
            return [_make_assessment_row()]
        if method == "GET" and path == "fda_assets":
            return [_make_asset_row()]
        if method == "GET" and path == "sub_agent_calls":
            return [
                {"role": "literature", "output": {},
                 "schema_pass": True, "created_at": "2026-05-08T10:00:00Z"},
                _make_specialist("competitive"),
            ]
        return []

    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    sb = _stub_client(fake_rest)
    ctx = ic_memo_runner.load_ic_memo_context(sb, "assess-1")
    assert "literature" not in ctx["specialists"]
    assert "competitive" in ctx["specialists"]


def test_load_context_raises_on_missing_assessment(monkeypatch):
    def fake_rest(self, method, path, *, params=None, json_body=None, prefer=None):
        return []

    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    sb = _stub_client(fake_rest)

    with pytest.raises(ic_memo_runner.ICMemoOrchestrationError, match="not found"):
        ic_memo_runner.load_ic_memo_context(sb, "nope")


def test_load_context_raises_when_no_specialists_present(monkeypatch):
    """Refusing to synthesize an IC memo with zero specialist inputs is
    safer than silently emitting a vague memo."""
    def fake_rest(self, method, path, *, params=None, json_body=None, prefer=None):
        if method == "GET" and path == "convergence_assessments":
            return [_make_assessment_row()]
        if method == "GET" and path == "fda_assets":
            return [_make_asset_row()]
        if method == "GET" and path == "sub_agent_calls":
            return []
        return []

    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    sb = _stub_client(fake_rest)

    with pytest.raises(ic_memo_runner.ICMemoOrchestrationError,
                       match="no schema-passing specialist"):
        ic_memo_runner.load_ic_memo_context(sb, "assess-1")


def test_load_context_handles_missing_anchor(monkeypatch):
    """Anchor block is optional — when reference_class is null AND
    base_rate is null, the returned context omits the anchor (None)."""
    def fake_rest(self, method, path, *, params=None, json_body=None, prefer=None):
        if method == "GET" and path == "convergence_assessments":
            return [_make_assessment_row(
                reference_class=None,
                reference_class_base_rate=None,
                similar_resolved_case_ids=None,
            )]
        if method == "GET" and path == "fda_assets":
            return [_make_asset_row()]
        if method == "GET" and path == "sub_agent_calls":
            return [_make_specialist("literature")]
        return []

    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    sb = _stub_client(fake_rest)
    ctx = ic_memo_runner.load_ic_memo_context(sb, "assess-1")
    assert ctx["reference_class_anchor"] is None


# ---------------------------------------------------------------------------
# persist_ic_memo_result
# ---------------------------------------------------------------------------

def test_persist_inserts_event_scoped_ic_memo_review(monkeypatch):
    captured: List[Dict[str, Any]] = []

    def fake_rest(self, method, path, *, params=None, json_body=None, prefer=None):
        captured.append({"method": method, "path": path,
                         "json_body": json_body, "prefer": prefer})
        if method == "GET" and path == "convergence_assessments":
            return [{"asset_id": "asset-1"}]
        if method == "GET" and path == "fda_regulatory_events":
            return [{"id": "event-1"}]
        if method == "POST" and path == "fda_agent_reviews":
            return [{"id": "review-new-1"}]
        return []

    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    sb = _stub_client(fake_rest)

    result = SubAgentResult(
        role="ic_memo", schema_pass=True, schema_retries=0,
        output={"thesis": {"direction": "long"}},
        tokens_input=1200, tokens_output=500,
        cost_usd=0.04, latency_ms=3800,
    )

    new_id = ic_memo_runner.persist_ic_memo_result(
        sb, "assess-1", "Synthesize the case.", result,
    )
    assert new_id == "review-new-1"

    post = next(c for c in captured if c["method"] == "POST")
    body = post["json_body"]
    assert post["path"] == "fda_agent_reviews"
    assert body["event_id"] == "event-1"
    assert body["agent_kind"] == "ic_memo"
    assert body["status"] == "completed"
    assert body["snapshot_hash"] == "assessment:assess-1"
    assert body["structured_output"]["thesis"] == {"direction": "long"}
    assert body["structured_output"]["_orchestrator_meta"]["assessment_id"] == "assess-1"
    assert body["structured_output"]["_orchestrator_meta"]["tokens"] == 1700
    assert post["prefer"] == "return=representation"


def test_persist_raises_when_insert_returns_no_row(monkeypatch):
    def fake_rest(self, method, path, *, params=None, json_body=None, prefer=None):
        if method == "GET" and path == "convergence_assessments":
            return [{"asset_id": "asset-1"}]
        if method == "GET" and path == "fda_regulatory_events":
            return [{"id": "event-1"}]
        return []  # POST returns no rows

    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    sb = _stub_client(fake_rest)
    result = SubAgentResult(
        role="ic_memo", schema_pass=True, schema_retries=0, output={},
    )
    with pytest.raises(ic_memo_runner.ICMemoOrchestrationError,
                       match="returned no row"):
        ic_memo_runner.persist_ic_memo_result(sb, "a", "q", result)


def test_persist_skips_when_assessment_has_no_pending_event(monkeypatch):
    def fake_rest(self, method, path, *, params=None, json_body=None, prefer=None):
        if method == "GET" and path == "convergence_assessments":
            return [{"asset_id": "asset-1"}]
        if method == "GET" and path == "fda_regulatory_events":
            return []
        raise AssertionError(f"unexpected call {method} {path}")

    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    sb = _stub_client(fake_rest)
    result = SubAgentResult(
        role="ic_memo", schema_pass=True, schema_retries=0, output={},
    )
    assert ic_memo_runner.persist_ic_memo_result(sb, "a", "q", result) is None


# ---------------------------------------------------------------------------
# run_ic_memo (end-to-end)
# ---------------------------------------------------------------------------

def test_run_ic_memo_end_to_end(monkeypatch):
    captured: List[Dict[str, Any]] = []

    def fake_rest(self, method, path, *, params=None, json_body=None, prefer=None):
        captured.append({"method": method, "path": path,
                         "json_body": json_body})
        if method == "GET" and path == "convergence_assessments":
            return [_make_assessment_row()]
        if method == "GET" and path == "fda_assets":
            return [_make_asset_row()]
        if method == "GET" and path == "sub_agent_calls":
            return [
                _make_specialist("literature"),
                _make_specialist("competitive"),
                _make_specialist("regulatory_history"),
                _make_specialist("options_microstructure"),
            ]
        if method == "GET" and path == "fda_regulatory_events":
            return [{"id": "event-1"}]
        if method == "POST" and path == "fda_agent_reviews":
            return [{"id": "review-new-1"}]
        return []

    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    sb = _stub_client(fake_rest)

    fake_runner = _FakeRunner(output={
        "schema_version": 1,
        "asset_id": "asset-1",
        "thesis": {"direction": "long", "headline": "PDUFA likely"},
        "kill_conditions": [{"trigger": "CRL", "rationale": "..."}],
    })

    out = ic_memo_runner.run_ic_memo(
        sb, "assess-1",
        question="Custom synthesis prompt for AXS-05.",
        runner=fake_runner,
    )

    # Runner saw the full asset_context shape
    assert len(fake_runner.calls) == 1
    ctx = fake_runner.calls[0]["asset_context"]
    assert ctx["asset"]["ticker"] == "AXSM"
    assert set(ctx["specialists"].keys()) == {
        "literature", "competitive", "regulatory_history",
        "options_microstructure",
    }
    assert ctx["thesis"]["direction"] == "long"
    assert fake_runner.calls[0]["question"] == "Custom synthesis prompt for AXS-05."

    # Result returned + persisted
    assert out["sub_agent_call_id"] == "review-new-1"
    assert out["assessment_id"] == "assess-1"
    assert out["output"]["thesis"]["direction"] == "long"
    assert out["cost_usd"] == 0.05
    assert out["tokens_input"] == 1500
    assert out["tokens_output"] == 600
    assert out["latency_ms"] == 4200

    # Persistence happened with correct shape
    posts = [c for c in captured
             if c["method"] == "POST" and c["path"] == "fda_agent_reviews"]
    assert len(posts) == 1
    assert posts[0]["json_body"]["agent_kind"] == "ic_memo"
    assert posts[0]["json_body"]["event_id"] == "event-1"


def test_run_ic_memo_uses_default_question_when_none(monkeypatch):
    def fake_rest(self, method, path, *, params=None, json_body=None, prefer=None):
        if method == "GET" and path == "convergence_assessments":
            return [_make_assessment_row()]
        if method == "GET" and path == "fda_assets":
            return [_make_asset_row()]
        if method == "GET" and path == "sub_agent_calls":
            return [_make_specialist("literature")]
        if method == "GET" and path == "fda_regulatory_events":
            return [{"id": "event-1"}]
        if method == "POST" and path == "fda_agent_reviews":
            return [{"id": "review-1"}]
        return []

    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    sb = _stub_client(fake_rest)

    fake_runner = _FakeRunner(output={"schema_version": 1})
    ic_memo_runner.run_ic_memo(sb, "assess-1", runner=fake_runner)
    assert fake_runner.calls[0]["question"] == ic_memo_runner.DEFAULT_IC_MEMO_QUESTION


def test_run_ic_memo_persist_false_skips_db_write(monkeypatch):
    captured: List[Dict[str, Any]] = []

    def fake_rest(self, method, path, *, params=None, json_body=None, prefer=None):
        captured.append({"method": method, "path": path})
        if method == "GET" and path == "convergence_assessments":
            return [_make_assessment_row()]
        if method == "GET" and path == "fda_assets":
            return [_make_asset_row()]
        if method == "GET" and path == "sub_agent_calls":
            return [_make_specialist("literature")]
        return []

    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    sb = _stub_client(fake_rest)

    fake_runner = _FakeRunner(output={"schema_version": 1})
    out = ic_memo_runner.run_ic_memo(
        sb, "assess-1", runner=fake_runner, persist=False,
    )
    assert out["sub_agent_call_id"] is None
    posts = [c for c in captured
             if c["method"] == "POST" and c["path"] == "fda_agent_reviews"]
    assert posts == []


def test_run_ic_memo_propagates_schema_error(monkeypatch):
    """When the inner runner raises SubAgentSchemaError, the orchestrator
    does NOT swallow it (caller decides whether to retry / surface)."""

    class _RaisingRunner:
        def run(self, **kwargs):
            raise SubAgentSchemaError(
                "ic_memo",
                ["schema_version: required"],
                payload={"partial_output": True},
            )

    def fake_rest(self, method, path, *, params=None, json_body=None, prefer=None):
        if method == "GET" and path == "convergence_assessments":
            return [_make_assessment_row()]
        if method == "GET" and path == "fda_assets":
            return [_make_asset_row()]
        if method == "GET" and path == "sub_agent_calls":
            return [_make_specialist("literature")]
        return []

    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    sb = _stub_client(fake_rest)

    with pytest.raises(SubAgentSchemaError):
        ic_memo_runner.run_ic_memo(sb, "assess-1", runner=_RaisingRunner())


def test_run_ic_memo_propagates_orchestration_error_pre_runner(monkeypatch):
    """If the assessment is missing, we never reach the runner."""
    runner_called = {"yes": False}

    class _Sentinel:
        def run(self, **kwargs):
            runner_called["yes"] = True
            return SubAgentResult(role="ic_memo", schema_pass=True,
                                  schema_retries=0, output={})

    def fake_rest(self, method, path, *, params=None, json_body=None, prefer=None):
        return []  # nothing exists

    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    sb = _stub_client(fake_rest)

    with pytest.raises(ic_memo_runner.ICMemoOrchestrationError):
        ic_memo_runner.run_ic_memo(sb, "missing", runner=_Sentinel())
    assert not runner_called["yes"]
