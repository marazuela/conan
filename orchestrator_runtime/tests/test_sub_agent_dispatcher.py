"""Tests for orchestrator_runtime.sub_agent_dispatcher.

Run: python -m pytest orchestrator_runtime/tests/test_sub_agent_dispatcher.py -v
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

from orchestrator_runtime import sub_agent_dispatcher as disp
from modal_workers.sub_agents import ROLE_REGISTRY
from modal_workers.sub_agents.runtime import SubAgentResult, SubAgentSchemaError


# ---------- helpers ----------


class _FakeRunner:
    """Minimal runner that returns a canned schema-valid payload."""

    role = ""

    @classmethod
    def for_role(cls, role: str, payload: Dict[str, Any]):
        klass = type(f"FakeRunner_{role}", (cls,), {"role": role, "_payload": payload})
        return klass

    def __init__(self):
        pass

    def run(self, *, question: str, asset_context: Dict[str, Any], budget_token_cap=None):
        return SubAgentResult(
            role=self.role,
            schema_pass=True,
            schema_retries=0,
            output=self._payload,
            tokens_input=200,
            tokens_output=300,
            cost_usd=0.005,
            latency_ms=1500,
            tool_call_log=[{"name": "fake_tool", "input": {}, "turn": 0}],
        )


class _FailingRunner:
    role = ""

    @classmethod
    def for_role(cls, role: str):
        return type(f"FailingRunner_{role}", (cls,), {"role": role})

    def __init__(self):
        pass

    def run(self, *, question: str, asset_context: Dict[str, Any], budget_token_cap=None):
        raise SubAgentSchemaError(
            self.role,
            ["['papers']: required"],
            payload={"asset_id": "00000000-0000-0000-0000-000000000000"},
        )


# ---------- dispatch_sub_agent ----------


def test_dispatch_unknown_role_returns_error_outcome():
    out = disp.dispatch_sub_agent("not_a_role", "test")
    assert out.schema_pass is False
    assert out.role == "not_a_role"
    assert any("unknown role" in e for e in out.errors)


def test_dispatch_skips_role_when_kill_switch_set(monkeypatch):
    """ORCH_DISABLE_<ROLE>=1 short-circuits the runner; the dispatch loop
    keeps moving without crashing the rest of Stage 1."""
    monkeypatch.setenv("ORCH_DISABLE_LITERATURE", "1")
    disp.reset_budget()
    fake = _FakeRunner.for_role("literature", {"schema_version": 1, "asset_id": "x"})
    with patch.dict(ROLE_REGISTRY, {"literature": fake}, clear=False):
        out = disp.dispatch_sub_agent("literature", "find papers")
    assert out.schema_pass is False
    assert out.role == "literature"
    assert any("role_disabled" in e for e in out.errors)
    # Runner was NOT called — token budget untouched.
    assert out.tokens == 0


def test_dispatch_unaffected_when_other_role_disabled(monkeypatch):
    """Disabling literature doesn't block competitive."""
    monkeypatch.setenv("ORCH_DISABLE_LITERATURE", "1")
    disp.reset_budget()
    fake = _FakeRunner.for_role("competitive", {"schema_version": 1})
    with patch.dict(ROLE_REGISTRY, {"competitive": fake}, clear=False), \
         patch.object(disp, "_log_call", return_value="call-id-c"):
        out = disp.dispatch_sub_agent("competitive", "scan pipeline")
    assert out.schema_pass is True
    assert out.role == "competitive"


def test_dispatch_routes_to_runner_and_returns_outcome():
    disp.reset_budget()
    fake = _FakeRunner.for_role("literature", {"schema_version": 1, "asset_id": "x"})
    with patch.dict(ROLE_REGISTRY, {"literature": fake}, clear=False), \
         patch.object(disp, "_log_call", return_value="call-id-1"):
        out = disp.dispatch_sub_agent("literature", "find papers")
    assert out.schema_pass is True
    assert out.role == "literature"
    assert out.tokens == 500
    assert out.sub_agent_call_id == "call-id-1"
    assert out.output["schema_version"] == 1


def test_schema_failure_logs_to_dlq_and_returns_failure():
    disp.reset_budget()
    failing = _FailingRunner.for_role("literature")
    dlq_calls: List[Dict[str, Any]] = []

    def _capture_dlq(role, errors, payload):
        dlq_calls.append({"role": role, "errors": errors, "payload": payload})

    with patch.dict(ROLE_REGISTRY, {"literature": failing}, clear=False), \
         patch.object(disp, "_log_to_dlq", side_effect=_capture_dlq), \
         patch.object(disp, "_log_call", return_value=None):
        out = disp.dispatch_sub_agent("literature", "find papers")
    assert out.schema_pass is False
    assert dlq_calls and dlq_calls[0]["role"] == "literature"
    assert any("required" in e for e in dlq_calls[0]["errors"])


def test_budget_exhaustion_blocks_subsequent_calls():
    disp.reset_budget()
    fake = _FakeRunner.for_role("literature", {"schema_version": 1, "asset_id": "x"})
    with patch.dict(ROLE_REGISTRY, {"literature": fake}, clear=False), \
         patch.object(disp, "_log_call", return_value="c1"):
        # First call costs 500 tokens; cap at 400 → second call blocked
        out1 = disp.dispatch_sub_agent("literature", "q1", budget_token_cap=400)
        assert out1.schema_pass is True
        out2 = disp.dispatch_sub_agent("literature", "q2", budget_token_cap=400)
        assert out2.schema_pass is False
        assert any("budget_exhausted" in e for e in out2.errors)


def test_per_role_budget_caps_each_role(monkeypatch):
    """Each role is capped at PER_ROLE_BUDGET_TOKENS even when the global
    aggregate has plenty left — so dispatch ORDER can't starve later roles
    (the {"partial_output": true} failure mode, 2026-06-02)."""
    monkeypatch.setattr(disp, "PER_ROLE_BUDGET_TOKENS", 200_000)
    monkeypatch.setattr(disp, "DEFAULT_BUDGET_TOKENS", 800_000)
    disp.reset_budget()
    seen: Dict[str, Any] = {}

    class _RecordingRunner:
        role = "competitive"

        def __init__(self):
            pass

        def run(self, *, question, asset_context, budget_token_cap=None):
            seen["budget"] = budget_token_cap
            return SubAgentResult(
                role=self.role, schema_pass=True, schema_retries=0,
                output={"schema_version": 1}, tokens_input=100, tokens_output=100,
                cost_usd=0.001, latency_ms=10, tool_call_log=[],
            )

    with patch.dict(ROLE_REGISTRY, {"competitive": _RecordingRunner}, clear=False), \
         patch.object(disp, "_log_call", return_value="c"):
        # No budget_token_cap passed → cap = DEFAULT_BUDGET_TOKENS (800k global).
        disp.dispatch_sub_agent("competitive", "q")
    # Runner received the 200k per-role cap, NOT the 800k global remaining.
    assert seen["budget"] == 200_000


def test_dispatch_tool_handler_returns_serializable_dict():
    disp.reset_budget()
    fake = _FakeRunner.for_role("competitive", {"schema_version": 1, "asset_id": "x", "competitors": []})
    with patch.dict(ROLE_REGISTRY, {"competitive": fake}, clear=False), \
         patch.object(disp, "_log_call", return_value="c-id"):
        result = disp.dispatch_sub_agent_tool(
            {"role": "competitive", "question": "who else has this MOA?"},
            asset_context={"ticker": "FOO", "indication": "X"},
            assessment_id="asmt-1",
        )
    assert result["role"] == "competitive"
    assert result["schema_pass"] is True
    assert result["metadata"]["sub_agent_call_id"] == "c-id"
    import json as _json
    _json.dumps(result)  # must be JSON-serializable


def test_dispatch_tool_def_has_five_roles():
    enum_roles = disp.DISPATCH_TOOL_DEF["input_schema"]["properties"]["role"]["enum"]
    assert set(enum_roles) == {
        "literature", "competitive", "regulatory_history", "options_microstructure",
        "commercial_opportunity",
    }


# ---------- backfill_assessment_id ----------


def test_backfill_assessment_id_updates_rows_by_run_id():
    captured: Dict[str, Any] = {}

    def _fake_rest(method, table, *, params=None, json_body=None, prefer=None):
        captured.update(
            method=method, table=table, params=params or {},
            json_body=json_body or {}, prefer=prefer,
        )
        return [{"id": "row-1"}, {"id": "row-2"}, {"id": "row-3"}]

    with patch.object(disp._client(), "_rest", side_effect=_fake_rest):
        n = disp.backfill_assessment_id(
            orchestrator_run_id="run-abc",
            assessment_id="asmt-xyz",
        )
    assert n == 3
    assert captured["method"] == "PATCH"
    assert captured["table"] == "sub_agent_calls"
    assert captured["params"]["orchestrator_run_id"] == "eq.run-abc"
    assert captured["params"]["assessment_id"] == "is.null"  # don't clobber existing
    assert captured["json_body"] == {"assessment_id": "asmt-xyz"}


def test_backfill_assessment_id_returns_zero_on_rest_failure():
    def _raise(*_args, **_kwargs):
        raise RuntimeError("supabase 5xx")

    with patch.object(disp._client(), "_rest", side_effect=_raise):
        n = disp.backfill_assessment_id(
            orchestrator_run_id="run-abc",
            assessment_id="asmt-xyz",
        )
    assert n == 0  # best-effort: orphans a row but does not unwind the assessment


def test_backfill_assessment_id_no_op_on_missing_inputs():
    # No back-fill if either input is empty — guards against accidental
    # mass-UPDATEs from a malformed orchestrator_run_id.
    with patch.object(disp._client(), "_rest") as mock_rest:
        assert disp.backfill_assessment_id(orchestrator_run_id="", assessment_id="a") == 0
        assert disp.backfill_assessment_id(orchestrator_run_id="r", assessment_id="") == 0
        mock_rest.assert_not_called()
