"""Phase 6a tests: flag flip — v4 is now the production default.

The Phase 2 work introduced ORCH_V4=1 as the opt-in switch for the v4 pipeline.
Phase 6a inverts the polarity: v4 runs by default; ORCH_V4=0 is the explicit
rollback path for emergency revert during the post-flip observation period.

Phase 6a is purely an env-default change inside `_run_one_inner`. No new code
path, no migration. The test locks down:
  1. Source: env read uses the new default ("1") + "!=" "0" check
  2. delenv → v4 active (the production default)
  3. setenv(ORCH_V4=0) → v3 fallback active (rollback path)
  4. setenv(ORCH_V4=1) → v4 active (explicit, backward-compat)
  5. The startup log differentiates default-v4 vs ORCH_V4=0 explicit-rollback

Plan: ~/.claude/plans/proud-booping-seal.md (Phase 6a).
Run: python3 -m pytest modal_workers/tests/test_orchestrator_v4_phase6a.py -v
"""
from __future__ import annotations

import inspect
import os

os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")


# ---------------------------------------------------------------------------
# Source-level invariants
# ---------------------------------------------------------------------------

def test_runtime_default_semantics_use_inverted_check():
    """The env read MUST use the 'default-on' form. The Phase 2 form
    `os.environ.get("ORCH_V4") == "1"` would mean ORCH_V4=0 (or unset) routes
    to v3 — that's the pre-Phase-6a polarity and a silent regression risk
    if a future edit reverts the default."""
    from orchestrator_runtime import runtime

    source = inspect.getsource(runtime._run_one_inner)
    # The new check: present, with default "1", and an "!=" "0" semantics.
    assert 'os.environ.get("ORCH_V4", "1")' in source, (
        "Phase 6a: _run_one_inner must read ORCH_V4 with default '1' so v4 "
        "is the production default"
    )
    assert '!= "0"' in source, (
        "Phase 6a: comparison must be `!= \"0\"` so ORCH_V4=0 is the only "
        "way to opt out (and unset → v4)"
    )

    # The pre-Phase-6a `== "1"` polarity must be gone — present today only
    # in test files, not in runtime.py.
    assert 'os.environ.get("ORCH_V4") == "1"' not in source, (
        "Phase 6a: pre-flip check `== \"1\"` must not survive in runtime.py"
    )


def test_rollback_log_warns_when_v3_active():
    """When ORCH_V4=0 is explicitly set (rollback), the runtime should log a
    warning so the operator sees the rollback path actually fired and isn't
    silently sitting on v3 forever."""
    from orchestrator_runtime import runtime

    source = inspect.getsource(runtime._run_one_inner)
    # The else branch must exist + must log something operator-visible.
    assert "else:" in source
    assert "rollback" in source.lower() or "ORCH_V4=0" in source, (
        "Phase 6a: when v4 is disabled via ORCH_V4=0, the runtime must log "
        "that the rollback path is active"
    )


# ---------------------------------------------------------------------------
# Behavioral checks: env state → resolved is_v4 value
# ---------------------------------------------------------------------------

def _resolve_is_v4_like_runtime() -> bool:
    """Mirror of `_run_one_inner`'s flag-resolution one-liner. If this drifts
    from the actual function body, the upstream test (above) catches it via
    source inspection."""
    return os.environ.get("ORCH_V4", "1") != "0"


def test_unset_env_defaults_to_v4(monkeypatch):
    """Fresh Modal container, no Modal secret, no per-call override → v4.
    This is the production default after Phase 6a."""
    monkeypatch.delenv("ORCH_V4", raising=False)
    assert _resolve_is_v4_like_runtime() is True


def test_explicit_zero_routes_to_v3(monkeypatch):
    """Operator hits the rollback path: ORCH_V4=0. Must route to v3."""
    monkeypatch.setenv("ORCH_V4", "0")
    assert _resolve_is_v4_like_runtime() is False


def test_explicit_one_routes_to_v4(monkeypatch):
    """Backward-compat with pre-Phase-6a callers that explicitly set
    ORCH_V4=1. Must keep working."""
    monkeypatch.setenv("ORCH_V4", "1")
    assert _resolve_is_v4_like_runtime() is True


def test_unrecognized_values_default_to_v4(monkeypatch):
    """Defensive: anything other than the literal string '0' routes to v4.
    Prevents subtle bugs like ORCH_V4='false' silently disabling v4 in
    Cowork/CI environments that pass string booleans."""
    for value in ("true", "yes", "TRUE", "on", "", "1", "garbage"):
        monkeypatch.setenv("ORCH_V4", value)
        assert _resolve_is_v4_like_runtime() is True, (
            f"value={value!r}: only literal '0' should disable v4"
        )


# ---------------------------------------------------------------------------
# The forced knobs still apply on the default (v4) path
# ---------------------------------------------------------------------------

def test_v4_default_path_still_collapses_stages():
    """Phase 6a doesn't change what v4 does — it just makes v4 the default.
    The Phase 2c stage-collapse forces (ensemble_n=1, enable_premortem=False,
    constitutional_skip_semantic=True) still apply when ORCH_V4 is unset."""
    from orchestrator_runtime import runtime

    source = inspect.getsource(runtime._run_one_inner)
    # The forced-knob block must still live inside `if is_v4:` and contain
    # all three Phase 2c assignments.
    assert "if is_v4:" in source
    # The three knobs locked by Phase 2c.
    import re
    block_match = re.search(
        r"if is_v4:\n((?:        [^\n]*\n)+)",
        source,
    )
    assert block_match is not None
    block = block_match.group(1)
    assert "ensemble_n = 1" in block
    assert "enable_premortem = False" in block
    assert "constitutional_skip_semantic = True" in block
