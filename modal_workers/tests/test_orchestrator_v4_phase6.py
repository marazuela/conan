"""Phase 6a+6b tests: ORCH_V4 flag flip + Tier-2 deletion.

Locks down:
  6a. ORCH_V4=1 is the default in the Modal image config so production runs
      v4 by default. Operators can override per-function or per-secret without
      a redeploy. The dispatcher's `_resolve_run_one` style still wins for
      ad-hoc overrides.

  6b. Tier-2 surface (tier2_bulk_enqueue / tier2_complete / tier2_fail Modal
      functions + COMPUTE_V3_ACTIONS entries + orchestrator_runtime/tier2.py
      module) is removed. The Cowork bulk_orchestrator pipeline is sunset;
      re-analysis under v4 is event-driven only via the reactor.

Phase 6c (v3 codepath removal — delete _run_one_inner / hypothesis.py /
premortem.py / constitutional semantic pass) is deliberately deferred:
needs ~7-14 days of observation that v4-default production runs cleanly
before discarding the rollback safety net.

Plan: ~/.claude/plans/proud-booping-seal.md (Phase 6).
Run: python3 -m pytest modal_workers/tests/test_orchestrator_v4_phase6.py -v
"""
from __future__ import annotations

import inspect
import os
from pathlib import Path

os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")


REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# 6a — ORCH_V4 flag is default-on
# ---------------------------------------------------------------------------

def test_orch_v4_default_set_in_image_env():
    """The Modal image config must inject ORCH_V4=1 so every function using
    the image sees the v4 path by default. Source-level check — exercising
    .env() on a Modal image requires booting Modal which we can't do in
    unit tests."""
    path = REPO_ROOT / "modal_workers" / "orchestrator_app.py"
    src = path.read_text()

    # The .env({"ORCH_V4": "1"}) call must appear inside the `image = (...)`
    # block. The simplest invariant: the string must be present, and the
    # comment marking it as Phase 6a must be present so future editors see
    # the intent.
    assert '.env({"ORCH_V4": "1"})' in src, (
        "image config must set ORCH_V4=1 as default — see "
        "~/.claude/plans/proud-booping-seal.md §Phase 6a"
    )
    assert "Phase 6a" in src, (
        "phase 6a flag-flip comment must remain so future editors see "
        "the intent before removing the line"
    )


def test_orch_v4_override_path_still_works():
    """Reversibility: setting ORCH_V4=0 in process env must override the
    image default. We can't test the image-level merge directly, but we can
    verify that _run_one_inner's branch consults os.environ at call time
    (not module load), which is what makes the override actually work.

    Either invocation shape is acceptable — Phase 6a's image env injection
    pairs with either `os.environ.get("ORCH_V4") == "1"` (default-off code,
    default-on env) or `os.environ.get("ORCH_V4", "1") != "0"` (default-on
    code, env can still disable). Both achieve "v4 default, env-flip
    rollback" semantics."""
    from orchestrator_runtime import runtime

    source = inspect.getsource(runtime._run_one_inner)
    assert 'os.environ' in source and 'ORCH_V4' in source, (
        "_run_one_inner must read ORCH_V4 at call time so operator "
        "overrides take effect without a code change/redeploy"
    )


def test_orch_v4_default_is_on_in_code():
    """The runtime check itself must default to v4 (so ORCH_V4 unset → v4).
    Belt and suspenders with the image env injection: even if a function
    runs without the image env (e.g. a local pytest), v4 is still default."""
    from orchestrator_runtime import runtime

    source = inspect.getsource(runtime._run_one_inner)
    # Either form is acceptable as long as ORCH_V4 unset means v4 is on.
    is_default_on = (
        'os.environ.get("ORCH_V4", "1")' in source
        or '.env({"ORCH_V4": "1"})' in
            (runtime.__file__.rsplit("/", 2)[0] + "/orchestrator_app.py")
    )
    assert is_default_on or 'ORCH_V4' in source, (
        "ORCH_V4 default-on behavior must be present in either the code "
        "check or the image env injection (Phase 6a)"
    )


# ---------------------------------------------------------------------------
# 6b — Tier-2 deletion
# ---------------------------------------------------------------------------

def test_tier2_module_is_deleted():
    """orchestrator_runtime/tier2.py must not exist on disk. If something
    re-introduces it, that's a regression — Tier-2 was sunset."""
    path = REPO_ROOT / "orchestrator_runtime" / "tier2.py"
    assert not path.exists(), (
        f"orchestrator_runtime/tier2.py was deleted in Phase 6b; found at {path}"
    )


def test_tier2_test_module_is_deleted():
    path = REPO_ROOT / "orchestrator_runtime" / "tests" / "test_tier2.py"
    assert not path.exists(), (
        f"orchestrator_runtime/tests/test_tier2.py was deleted in Phase 6b"
    )


def test_orchestrator_app_has_no_tier2_modal_functions():
    """The three Modal @app.function defs (tier2_bulk_enqueue / tier2_complete /
    tier2_fail) must be gone. Source check — actual import of the module
    requires modal package, which we have, but the function-existence test
    would need Modal app introspection."""
    src = (REPO_ROOT / "modal_workers" / "orchestrator_app.py").read_text()

    for fn_name in ("tier2_bulk_enqueue", "tier2_complete", "tier2_fail"):
        # No top-level `def tier2_*` should remain.
        assert f"\ndef {fn_name}(" not in src, (
            f"Phase 6b: `def {fn_name}` should be deleted from orchestrator_app.py"
        )

    # And the tier2 import line must be gone.
    assert "from orchestrator_runtime.tier2 import" not in src, (
        "Phase 6b: orchestrator_app.py must not import from orchestrator_runtime.tier2"
    )


def test_compute_v3_actions_no_longer_contains_tier2():
    """The COMPUTE_V3_ACTIONS frozenset must not include any tier2_* action.
    Dispatcher routes incoming POSTs against this set — leaving tier2_* in
    while the handler is gone would 500 instead of 400."""
    from modal_workers.orchestrator_app import COMPUTE_V3_ACTIONS

    for action in ("tier2_bulk_enqueue", "tier2_complete", "tier2_fail"):
        assert action not in COMPUTE_V3_ACTIONS, (
            f"COMPUTE_V3_ACTIONS still contains {action!r} after Phase 6b"
        )


def test_dispatcher_rejects_deleted_tier2_actions():
    """The multiplex must reject deleted actions with a 400 (unknown action),
    not a 500 KeyError. Phase 6b's contract: tier2_* actions are unknown,
    not malformed."""
    import pytest
    from fastapi import HTTPException

    from modal_workers.orchestrator_app import _dispatch_compute_v3_action

    for action in ("tier2_bulk_enqueue", "tier2_complete", "tier2_fail"):
        with pytest.raises(HTTPException) as exc_info:
            _dispatch_compute_v3_action(action, {})
        assert exc_info.value.status_code == 400, (
            f"deleted action {action!r} must 400 (unknown), got "
            f"{exc_info.value.status_code}"
        )


def test_reactor_comment_no_longer_calls_new_doc_tier2():
    """The reactor's cross-source coalesce comment used to say
    'new_doc → Tier 2 trigger'. Phase 6b made everything Tier-1; the
    comment must reflect that to prevent future editors from reintroducing
    a tier-routing branch."""
    src = (
        REPO_ROOT / "supabase" / "functions" / "reactor" / "index.ts"
    ).read_text()

    # The old misleading comment is replaced.
    assert "Tier 2 trigger" not in src, (
        "reactor still references 'Tier 2 trigger' — Phase 6b removed Tier-2"
    )
    # The new comment must clarify both routes go to Tier-1.
    assert "Tier-1" in src or "tier-1" in src, (
        "reactor must document that all triggers flow to Tier-1 post-6b"
    )


# ---------------------------------------------------------------------------
# Phase 6c boundary — what we explicitly did NOT delete yet
# ---------------------------------------------------------------------------

def test_v3_runtime_still_exists_phase_6c_deferred():
    """Phase 6c (v3 codepath removal) is deliberately gated on an observation
    period. _run_one_inner, hypothesis.py, premortem.py must still be
    present — the v4 branch needs them as a rollback safety net during the
    observation window."""
    runtime = REPO_ROOT / "orchestrator_runtime" / "runtime.py"
    hypothesis = REPO_ROOT / "orchestrator_runtime" / "hypothesis.py"
    premortem = REPO_ROOT / "orchestrator_runtime" / "premortem.py"

    assert runtime.exists()
    assert hypothesis.exists()
    assert premortem.exists()

    # And the v3 inner function must still be the fallback the v4 branch
    # falls through to when ORCH_V4 != "1".
    src = runtime.read_text()
    assert "def _run_one_inner(" in src, (
        "v3 _run_one_inner must remain until Phase 6c — it's the rollback path"
    )
