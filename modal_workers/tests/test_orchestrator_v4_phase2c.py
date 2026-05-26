"""Phase 2c tests: stage collapse for the v4 codepath.

Locks down that when ORCH_V4=1, `_run_one_inner` forces the three knobs that
short-circuit Stages 2 (hypothesis), 3 (premortem), 6 (ensemble), and 7
(semantic constitutional pass):

  - ensemble_n = 1                    → Stage 6 gate `if ensemble_n > 1` falls
                                        through; single-shot Stage 1+9 only.
  - enable_premortem = False          → Stage 2/3 block skipped.
  - constitutional_skip_semantic=True → Stage 7 runs ONLY the deterministic
                                        citation resolver; no Sonnet semantic
                                        pass. Citation integrity preserved.

Tested via source inspection so we don't have to mock Anthropic + Supabase +
RAG + the four sub-agent runners just to see which gates flip. Behavioral
end-to-end coverage lives in test_orchestrator_e2e_axs05.py and runs against
mocked Anthropic.

Plan: ~/.claude/plans/proud-booping-seal.md (Phase 2c).
Run: python3 -m pytest modal_workers/tests/test_orchestrator_v4_phase2c.py -v
"""
from __future__ import annotations

import inspect
import os
import re

os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")


# ---------------------------------------------------------------------------
# Forced-knob invariants inside `_run_one_inner`
# ---------------------------------------------------------------------------

def _v4_branch_source() -> str:
    """Return just the `if is_v4:` block that lives at the top of
    `_run_one_inner`. We pin to this block so the test doesn't spuriously
    pass on a string match deep in the function body."""
    from orchestrator_runtime import runtime

    full = inspect.getsource(runtime._run_one_inner)
    match = re.search(
        r"if is_v4:\n((?:        [^\n]*\n)+)",
        full,
    )
    assert match, (
        "Could not locate `if is_v4:` block in _run_one_inner. Either the v4 "
        "branch was removed or its indentation drifted."
    )
    return match.group(1)


def test_v4_forces_single_shot_ensemble():
    """Stage 6 collapse: ensemble_n must be forced to 1 when v4 is on.
    Otherwise an operator-set ensemble_n=3 would still spend 3× on Stage 1."""
    block = _v4_branch_source()
    assert re.search(r"\bensemble_n\s*=\s*1\b", block), (
        "v4 branch must force ensemble_n=1 to collapse Stage 6"
    )


def test_v4_forces_premortem_off():
    """Stage 2 + 3 collapse: enable_premortem must be False when v4 is on.
    The v4 Stage 1 prompt absorbs hypothesis enumeration + adversarial
    premortem inline — running the separate stages would double-pay."""
    block = _v4_branch_source()
    assert re.search(r"\benable_premortem\s*=\s*False\b", block), (
        "v4 branch must set enable_premortem=False to collapse Stage 2/3"
    )


def test_v4_keeps_deterministic_citation_check_drops_semantic():
    """Stage 7 split: constitutional_skip_semantic=True keeps the deterministic
    citation resolver (every [F:short] must resolve to a real fact_id) but
    skips the Sonnet semantic adversarial pass. Citation integrity is the
    load-bearing gate; semantic check was variance reduction."""
    block = _v4_branch_source()
    assert re.search(r"\bconstitutional_skip_semantic\s*=\s*True\b", block), (
        "v4 branch must set constitutional_skip_semantic=True to drop the "
        "semantic adversarial pass while preserving deterministic citation "
        "resolution"
    )


def test_v4_logs_collapse_for_observability():
    """When the v4 branch fires we want an obvious log line operators can
    grep — otherwise a wrong-flag misfire is silent and we burn money on the
    wrong path."""
    block = _v4_branch_source()
    assert "stages 2/3/6/semantic-7 collapsed" in block, (
        "v4 branch must log the collapse so operators can confirm in logs "
        "which path ran"
    )


# ---------------------------------------------------------------------------
# v3 rollback (ORCH_V4=0) leaves the operator knobs alone
# ---------------------------------------------------------------------------

def test_v3_rollback_does_not_force_knobs(monkeypatch):
    """Phase 6a flipped the default to v4. The forced-knob block lives inside
    `if is_v4:` so v3-rollback callers (ORCH_V4=0) retain their operator-passed
    values. Tested by recomputing the gate value the way the function does."""
    monkeypatch.setenv("ORCH_V4", "0")

    # Phase 6a default semantics: ORCH_V4=0 is the explicit rollback path.
    is_v4 = os.environ.get("ORCH_V4", "1") != "0"
    assert is_v4 is False

    # And the source must keep these forces strictly inside the `if is_v4:`
    # block — no top-level unconditional overwrites snuck in.
    from orchestrator_runtime import runtime
    source = inspect.getsource(runtime._run_one_inner)

    # Strip the v4 block, then verify the remaining function body does NOT
    # contain unconditional knob reassignment.
    stripped = re.sub(
        r"if is_v4:\n(?:        [^\n]*\n)+",
        "",
        source,
        count=1,
    )
    # These would be regressions: anywhere outside the v4 block reassigning
    # the three knobs would silently break v3-rollback.
    assert not re.search(
        r"^    ensemble_n\s*=\s*1\s*$", stripped, re.MULTILINE
    ), "ensemble_n=1 reassigned outside v4 block"
    assert not re.search(
        r"^    enable_premortem\s*=\s*False\s*$", stripped, re.MULTILINE
    ), "enable_premortem=False reassigned outside v4 block"
    assert not re.search(
        r"^    constitutional_skip_semantic\s*=\s*True\s*$",
        stripped,
        re.MULTILINE,
    ), "constitutional_skip_semantic=True reassigned outside v4 block"


# ---------------------------------------------------------------------------
# Downstream: build_claim_ledger must tolerate hypothesis_result=None
# (Phase 2c skips Stage 2 → no HypothesisResult on the v4 path)
# ---------------------------------------------------------------------------

def test_build_claim_ledger_handles_none_hypothesis_result():
    """v4 path skips Stage 2, so build_claim_ledger receives hypothesis_result=None.
    Verify the function returns a valid ledger structure rather than raising."""
    from orchestrator_runtime.runtime import build_claim_ledger

    # Minimal v4-shaped inputs.
    cited_prose_blocks = [
        {
            "section": "Catalyst landscape",
            "text": "PDUFA on 2026-08-01 per [F:abc12345].",
            "fact_citations": ["abc12345-aaaa-bbbb-cccc-dddddddddddd"],
            "doc_citations": [],
        },
    ]
    key_facts = [
        {"text": "PDUFA on 2026-08-01", "fact_id": "abc12345-aaaa-bbbb-cccc-dddddddddddd"},
    ]
    ledger = build_claim_ledger(
        cited_prose_blocks=cited_prose_blocks,
        key_facts=key_facts,
        hypothesis_result=None,
    )
    assert isinstance(ledger, list), (
        "build_claim_ledger must return a list even when hypothesis_result=None"
    )
