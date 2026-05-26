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
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "x")
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
        "seed_fda_asset_aliases_refresh",
        # Phase 3a/3b/4 (added 2026-06-04)
        "earnings_calendar_fetch_daily",
        "fomc_calendar_refresh",
        "q1_audit_run",
        "q2_audit_run",
        "calibration_refit_run",
        "fda_event_harvest_daily",
        # D-129 WI-2 follow-up (2026-05-25)
        "bc_class_precedent_refresh",
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
# LLM ingestion actions — disabled after skill cutover
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "action",
    ["asset_linker_run", "asset_linker_pass2_run", "fact_extractor_run"],
)
def test_dispatch_llm_ingestion_actions_are_disabled(monkeypatch, action):
    """Asset linking and fact extraction are local skill workflows, not
    production Modal/ANTHROPIC_API_KEY actions."""
    import modal as _modal

    def fail_from_name(*_args, **_kwargs):
        raise AssertionError("disabled asset linker action attempted Modal spawn")

    monkeypatch.setattr(_modal.Function, "from_name", staticmethod(fail_from_name))
    from fastapi import HTTPException
    from modal_workers.orchestrator_app import _dispatch_compute_v3_action

    with pytest.raises(HTTPException) as exc_info:
        _dispatch_compute_v3_action(action, {})
    assert exc_info.value.status_code == 400
    assert action not in exc_info.value.detail["valid_actions"]


# ---------------------------------------------------------------------------
# seed_fda_asset_aliases_refresh — weekly alias-index refresh
# ---------------------------------------------------------------------------

def test_dispatch_seed_alias_refresh_is_a_valid_action():
    """The weekly alias-index refresh is a valid compute_v3 action."""
    from modal_workers.orchestrator_app import COMPUTE_V3_ACTIONS
    assert "seed_fda_asset_aliases_refresh" in COMPUTE_V3_ACTIONS


def test_dispatch_seed_alias_refresh_spawns_remote_fn(monkeypatch):
    """The action must look up seed_fda_asset_aliases_refresh in
    conan-v3-orchestrator and spawn it fire-and-forget."""
    spawned: Dict[str, Any] = {}

    class _Handle:
        object_id = "fc-seed-xyz"

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

    out = _dispatch_compute_v3_action("seed_fda_asset_aliases_refresh", {})
    assert out == {"spawned": True, "function_call_id": "fc-seed-xyz"}
    assert spawned["app"] == "conan-v3-orchestrator"
    assert spawned["fn"] == "seed_fda_asset_aliases_refresh"
    assert spawned["kwargs"] == {}


def test_dispatch_seed_alias_refresh_passes_sources_arg(monkeypatch):
    """The ``sources`` arg (if provided) reaches the spawned function;
    extra keys must NOT pass through."""
    spawned: Dict[str, Any] = {}

    class _Handle:
        object_id = "fc-seed-2"

    class _FakeFn:
        def spawn(self, **kwargs):
            spawned["kwargs"] = kwargs
            return _Handle()

    import modal as _modal
    monkeypatch.setattr(_modal.Function, "from_name",
                        staticmethod(lambda *a, **kw: _FakeFn()))
    from modal_workers.orchestrator_app import _dispatch_compute_v3_action

    out = _dispatch_compute_v3_action("seed_fda_asset_aliases_refresh", {
        "sources": "openfda_label",
        "should_be_dropped": True,
    })
    assert out["spawned"] is True
    assert spawned["kwargs"] == {"sources": "openfda_label"}
    assert "should_be_dropped" not in spawned["kwargs"]


# ---------------------------------------------------------------------------
# Phase 3a/3b/4 — spawn actions added 2026-06-04
#
# Each new action wires pg_cron → compute_v3_dispatch → worker via
# modal.Function.from_name + spawn. Tests follow the same fake-from-name
# pattern as the existing spawn-action tests (drain_queue, seed_alias_refresh).
# ---------------------------------------------------------------------------


def _patched_spawn(monkeypatch) -> Dict[str, Any]:
    """Returns a dict that test bodies inspect; monkeypatches
    modal.Function.from_name so any action that spawns is captured."""
    captured: Dict[str, Any] = {}

    class _Handle:
        object_id = "fc-test-handle"

    class _FakeFn:
        def spawn(self, **kwargs):
            captured["kwargs"] = kwargs
            return _Handle()

    def fake_from_name(app_name, fn_name):
        captured["app"] = app_name
        captured["fn"] = fn_name
        return _FakeFn()

    import modal as _modal
    monkeypatch.setattr(_modal.Function, "from_name", staticmethod(fake_from_name))
    return captured


def test_dispatch_earnings_calendar_fetch_daily_spawns_worker(monkeypatch):
    captured = _patched_spawn(monkeypatch)
    from modal_workers.orchestrator_app import _dispatch_compute_v3_action

    out = _dispatch_compute_v3_action("earnings_calendar_fetch_daily", {})
    assert out == {"spawned": True, "function_call_id": "fc-test-handle"}
    assert captured["app"] == "conan-v3-orchestrator"
    assert captured["fn"] == "phase3a_earnings_calendar_fetch_worker"
    assert captured["kwargs"] == {}


def test_dispatch_earnings_calendar_passes_through_window_and_tickers(monkeypatch):
    captured = _patched_spawn(monkeypatch)
    from modal_workers.orchestrator_app import _dispatch_compute_v3_action

    _dispatch_compute_v3_action("earnings_calendar_fetch_daily", {
        "window_days": 14,
        "forward_days": 30,
        "tickers": ["AXSM", "VRDN"],
        "unknown_key": "dropped",
    })
    assert captured["kwargs"] == {
        "window_days": 14, "forward_days": 30, "tickers": ["AXSM", "VRDN"],
    }


def test_dispatch_fomc_calendar_refresh_spawns_worker(monkeypatch):
    captured = _patched_spawn(monkeypatch)
    from modal_workers.orchestrator_app import _dispatch_compute_v3_action

    out = _dispatch_compute_v3_action("fomc_calendar_refresh", {})
    assert out["spawned"] is True
    assert captured["fn"] == "phase3a_fomc_calendar_refresh_worker"
    assert captured["kwargs"] == {}


def test_dispatch_fomc_calendar_refresh_passes_year_arg(monkeypatch):
    captured = _patched_spawn(monkeypatch)
    from modal_workers.orchestrator_app import _dispatch_compute_v3_action

    _dispatch_compute_v3_action("fomc_calendar_refresh", {
        "year": 2026, "ignored": True,
    })
    assert captured["kwargs"] == {"year": 2026}


def test_dispatch_q1_audit_run_spawns_worker(monkeypatch):
    captured = _patched_spawn(monkeypatch)
    from modal_workers.orchestrator_app import _dispatch_compute_v3_action

    out = _dispatch_compute_v3_action("q1_audit_run", {})
    assert out["spawned"] is True
    assert captured["fn"] == "q1_audit_run_worker"
    assert captured["kwargs"] == {}


def test_dispatch_q1_audit_run_passes_re_audit_flag(monkeypatch):
    captured = _patched_spawn(monkeypatch)
    from modal_workers.orchestrator_app import _dispatch_compute_v3_action

    _dispatch_compute_v3_action("q1_audit_run", {
        "re_audit": True, "noise": "discarded",
    })
    assert captured["kwargs"] == {"re_audit": True}


def test_dispatch_q1_audit_run_coerces_re_audit_to_bool(monkeypatch):
    """re_audit is bool-coerced so callers can pass '1' / 'true' / 0
    without exploding the worker signature."""
    captured = _patched_spawn(monkeypatch)
    from modal_workers.orchestrator_app import _dispatch_compute_v3_action

    _dispatch_compute_v3_action("q1_audit_run", {"re_audit": 1})
    assert captured["kwargs"] == {"re_audit": True}


def test_dispatch_q2_audit_run_spawns_worker(monkeypatch):
    captured = _patched_spawn(monkeypatch)
    from modal_workers.orchestrator_app import _dispatch_compute_v3_action

    out = _dispatch_compute_v3_action("q2_audit_run", {})
    assert out["spawned"] is True
    assert captured["fn"] == "q2_audit_run_worker"
    assert captured["kwargs"] == {}


def test_dispatch_q2_audit_run_passes_profile_arg(monkeypatch):
    captured = _patched_spawn(monkeypatch)
    from modal_workers.orchestrator_app import _dispatch_compute_v3_action

    _dispatch_compute_v3_action("q2_audit_run", {
        "profile": "binary_catalyst", "extra": "dropped",
    })
    assert captured["kwargs"] == {"profile": "binary_catalyst"}


def test_dispatch_fda_event_harvest_daily_spawns_worker(monkeypatch):
    captured = _patched_spawn(monkeypatch)
    from modal_workers.orchestrator_app import _dispatch_compute_v3_action

    out = _dispatch_compute_v3_action("fda_event_harvest_daily", {})
    assert out["spawned"] is True
    assert captured["fn"] == "fda_event_harvest_daily_worker"
    assert captured["kwargs"] == {}


def test_dispatch_fda_event_harvest_passes_date_range_args(monkeypatch):
    captured = _patched_spawn(monkeypatch)
    from modal_workers.orchestrator_app import _dispatch_compute_v3_action

    _dispatch_compute_v3_action("fda_event_harvest_daily", {
        "start_date": "2026-05-01",
        "end_date": "2026-06-01",
        "ignored": True,
    })
    assert captured["kwargs"] == {
        "start_date": "2026-05-01", "end_date": "2026-06-01",
    }


def test_dispatch_bc_class_precedent_refresh_spawns_worker(monkeypatch):
    captured = _patched_spawn(monkeypatch)
    from modal_workers.orchestrator_app import _dispatch_compute_v3_action

    out = _dispatch_compute_v3_action("bc_class_precedent_refresh", {})
    assert out["spawned"] is True
    assert captured["fn"] == "bc_class_precedent_refresh_worker"
    assert captured["kwargs"] == {}


def test_dispatch_bc_class_precedent_refresh_passes_tuning_args(monkeypatch):
    captured = _patched_spawn(monkeypatch)
    from modal_workers.orchestrator_app import _dispatch_compute_v3_action

    _dispatch_compute_v3_action("bc_class_precedent_refresh", {
        "lookback_years": 5,
        "apply": False,
        "ignored": True,
    })
    assert captured["kwargs"] == {"lookback_years": 5, "apply": False}


def test_spawn_only_actions_set_is_documented():
    """The _SPAWN_ONLY_ACTIONS map must list every spawn action so the
    multiplex can't develop two ways to spawn (one via if-chain, one via
    map) without intent. Defensive."""
    from modal_workers.orchestrator_app import (
        _SPAWN_ONLY_ACTIONS, COMPUTE_V3_ACTIONS,
    )
    assert set(_SPAWN_ONLY_ACTIONS).issubset(COMPUTE_V3_ACTIONS)
    # The Phase 3a/3b/4 additions should all be in the spawn map.
    for action in (
        "earnings_calendar_fetch_daily", "fomc_calendar_refresh",
        "q1_audit_run", "q2_audit_run", "fda_event_harvest_daily",
        "bc_class_precedent_refresh",
    ):
        assert action in _SPAWN_ONLY_ACTIONS
