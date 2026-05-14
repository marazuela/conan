"""Tests for the Phase 4B compute_v3 multiplex dispatcher.

The dispatcher is a single FastAPI endpoint that routes
{action, args} bodies to the right runtime helper. We test the pure
`_dispatch_compute_v3_action` and `_verify_compute_secret` helpers
directly so the tests don't pull in the Modal app at import time
(matching the existing `test_orchestrator_drain_budget.py` pattern).

Run: python3 -m pytest modal_workers/tests/test_compute_v3_dispatch.py -v
"""
from __future__ import annotations

import os
from typing import Any, Dict, List

import pytest

os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")


# ---------------------------------------------------------------------------
# _verify_compute_secret
# ---------------------------------------------------------------------------

def test_verify_compute_secret_passes_on_match(monkeypatch):
    monkeypatch.setenv("CONAN_COMPUTE_SECRET", "matching-secret")
    from modal_workers.orchestrator_app import _verify_compute_secret

    # No raise = pass.
    _verify_compute_secret("matching-secret")


def test_verify_compute_secret_raises_401_on_mismatch(monkeypatch):
    monkeypatch.setenv("CONAN_COMPUTE_SECRET", "expected-secret")
    from fastapi import HTTPException

    from modal_workers.orchestrator_app import _verify_compute_secret

    with pytest.raises(HTTPException) as exc_info:
        _verify_compute_secret("wrong-secret")
    assert exc_info.value.status_code == 401


def test_verify_compute_secret_raises_401_on_missing_header(monkeypatch):
    monkeypatch.setenv("CONAN_COMPUTE_SECRET", "expected-secret")
    from fastapi import HTTPException

    from modal_workers.orchestrator_app import _verify_compute_secret

    with pytest.raises(HTTPException) as exc_info:
        _verify_compute_secret(None)
    assert exc_info.value.status_code == 401


def test_verify_compute_secret_raises_500_on_server_misconfig(monkeypatch):
    monkeypatch.delenv("CONAN_COMPUTE_SECRET", raising=False)
    from fastapi import HTTPException

    from modal_workers.orchestrator_app import _verify_compute_secret

    with pytest.raises(HTTPException) as exc_info:
        _verify_compute_secret("anything")
    assert exc_info.value.status_code == 500


# ---------------------------------------------------------------------------
# _dispatch_compute_v3_action
# ---------------------------------------------------------------------------

def test_dispatch_unknown_action_raises_400():
    from fastapi import HTTPException

    from modal_workers.orchestrator_app import _dispatch_compute_v3_action

    with pytest.raises(HTTPException) as exc_info:
        _dispatch_compute_v3_action("do_the_dishes", {})
    assert exc_info.value.status_code == 400
    detail = exc_info.value.detail
    assert "valid_actions" in detail
    assert "tier2_bulk_enqueue" in detail["valid_actions"]


def test_dispatch_routes_tier2_bulk_enqueue(monkeypatch):
    """Ensure the dispatcher passes through to enqueue_tier2_bulk."""
    captured: Dict[str, Any] = {}

    def fake_enqueue(sb, asset_ids):
        captured["sb"] = sb
        captured["asset_ids"] = asset_ids
        return {"enqueued": [{"asset_id": "a", "run_id": "r"}],
                "failed": [], "enqueued_count": 1, "failed_count": 0}

    monkeypatch.setattr("orchestrator_runtime.tier2.enqueue_tier2_bulk",
                        fake_enqueue)
    from modal_workers.orchestrator_app import _dispatch_compute_v3_action

    out = _dispatch_compute_v3_action(
        "tier2_bulk_enqueue", {"asset_ids": ["a1", "a2"]},
    )
    assert out["enqueued_count"] == 1
    assert captured["asset_ids"] == ["a1", "a2"]


def test_dispatch_routes_tier2_complete(monkeypatch):
    captured: Dict[str, Any] = {}

    def fake_complete(sb, run_id, payload, *, cost_usd=0.0, latency_ms=None):
        captured["run_id"] = run_id
        captured["payload"] = payload
        captured["cost_usd"] = cost_usd
        captured["latency_ms"] = latency_ms
        return {"status": "completed", "assessment_id": "a-1",
                "escalated": False, "escalation_reasons": [],
                "escalation_run_id": None}

    monkeypatch.setattr("orchestrator_runtime.tier2.complete_tier2_run",
                        fake_complete)
    from modal_workers.orchestrator_app import _dispatch_compute_v3_action

    out = _dispatch_compute_v3_action("tier2_complete", {
        "run_id": "run-1",
        "payload": {"tier": 2, "thesis_direction": "long"},
        "cost_usd": 0.42,
        "latency_ms": 45000,
    })
    assert out["status"] == "completed"
    assert captured["run_id"] == "run-1"
    assert captured["cost_usd"] == 0.42
    assert captured["latency_ms"] == 45000


def test_dispatch_tier2_complete_uses_defaults_when_optional_args_missing(monkeypatch):
    """cost_usd and latency_ms are optional in the contract; verify defaults."""
    captured: Dict[str, Any] = {}

    def fake_complete(sb, run_id, payload, *, cost_usd=0.0, latency_ms=None):
        captured["cost_usd"] = cost_usd
        captured["latency_ms"] = latency_ms
        return {"status": "completed"}

    monkeypatch.setattr("orchestrator_runtime.tier2.complete_tier2_run",
                        fake_complete)
    from modal_workers.orchestrator_app import _dispatch_compute_v3_action

    _dispatch_compute_v3_action("tier2_complete", {
        "run_id": "run-1",
        "payload": {},
    })
    assert captured["cost_usd"] == 0.0
    assert captured["latency_ms"] is None


def test_dispatch_routes_tier2_fail(monkeypatch):
    captured: Dict[str, Any] = {}

    def fake_fail(sb, run_id, error_message):
        captured["run_id"] = run_id
        captured["error_message"] = error_message
        return {"run_id": run_id, "status": "failed"}

    monkeypatch.setattr("orchestrator_runtime.tier2.fail_tier2_run",
                        fake_fail)
    from modal_workers.orchestrator_app import _dispatch_compute_v3_action

    out = _dispatch_compute_v3_action("tier2_fail", {
        "run_id": "run-9", "error_message": "modal cold-start timeout",
    })
    assert out["status"] == "failed"
    assert captured["error_message"] == "modal cold-start timeout"


def test_dispatch_routes_ic_memo_run(monkeypatch):
    captured: Dict[str, Any] = {}

    def fake_run_ic_memo(sb, assessment_id, *, question=None, persist=True,
                          a_client=None, runner=None):
        captured["assessment_id"] = assessment_id
        captured["question"] = question
        captured["persist"] = persist
        return {"sub_agent_call_id": "sac-1",
                "assessment_id": assessment_id,
                "output": {}, "tokens_input": 0, "tokens_output": 0,
                "cost_usd": 0.0, "latency_ms": 0, "wall_seconds": 0}

    monkeypatch.setattr("orchestrator_runtime.ic_memo_runner.run_ic_memo",
                        fake_run_ic_memo)
    from modal_workers.orchestrator_app import _dispatch_compute_v3_action

    out = _dispatch_compute_v3_action("ic_memo_run", {
        "assessment_id": "assess-1",
        "question": "Custom prompt.",
        "persist": False,
    })
    assert out["sub_agent_call_id"] == "sac-1"
    assert captured["question"] == "Custom prompt."
    assert captured["persist"] is False


def test_dispatch_ic_memo_run_uses_defaults(monkeypatch):
    captured: Dict[str, Any] = {}

    def fake_run_ic_memo(sb, assessment_id, *, question=None, persist=True,
                          a_client=None, runner=None):
        captured["question"] = question
        captured["persist"] = persist
        return {"sub_agent_call_id": "sac-1"}

    monkeypatch.setattr("orchestrator_runtime.ic_memo_runner.run_ic_memo",
                        fake_run_ic_memo)
    from modal_workers.orchestrator_app import _dispatch_compute_v3_action

    _dispatch_compute_v3_action("ic_memo_run", {"assessment_id": "a-1"})
    assert captured["question"] is None
    assert captured["persist"] is True


def test_dispatch_required_args_missing_raises_keyerror(monkeypatch):
    """Missing required args bubble as KeyError → FastAPI translates to
    a 500 on production. Tests assert the exception type rather than HTTP
    response code (the dispatcher itself doesn't translate)."""
    from modal_workers.orchestrator_app import _dispatch_compute_v3_action

    with pytest.raises(KeyError, match="asset_ids"):
        _dispatch_compute_v3_action("tier2_bulk_enqueue", {})

    with pytest.raises(KeyError, match="run_id"):
        _dispatch_compute_v3_action("tier2_complete", {"payload": {}})

    with pytest.raises(KeyError, match="run_id"):
        _dispatch_compute_v3_action("tier2_fail", {})

    with pytest.raises(KeyError, match="assessment_id"):
        _dispatch_compute_v3_action("ic_memo_run", {})


# ---------------------------------------------------------------------------
# Action registry
# ---------------------------------------------------------------------------

def test_compute_v3_actions_set_matches_dispatcher_branches():
    """Defensive: the COMPUTE_V3_ACTIONS frozenset must exactly match the
    set of actions the dispatcher knows how to route. Adding a new
    action without updating both should be caught here."""
    from modal_workers.orchestrator_app import COMPUTE_V3_ACTIONS

    assert COMPUTE_V3_ACTIONS == frozenset({
        "tier2_bulk_enqueue",
        "tier2_complete",
        "tier2_fail",
        "ic_memo_run",
        "feedback_loop_kickoff",
        "orchestrator_drain_queue",
        "asset_linker_run",
        "asset_linker_pass2_run",
        "fact_extractor_run",
    })


# ---------------------------------------------------------------------------
# feedback_loop_kickoff — fire-and-forget spawn into conan-v3-feedback-loop
# ---------------------------------------------------------------------------

def test_dispatch_feedback_loop_kickoff_spawns_remote_fn(monkeypatch):
    """The kickoff action looks up daily_feedback_loop in the deployed
    feedback-loop app and spawns it. The endpoint must return the
    function_call_id without blocking on the (up to 7200s) chain."""
    spawned: Dict[str, Any] = {}

    class _Handle:
        object_id = "fc-abc123"

    class _FakeFn:
        def spawn(self, **kwargs):
            spawned["kwargs"] = kwargs
            return _Handle()

    def fake_from_name(app_name, fn_name):
        spawned["app"] = app_name
        spawned["fn"] = fn_name
        return _FakeFn()

    import modal as _modal
    monkeypatch.setattr(_modal.Function, "from_name", staticmethod(fake_from_name))
    from modal_workers.orchestrator_app import _dispatch_compute_v3_action

    out = _dispatch_compute_v3_action("feedback_loop_kickoff", {})
    assert out == {"spawned": True, "function_call_id": "fc-abc123"}
    assert spawned["app"] == "conan-v3-feedback-loop"
    assert spawned["fn"] == "daily_feedback_loop"
    assert spawned["kwargs"] == {}


def test_dispatch_feedback_loop_kickoff_passes_through_optional_args(monkeypatch):
    """Optional knobs (drain_batch_size, monitor_window_days, refit_min_n,
    refit_bootstrap_resamples) must reach the spawned function untouched.
    Unknown keys must NOT be forwarded so daily_feedback_loop's signature
    stays the source of truth for valid kwargs."""
    spawned: Dict[str, Any] = {}

    class _Handle:
        object_id = "fc-xyz789"

    class _FakeFn:
        def spawn(self, **kwargs):
            spawned["kwargs"] = kwargs
            return _Handle()

    import modal as _modal
    monkeypatch.setattr(_modal.Function, "from_name",
                        staticmethod(lambda *a, **kw: _FakeFn()))
    from modal_workers.orchestrator_app import _dispatch_compute_v3_action

    out = _dispatch_compute_v3_action("feedback_loop_kickoff", {
        "drain_batch_size": 100,
        "monitor_window_days": 14,
        "refit_min_n": 50,
        "refit_bootstrap_resamples": 1000,
        "ignored_extra_key": "should_not_pass_through",
    })
    assert out["spawned"] is True
    assert spawned["kwargs"] == {
        "drain_batch_size": 100,
        "monitor_window_days": 14,
        "refit_min_n": 50,
        "refit_bootstrap_resamples": 1000,
    }
    assert "ignored_extra_key" not in spawned["kwargs"]


# ---------------------------------------------------------------------------
# orchestrator_drain_queue — fire-and-forget spawn into conan-v3-orchestrator
# ---------------------------------------------------------------------------

def test_dispatch_orchestrator_drain_queue_spawns_remote_fn(monkeypatch):
    """Drain action must look up orchestrator_drain_queue in the deployed
    conan-v3-orchestrator app and spawn it fire-and-forget. The endpoint
    returns function_call_id without blocking on the (up to 3600s) drain."""
    spawned: Dict[str, Any] = {}

    class _Handle:
        object_id = "fc-drain-abc"

    class _FakeFn:
        def spawn(self, **kwargs):
            spawned["kwargs"] = kwargs
            return _Handle()

    def fake_from_name(app_name, fn_name):
        spawned["app"] = app_name
        spawned["fn"] = fn_name
        return _FakeFn()

    import modal as _modal
    monkeypatch.setattr(_modal.Function, "from_name", staticmethod(fake_from_name))
    from modal_workers.orchestrator_app import _dispatch_compute_v3_action

    out = _dispatch_compute_v3_action("orchestrator_drain_queue", {})
    assert out == {"spawned": True, "function_call_id": "fc-drain-abc"}
    assert spawned["app"] == "conan-v3-orchestrator"
    assert spawned["fn"] == "orchestrator_drain_queue"
    assert spawned["kwargs"] == {}


def test_dispatch_orchestrator_drain_queue_passes_max_per_run(monkeypatch):
    """max_per_run is the only valid kwarg; unknown keys must NOT be
    forwarded so orchestrator_drain_queue's signature stays canonical."""
    spawned: Dict[str, Any] = {}

    class _Handle:
        object_id = "fc-drain-xyz"

    class _FakeFn:
        def spawn(self, **kwargs):
            spawned["kwargs"] = kwargs
            return _Handle()

    import modal as _modal
    monkeypatch.setattr(_modal.Function, "from_name",
                        staticmethod(lambda *a, **kw: _FakeFn()))
    from modal_workers.orchestrator_app import _dispatch_compute_v3_action

    out = _dispatch_compute_v3_action("orchestrator_drain_queue", {
        "max_per_run": 10,
        "ignored_extra_key": "should_not_pass_through",
    })
    assert out["spawned"] is True
    assert spawned["kwargs"] == {"max_per_run": 10}
    assert "ignored_extra_key" not in spawned["kwargs"]


# ---------------------------------------------------------------------------
# asset_linker_run — fire-and-forget spawn into conan-v3-orchestrator
# ---------------------------------------------------------------------------

def test_dispatch_asset_linker_run_spawns_remote_fn(monkeypatch):
    """Pass-1 linker action must look up asset_linker_run in the deployed
    conan-v3-orchestrator app and spawn it fire-and-forget."""
    spawned: Dict[str, Any] = {}

    class _Handle:
        object_id = "fc-linker-abc"

    class _FakeFn:
        def spawn(self, **kwargs):
            spawned["kwargs"] = kwargs
            return _Handle()

    def fake_from_name(app_name, fn_name):
        spawned["app"] = app_name
        spawned["fn"] = fn_name
        return _FakeFn()

    import modal as _modal
    monkeypatch.setattr(_modal.Function, "from_name", staticmethod(fake_from_name))
    from modal_workers.orchestrator_app import _dispatch_compute_v3_action

    out = _dispatch_compute_v3_action("asset_linker_run", {})
    assert out == {"spawned": True, "function_call_id": "fc-linker-abc"}
    assert spawned["app"] == "conan-v3-orchestrator"
    assert spawned["fn"] == "asset_linker_run"
    assert spawned["kwargs"] == {}


def test_dispatch_asset_linker_run_passes_through_optional_args(monkeypatch):
    """asset_id, max_docs, budget_usd must reach the spawned function;
    unknown keys must NOT pass through."""
    spawned: Dict[str, Any] = {}

    class _Handle:
        object_id = "fc-linker-xyz"

    class _FakeFn:
        def spawn(self, **kwargs):
            spawned["kwargs"] = kwargs
            return _Handle()

    import modal as _modal
    monkeypatch.setattr(_modal.Function, "from_name",
                        staticmethod(lambda *a, **kw: _FakeFn()))
    from modal_workers.orchestrator_app import _dispatch_compute_v3_action

    out = _dispatch_compute_v3_action("asset_linker_run", {
        "asset_id": "asset-1",
        "max_docs": 50,
        "budget_usd": 5.0,
        "ignored_extra_key": "should_not_pass_through",
    })
    assert out["spawned"] is True
    assert spawned["kwargs"] == {
        "asset_id": "asset-1",
        "max_docs": 50,
        "budget_usd": 5.0,
    }
    assert "ignored_extra_key" not in spawned["kwargs"]


# ---------------------------------------------------------------------------
# asset_linker_pass2_run — fire-and-forget spawn into conan-v3-orchestrator
# ---------------------------------------------------------------------------

def test_dispatch_asset_linker_pass2_run_spawns_remote_fn(monkeypatch):
    """Pass-2 verifier action must look up asset_linker_pass2_run in the
    deployed conan-v3-orchestrator app and spawn it fire-and-forget."""
    spawned: Dict[str, Any] = {}

    class _Handle:
        object_id = "fc-pass2-abc"

    class _FakeFn:
        def spawn(self, **kwargs):
            spawned["kwargs"] = kwargs
            return _Handle()

    def fake_from_name(app_name, fn_name):
        spawned["app"] = app_name
        spawned["fn"] = fn_name
        return _FakeFn()

    import modal as _modal
    monkeypatch.setattr(_modal.Function, "from_name", staticmethod(fake_from_name))
    from modal_workers.orchestrator_app import _dispatch_compute_v3_action

    out = _dispatch_compute_v3_action("asset_linker_pass2_run", {})
    assert out == {"spawned": True, "function_call_id": "fc-pass2-abc"}
    assert spawned["app"] == "conan-v3-orchestrator"
    assert spawned["fn"] == "asset_linker_pass2_run"
    assert spawned["kwargs"] == {}


def test_dispatch_asset_linker_pass2_run_passes_through_optional_args(monkeypatch):
    """asset_id, max_links, threshold, budget_usd must reach the spawned
    function; unknown keys must NOT pass through."""
    spawned: Dict[str, Any] = {}

    class _Handle:
        object_id = "fc-pass2-xyz"

    class _FakeFn:
        def spawn(self, **kwargs):
            spawned["kwargs"] = kwargs
            return _Handle()

    import modal as _modal
    monkeypatch.setattr(_modal.Function, "from_name",
                        staticmethod(lambda *a, **kw: _FakeFn()))
    from modal_workers.orchestrator_app import _dispatch_compute_v3_action

    out = _dispatch_compute_v3_action("asset_linker_pass2_run", {
        "asset_id": "asset-2",
        "max_links": 100,
        "threshold": 0.75,
        "budget_usd": 1.5,
        "ignored_extra_key": "should_not_pass_through",
    })
    assert out["spawned"] is True
    assert spawned["kwargs"] == {
        "asset_id": "asset-2",
        "max_links": 100,
        "threshold": 0.75,
        "budget_usd": 1.5,
    }
    assert "ignored_extra_key" not in spawned["kwargs"]


# ---------------------------------------------------------------------------
# fact_extractor_run — fire-and-forget spawn into conan-v3-orchestrator
# ---------------------------------------------------------------------------

def test_dispatch_fact_extractor_run_spawns_remote_fn(monkeypatch):
    """Fact extractor action with a valid asset_id must look up
    fact_extractor_run in the deployed conan-v3-orchestrator app and spawn
    it fire-and-forget."""
    spawned: Dict[str, Any] = {}

    class _Handle:
        object_id = "fc-fact-abc"

    class _FakeFn:
        def spawn(self, **kwargs):
            spawned["kwargs"] = kwargs
            return _Handle()

    def fake_from_name(app_name, fn_name):
        spawned["app"] = app_name
        spawned["fn"] = fn_name
        return _FakeFn()

    import modal as _modal
    monkeypatch.setattr(_modal.Function, "from_name", staticmethod(fake_from_name))
    from modal_workers.orchestrator_app import _dispatch_compute_v3_action

    out = _dispatch_compute_v3_action(
        "fact_extractor_run", {"asset_id": "asset-1"},
    )
    assert out == {"spawned": True, "function_call_id": "fc-fact-abc"}
    assert spawned["app"] == "conan-v3-orchestrator"
    assert spawned["fn"] == "fact_extractor_run"
    assert spawned["kwargs"] == {"asset_id": "asset-1"}


def test_dispatch_fact_extractor_run_rejects_blank_args(monkeypatch):
    """Blank-mode invocation (args without asset_id) is rejected — the
    hourly v3-fact-extractor cron was unscheduled 2026-05-11 and the
    dispatcher must not let a stale caller resurrect it accidentally."""
    from fastapi import HTTPException

    # Sanity: even if someone monkeypatches from_name, the validation should
    # short-circuit before Modal is touched. Install a from_name that would
    # blow the test up if reached.
    import modal as _modal

    def _explode(*a, **kw):
        raise AssertionError("from_name must not be called when args lack asset_id")

    monkeypatch.setattr(_modal.Function, "from_name", staticmethod(_explode))
    from modal_workers.orchestrator_app import _dispatch_compute_v3_action

    with pytest.raises(HTTPException) as exc_info:
        _dispatch_compute_v3_action("fact_extractor_run", {})
    assert exc_info.value.status_code == 400
    detail = exc_info.value.detail
    assert "asset_id" in detail["error"]


def test_dispatch_fact_extractor_run_rejects_non_string_asset_id(monkeypatch):
    """asset_id must be a string — bare numerics, None, dicts all rejected."""
    from fastapi import HTTPException

    import modal as _modal
    monkeypatch.setattr(_modal.Function, "from_name",
                        staticmethod(lambda *a, **kw: pytest.fail("unreachable")))
    from modal_workers.orchestrator_app import _dispatch_compute_v3_action

    for bad in (None, 42, {"id": "asset-1"}, ""):
        with pytest.raises(HTTPException) as exc_info:
            _dispatch_compute_v3_action(
                "fact_extractor_run", {"asset_id": bad},
            )
        assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# Lazy fact-extraction hook (Tier-1)
# ---------------------------------------------------------------------------

def test_lazy_extraction_passes_through_on_rc_zero(monkeypatch):
    """rc=0 from the extractor = success (or benign no-op). The hook must
    return None silently — Tier-1 synthesis proceeds."""
    captured: Dict[str, Any] = {}

    def fake_main(argv):
        captured["argv"] = argv
        return 0

    monkeypatch.setattr(
        "modal_workers.extractor.sonnet_fact_extractor.main", fake_main,
    )
    from modal_workers.orchestrator_app import _ensure_facts_extracted_for_asset

    _ensure_facts_extracted_for_asset("asset-xyz")
    # argv shape: ['--asset-id', '<id>', '--max', '<n>', '--budget-usd', '<n>']
    assert "--asset-id" in captured["argv"]
    assert "asset-xyz" in captured["argv"]


def test_lazy_extraction_raises_on_rc3(monkeypatch):
    """rc=3 = systemic exhaustion (credits out / every doc errored). The
    hook must raise FactExtractorExhaustedError so the drain loop's
    exception handler marks the orchestrator_runs row failed."""
    monkeypatch.setattr(
        "modal_workers.extractor.sonnet_fact_extractor.main",
        lambda argv: 3,
    )
    from modal_workers.orchestrator_app import (
        FactExtractorExhaustedError,
        _ensure_facts_extracted_for_asset,
    )

    with pytest.raises(FactExtractorExhaustedError) as exc_info:
        _ensure_facts_extracted_for_asset("asset-doomed")
    assert "asset-doomed" in str(exc_info.value)


def test_lazy_extraction_raises_on_rc2_missing_api_key(monkeypatch):
    """rc=2 = missing ANTHROPIC_API_KEY (config bug, not transient). The
    hook must raise so the run fails visibly rather than silently producing
    a fact-less assessment."""
    monkeypatch.setattr(
        "modal_workers.extractor.sonnet_fact_extractor.main",
        lambda argv: 2,
    )
    from modal_workers.orchestrator_app import _ensure_facts_extracted_for_asset

    with pytest.raises(RuntimeError):
        _ensure_facts_extracted_for_asset("asset-x")


def test_dispatch_fact_extractor_run_passes_through_optional_args(monkeypatch):
    """asset_id, max_links, budget_usd must reach the spawned function;
    unknown keys must NOT pass through."""
    spawned: Dict[str, Any] = {}

    class _Handle:
        object_id = "fc-fact-xyz"

    class _FakeFn:
        def spawn(self, **kwargs):
            spawned["kwargs"] = kwargs
            return _Handle()

    import modal as _modal
    monkeypatch.setattr(_modal.Function, "from_name",
                        staticmethod(lambda *a, **kw: _FakeFn()))
    from modal_workers.orchestrator_app import _dispatch_compute_v3_action

    out = _dispatch_compute_v3_action("fact_extractor_run", {
        "asset_id": "asset-3",
        "max_links": 200,
        "budget_usd": 12.0,
        "ignored_extra_key": "should_not_pass_through",
    })
    assert out["spawned"] is True
    assert spawned["kwargs"] == {
        "asset_id": "asset-3",
        "max_links": 200,
        "budget_usd": 12.0,
    }
    assert "ignored_extra_key" not in spawned["kwargs"]
