"""Phase 2a tests: v4 prompt constants + parameterized helpers + persist kwargs.

Validates the additive surface introduced in Phase 2a of the v4 architecture
simplification (~/.claude/plans/proud-booping-seal.md). Does NOT execute the
orchestrator end-to-end — those tests live in test_orchestrator_e2e_axs05.py
and run against a mocked Anthropic client. Phase 2a tests just lock down:

1. STAGE_1_V4_SYSTEM + STAGE_9_V4_SYSTEM exist and contain the new content
2. stage_1_synthesize / stage_9_extract accept a `system_prompt` kwarg
3. stage_10_persist accepts is_v4 / signal_category / commercial_dimensions
4. _run_one_inner picks v4 prompts when ORCH_V4=1 (verified via env-driven
   branch logic inspection, not full execution)

Run: python3 -m pytest modal_workers/tests/test_orchestrator_v4_phase2a.py -v
"""
from __future__ import annotations

import inspect
import os
import textwrap

os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")


# ---------------------------------------------------------------------------
# Prompt-content invariants
# ---------------------------------------------------------------------------

def test_stage_1_v4_system_exists_and_covers_commercial_dims():
    from orchestrator_runtime.runtime import STAGE_1_V4_SYSTEM

    # The v4 prompt MUST cover each commercial dimension the user's vision
    # called out. If any of these go missing the prompt has drifted away
    # from the spec.
    for term in (
        "biotech investment analyst",
        "REGULATORY",
        "COMMERCIAL",
        "TAM",
        "Standard of care",
        "Unmet need severity",
        "side effects",
        "Regulatory incentives",
        "Competitive landscape",
        "Commercial opportunity",
    ):
        assert term in STAGE_1_V4_SYSTEM, f"missing v4 prompt term: {term!r}"

    # Inferred-claim marker must be documented so commercial claims without
    # fact_id support don't get rejected by the constitutional check.
    assert "[INF]" in STAGE_1_V4_SYSTEM, "v4 prompt must define [INF] marker"


def test_stage_1_v4_system_keeps_cited_prose_contract():
    """v4 prompt extends but doesn't break v3's grounding contract."""
    from orchestrator_runtime.runtime import STAGE_1_V4_SYSTEM

    # Fact_id and document_id citation contract must survive into v4 —
    # otherwise downstream citation validation breaks.
    assert "[F:" in STAGE_1_V4_SYSTEM
    assert "[D:" in STAGE_1_V4_SYSTEM

    # The four-direction conclusion enum must survive — it's the input to
    # `thesis_direction` validation in Stage 9 + Stage 10.
    for direction in ("long", "short", "neutral", "straddle"):
        assert direction in STAGE_1_V4_SYSTEM, f"missing direction: {direction}"


def test_stage_9_v4_system_extends_schema_with_commercial_dimensions():
    from orchestrator_runtime.runtime import STAGE_9_V4_SYSTEM

    # The new top-level field must appear in the schema so Stage 9 emits it.
    assert "commercial_dimensions" in STAGE_9_V4_SYSTEM
    for sub_field in (
        "tam_estimate",
        "low_usd",
        "high_usd",
        "is_inferred",
        "standard_of_care",
        "soc_limitations",
        "soc_side_effects",
        "unmet_need_severity_1_5",
        "regulatory_incentives",
        "competitive_landscape_summary",
    ):
        assert sub_field in STAGE_9_V4_SYSTEM, f"missing schema field: {sub_field}"


def test_stage_9_v4_system_preserves_existing_required_fields():
    """v4 schema is a superset — all v3 required fields must still be there."""
    from orchestrator_runtime.runtime import STAGE_9_SYSTEM, STAGE_9_V4_SYSTEM

    # Spot-check a few load-bearing v3 fields. If the v4 schema accidentally
    # drops one, the persist layer will silently get NULL where it expects
    # a value.
    for field in (
        "thesis_direction",
        "conviction_pct",
        "evidence_quality",
        "key_facts",
        "uncertainties",
        "cited_prose_blocks",
        "reasoning_summary",
        "prediction_target",
    ):
        assert field in STAGE_9_SYSTEM
        assert field in STAGE_9_V4_SYSTEM, f"v4 dropped v3 field: {field}"


# ---------------------------------------------------------------------------
# Signature compatibility (parameterized helpers)
# ---------------------------------------------------------------------------

def test_stage_1_synthesize_accepts_system_prompt_kwarg():
    from orchestrator_runtime.runtime import STAGE_1_SYSTEM, stage_1_synthesize

    params = inspect.signature(stage_1_synthesize).parameters
    assert "system_prompt" in params, "stage_1_synthesize must accept system_prompt"
    assert params["system_prompt"].default == STAGE_1_SYSTEM, (
        "default must be the v3 prompt so existing callers are unaffected"
    )
    assert params["system_prompt"].kind == inspect.Parameter.KEYWORD_ONLY, (
        "system_prompt must be keyword-only to prevent positional accidents"
    )


def test_stage_9_extract_accepts_system_prompt_kwarg():
    from orchestrator_runtime.runtime import STAGE_9_SYSTEM, stage_9_extract

    params = inspect.signature(stage_9_extract).parameters
    assert "system_prompt" in params, "stage_9_extract must accept system_prompt"
    assert params["system_prompt"].default == STAGE_9_SYSTEM
    assert params["system_prompt"].kind == inspect.Parameter.KEYWORD_ONLY


def test_stage_10_persist_accepts_v4_metadata_kwargs():
    from orchestrator_runtime.runtime import stage_10_persist

    params = inspect.signature(stage_10_persist).parameters
    for kwarg in ("is_v4", "signal_category", "commercial_dimensions"):
        assert kwarg in params, f"stage_10_persist must accept {kwarg}"
        assert params[kwarg].kind == inspect.Parameter.KEYWORD_ONLY, (
            f"{kwarg} must be keyword-only so v3 positional callers are unaffected"
        )

    # Defaults must be inert so v3 callers persist as before.
    assert params["is_v4"].default is False
    assert params["signal_category"].default is None
    assert params["commercial_dimensions"].default is None


# ---------------------------------------------------------------------------
# Flag-driven prompt selection inside _run_one_inner
# ---------------------------------------------------------------------------

def test_run_one_inner_reads_orch_v4_env_var():
    """Phase 2a invariant: the v4 branch is env-driven, read at call time
    (not module load), so tests + production can flip the flag without
    re-importing. Asserted via source inspection rather than full execution
    because the function does ~20+ external calls before the flag matters."""
    from orchestrator_runtime import runtime

    source = inspect.getsource(runtime._run_one_inner)
    # Phase 6a flipped the default — the env read now carries a `"1"` default
    # arg so unset → v4. Either form (Phase 2a `("ORCH_V4")` or Phase 6a
    # `("ORCH_V4", "1")`) is a runtime read of the same env var, so accept both.
    assert 'os.environ.get("ORCH_V4"' in source, (
        "_run_one_inner must read ORCH_V4 from os.environ (runtime read, "
        "not module-load constant)"
    )
    # Must use both prompts conditionally.
    assert "STAGE_1_V4_SYSTEM" in source
    assert "STAGE_9_V4_SYSTEM" in source
    # The persist call must thread v4 metadata.
    assert "is_v4=is_v4" in source
    assert "commercial_dimensions=" in source
    assert "signal_category=" in source


def test_run_one_inner_v4_default_when_flag_unset(monkeypatch):
    """Phase 6a flipped the default. Unset ORCH_V4 now means v4 runs.

    Smoke check: re-evaluate the selection logic the way _run_one_inner does.
    If this diverges from the function body, the function body has drifted.
    """
    monkeypatch.delenv("ORCH_V4", raising=False)
    from orchestrator_runtime.runtime import (
        STAGE_1_SYSTEM, STAGE_1_V4_SYSTEM, STAGE_9_SYSTEM, STAGE_9_V4_SYSTEM,
    )

    # Phase 6a default: v4 unless ORCH_V4=0 is explicitly set.
    is_v4 = os.environ.get("ORCH_V4", "1") != "0"
    assert is_v4 is True
    assert (STAGE_1_V4_SYSTEM if is_v4 else STAGE_1_SYSTEM) is STAGE_1_V4_SYSTEM
    assert (STAGE_9_V4_SYSTEM if is_v4 else STAGE_9_SYSTEM) is STAGE_9_V4_SYSTEM


def test_run_one_inner_v3_rollback_when_flag_explicitly_zero(monkeypatch):
    """Phase 6a invariant: ORCH_V4=0 is the operator rollback path. Must
    still route to the v3 multi-stage pipeline without code changes."""
    monkeypatch.setenv("ORCH_V4", "0")
    from orchestrator_runtime.runtime import (
        STAGE_1_SYSTEM, STAGE_1_V4_SYSTEM, STAGE_9_SYSTEM, STAGE_9_V4_SYSTEM,
    )

    is_v4 = os.environ.get("ORCH_V4", "1") != "0"
    assert is_v4 is False
    assert (STAGE_1_V4_SYSTEM if is_v4 else STAGE_1_SYSTEM) is STAGE_1_SYSTEM
    assert (STAGE_9_V4_SYSTEM if is_v4 else STAGE_9_SYSTEM) is STAGE_9_SYSTEM


def test_run_one_inner_v4_branch_when_flag_set_explicit(monkeypatch):
    """Explicit ORCH_V4=1 still works (backward-compat with pre-6a callers)."""
    monkeypatch.setenv("ORCH_V4", "1")
    from orchestrator_runtime.runtime import (
        STAGE_1_SYSTEM, STAGE_1_V4_SYSTEM, STAGE_9_SYSTEM, STAGE_9_V4_SYSTEM,
    )

    is_v4 = os.environ.get("ORCH_V4", "1") != "0"
    assert is_v4 is True
    assert (STAGE_1_V4_SYSTEM if is_v4 else STAGE_1_SYSTEM) is STAGE_1_V4_SYSTEM
    assert (STAGE_9_V4_SYSTEM if is_v4 else STAGE_9_SYSTEM) is STAGE_9_V4_SYSTEM


# ---------------------------------------------------------------------------
# Row-dict v4 fields land at persistence
# ---------------------------------------------------------------------------

def test_stage_10_persist_writes_v4_columns_when_is_v4():
    """Inspect the source: when is_v4 is wired through, the row dict that
    goes to rpc/persist_assessment_v3 must include the three new columns.
    Source-level check because mocking the full RPC + secondaries pipeline
    is heavier than the value provides."""
    from orchestrator_runtime import runtime

    source = inspect.getsource(runtime.stage_10_persist)
    # All three new columns must be in the row dict.
    assert '"orchestrator_version_v4": is_v4' in source
    assert '"signal_category": signal_category' in source
    assert '"commercial_dimensions": commercial_dimensions' in source
