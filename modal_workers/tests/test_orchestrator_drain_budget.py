"""Tests for orchestrator_drain_queue's budget handling.

Verifies the three exit paths:
  - normal completion → status='completed' + cost_actual_usd written
  - BudgetExceededError → status='killed_budget' + partial cost_actual_usd
  - other exception → status='failed' (no cost write)

The drain function is large and pulls Modal at import; we test the inline
critical-path logic via direct simulation rather than importing the Modal app.

Run: python -m pytest modal_workers/tests/test_orchestrator_drain_budget.py -v
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

from orchestrator_runtime.client import BudgetExceededError, OrchestratorClient


def _drain_one(sb, run_row, run_one_fn, a_client):
    """Direct port of the per-row drain logic in
    modal_workers/orchestrator_app.py::orchestrator_drain_queue. Kept here
    so we can exercise it without spinning up Modal. Updated when
    orchestrator_drain_queue itself changes."""
    from modal_workers.shared.cost_budget import (
        PER_RUN_HARD_KILL_USD, check_24h_thresholds,
    )

    run_id = run_row["id"]
    asset_id = run_row["asset_id"]
    trigger = run_row["trigger_type"]

    sb._rest(
        "PATCH", "orchestrator_runs",
        params={"id": f"eq.{run_id}"},
        json_body={
            "status": "running",
            "started_at": datetime.now(timezone.utc).isoformat(),
        },
    )

    try:
        aid = run_one_fn(
            sb, a_client,
            asset_id=asset_id,
            trigger_type=trigger,
            run_id=run_id,
            hard_kill_usd=PER_RUN_HARD_KILL_USD,
        )
        cost_rows = sb._rest(
            "GET", "convergence_assessments",
            params={"id": f"eq.{aid}", "select": "cost_usd"},
        ) or []
        cost_actual = (
            float(cost_rows[0]["cost_usd"])
            if cost_rows and cost_rows[0].get("cost_usd") is not None
            else None
        )
        sb._rest(
            "PATCH", "orchestrator_runs",
            params={"id": f"eq.{run_id}"},
            json_body={
                "status": "completed",
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "assessment_id": aid,
                "cost_actual_usd": cost_actual,
            },
        )
        outcome = "completed"
    except BudgetExceededError as exc:
        partial = a_client.get_accumulated_cost()
        sb._rest(
            "PATCH", "orchestrator_runs",
            params={"id": f"eq.{run_id}"},
            json_body={
                "status": "killed_budget",
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "cost_actual_usd": round(partial, 4),
                "error_message": str(exc)[:1000],
            },
        )
        outcome = "killed_budget"
    except Exception as exc:
        sb._rest(
            "PATCH", "orchestrator_runs",
            params={"id": f"eq.{run_id}"},
            json_body={
                "status": "failed",
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "error_message": str(exc)[:1000],
            },
        )
        outcome = "failed"

    try:
        check_24h_thresholds(sb, asset_id)
    except Exception:
        pass

    return outcome


def _make_sb():
    sb = MagicMock()
    sb._rest = MagicMock(return_value=[])
    return sb


def _capture_patches(sb):
    """Return the list of (params_id, json_body) for each PATCH on
    orchestrator_runs."""
    calls = []
    for call in sb._rest.call_args_list:
        method = call.args[0] if call.args else call.kwargs.get("method")
        path = call.args[1] if len(call.args) > 1 else call.kwargs.get("path")
        if method == "PATCH" and path == "orchestrator_runs":
            calls.append(call.kwargs["json_body"])
    return calls


# ---------------------------------------------------------------------------
# Normal completion path
# ---------------------------------------------------------------------------

def test_drain_completed_writes_cost_actual_usd():
    sb = MagicMock()

    def _rest(method, path, **kwargs):
        if method == "GET" and path == "convergence_assessments":
            return [{"cost_usd": 4.25}]
        return []

    sb._rest = MagicMock(side_effect=_rest)
    a_client = MagicMock()
    a_client.get_accumulated_cost = MagicMock(return_value=0.0)
    run_one = MagicMock(return_value="assessment-1")

    outcome = _drain_one(sb, {
        "id": "run-1", "asset_id": "asset-1", "trigger_type": "manual",
    }, run_one, a_client)

    assert outcome == "completed"
    patches = _capture_patches(sb)
    # First PATCH = mark running, second = mark completed
    assert len(patches) == 2
    final = patches[1]
    assert final["status"] == "completed"
    assert final["cost_actual_usd"] == 4.25
    assert final["assessment_id"] == "assessment-1"


# ---------------------------------------------------------------------------
# BudgetExceededError path → killed_budget
# ---------------------------------------------------------------------------

def test_drain_budget_kill_writes_partial_cost_and_killed_budget():
    sb = _make_sb()
    a_client = MagicMock()
    a_client.get_accumulated_cost = MagicMock(return_value=15.42)

    def raise_budget(*args, **kwargs):
        raise BudgetExceededError(
            run_id=kwargs.get("run_id"),
            ceiling_usd=15.0, accumulated_usd=15.42,
        )

    run_one = MagicMock(side_effect=raise_budget)

    outcome = _drain_one(sb, {
        "id": "run-1", "asset_id": "asset-1", "trigger_type": "manual",
    }, run_one, a_client)

    assert outcome == "killed_budget"
    patches = _capture_patches(sb)
    final = patches[-1]
    assert final["status"] == "killed_budget"
    assert final["cost_actual_usd"] == 15.42
    assert "Budget exceeded" in final["error_message"]


def test_drain_budget_kill_does_not_set_assessment_id():
    sb = _make_sb()
    a_client = MagicMock()
    a_client.get_accumulated_cost = MagicMock(return_value=15.5)
    run_one = MagicMock(side_effect=BudgetExceededError("r", 15.0, 15.5))

    _drain_one(sb, {
        "id": "run-1", "asset_id": "asset-1", "trigger_type": "manual",
    }, run_one, a_client)

    final = _capture_patches(sb)[-1]
    # Killed_budget runs have no assessment to point at
    assert "assessment_id" not in final


# ---------------------------------------------------------------------------
# Generic exception → failed (no cost write)
# ---------------------------------------------------------------------------

def test_drain_generic_exception_marks_failed_no_cost():
    sb = _make_sb()
    a_client = MagicMock()
    a_client.get_accumulated_cost = MagicMock(return_value=2.0)
    run_one = MagicMock(side_effect=ValueError("kaboom"))

    outcome = _drain_one(sb, {
        "id": "run-1", "asset_id": "asset-1", "trigger_type": "manual",
    }, run_one, a_client)

    assert outcome == "failed"
    final = _capture_patches(sb)[-1]
    assert final["status"] == "failed"
    assert "kaboom" in final["error_message"]
    assert "cost_actual_usd" not in final


# ---------------------------------------------------------------------------
# 24h threshold check fires regardless of outcome
# ---------------------------------------------------------------------------

def test_drain_runs_24h_threshold_check_after_kill():
    sb = MagicMock()
    threshold_calls = []

    def _rest(method, path, **kwargs):
        if path.startswith("rpc/"):
            raise Exception("no rpc")
        if method == "GET" and path == "convergence_assessments":
            params = kwargs.get("params", {})
            if "asset_id" in params:
                threshold_calls.append("asset")
                return []
            threshold_calls.append("global")
            return []
        return []

    sb._rest = MagicMock(side_effect=_rest)
    a_client = MagicMock()
    a_client.get_accumulated_cost = MagicMock(return_value=15.0)
    run_one = MagicMock(side_effect=BudgetExceededError("r", 15.0, 15.0))

    _drain_one(sb, {
        "id": "run-1", "asset_id": "asset-1", "trigger_type": "manual",
    }, run_one, a_client)

    # check_24h_thresholds queries asset and global
    assert "asset" in threshold_calls
    assert "global" in threshold_calls


# ---------------------------------------------------------------------------
# Failure isolation: cost_actual_usd lookup error doesn't break drain
# ---------------------------------------------------------------------------

def test_drain_handles_missing_cost_row():
    sb = MagicMock()

    def _rest(method, path, **kwargs):
        if method == "GET" and path == "convergence_assessments":
            return []  # empty — assessment row not yet persisted
        return []

    sb._rest = MagicMock(side_effect=_rest)
    a_client = MagicMock()
    a_client.get_accumulated_cost = MagicMock(return_value=0.0)
    run_one = MagicMock(return_value="aid-1")

    outcome = _drain_one(sb, {
        "id": "run-1", "asset_id": "asset-1", "trigger_type": "manual",
    }, run_one, a_client)

    assert outcome == "completed"
    final = _capture_patches(sb)[-1]
    assert final["cost_actual_usd"] is None
