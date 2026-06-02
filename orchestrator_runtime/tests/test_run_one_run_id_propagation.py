"""Regression for Bug B (operator_flag b98c32d3 / 4fc126c0).

run_one() receives the orchestrator_runs row id as `run_id` (used for the
budget kill switch) but must ALSO forward it into _run_one_inner, which stamps
it onto the AssessmentRun. If it doesn't, run.orchestrator_run_id stays None ->
Stage 1 writes sub_agent_calls.orchestrator_run_id=NULL AND the assessment_id
back-fill guard (`if run.orchestrator_run_id and assessment_id`) is always
false, so every sub-agent row orphans from its parent assessment.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock

os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import orchestrator_runtime.runtime as rt


def test_run_one_forwards_run_id_to_inner(monkeypatch):
    captured = {}

    def fake_inner(sb, a_client, asset_id, trigger_type, model,
                   extractor_model, dry_run, parsed_out=None, run_id=None):
        captured["run_id"] = run_id
        return "assessment-x"

    monkeypatch.setattr(rt, "_run_one_inner", fake_inner)

    rt.run_one(
        MagicMock(), MagicMock(),
        asset_id="asset-1", trigger_type="new_doc",
        run_id="run-123", hard_kill_usd=None,
    )

    assert captured["run_id"] == "run-123"
