"""Phase 6 cleanup tests: v4 is the only live orchestrator runtime."""
from __future__ import annotations

import inspect
import os
from pathlib import Path

os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_retired_v3_stage_modules_are_deleted():
    for rel in (
        "orchestrator_runtime/hypothesis.py",
        "orchestrator_runtime/premortem.py",
        "orchestrator_runtime/constitutional.py",
        "orchestrator_runtime/ensemble.py",
    ):
        assert not (REPO_ROOT / rel).exists(), f"retired module still exists: {rel}"


def test_runtime_has_no_orch_v4_rollback_branch():
    from orchestrator_runtime import runtime

    source = inspect.getsource(runtime._run_one_inner)
    assert "ORCH_V4" not in source
    assert "STAGE_1_V4_SYSTEM" not in source
    assert "STAGE_9_V4_SYSTEM" not in source
    assert "run_hypothesis_enumeration" not in source
    assert "run_premortem" not in source
    assert "run_constitutional_check" not in source
    assert "run_streaming_ensemble" not in source


def test_runtime_keeps_deterministic_citation_validation():
    from orchestrator_runtime import runtime

    result = runtime._validate_citations(
        cited_prose="Positive result [F:abcdef12] from primary doc [D:12345678].",
        facts=[{"id": "abcdef12-0000-0000-0000-000000000000"}],
        document_ids=["12345678-0000-0000-0000-000000000000"],
    )
    assert result.pass_ is True
    assert result.n_citations_checked == 2
    assert result.n_citations_resolved == 2

    missing = runtime._validate_citations(
        cited_prose="Unsupported cite [F:deadbeef].",
        facts=[],
        document_ids=[],
    )
    assert missing.pass_ is False
    assert missing.findings[0].check == "unresolved_fact_id"


def test_orchestrator_app_public_args_are_v4_only():
    src = (REPO_ROOT / "modal_workers" / "orchestrator_app.py").read_text()
    header = src.split("def orchestrator_run_one(", 1)[1].split(") -> Dict[str, Any]:", 1)[0]
    for removed in (
        "ensemble_n",
        "ensemble_mode",
        "constitutional",
        "constitutional_deterministic_only",
        "enable_premortem",
    ):
        assert removed not in header


def test_tier2_surface_remains_deleted():
    from modal_workers.orchestrator_app import COMPUTE_V3_ACTIONS

    assert not (REPO_ROOT / "orchestrator_runtime" / "tier2.py").exists()
    for action in ("tier2_bulk_enqueue", "tier2_complete", "tier2_fail"):
        assert action not in COMPUTE_V3_ACTIONS
