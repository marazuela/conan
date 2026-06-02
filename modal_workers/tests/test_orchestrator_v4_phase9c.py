"""v4 Phase 9c — prompt_proposals schema + prompt_retrospective skill tests.

Mirrors test_orchestrator_v4_phase7.py shape: parse the migration SQL to
verify table/column/index presence (no live DB); parse the Cowork skill
markdown to verify documented invariants are present so the skill stays
human-reviewable.

Run: python -m pytest modal_workers/tests/test_orchestrator_v4_phase9c.py -v
"""

from __future__ import annotations

from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Migration: prompt_proposals
# ---------------------------------------------------------------------------

def _migration_path() -> Path:
    return REPO_ROOT / "supabase" / "migrations" / "20260528092213_v4_prompt_proposals.sql"


def test_migration_file_exists():
    assert _migration_path().exists(), (
        f"prompt_proposals migration missing at {_migration_path()}"
    )


def test_migration_creates_prompt_proposals_table():
    sql = _migration_path().read_text()
    assert "create table if not exists public.prompt_proposals" in sql.lower()


@pytest.mark.parametrize("col", [
    "id",
    "stage",
    "current_prompt_version_id",
    "current_prompt_text",
    "proposed_prompt_text",
    "rationale",
    "prompt_diff",
    "cohort_window_start",
    "cohort_window_end",
    "cohort_size",
    "brier_delta",
    "paired_bootstrap_p",
    "auc_delta",
    "n_eval_cases",
    "agent_version",
    "status",
    "approved_by",
    "approved_at",
    "applied_prompt_version_id",
    "applied_at",
    "rejected_reason",
    "rejected_by",
    "rejected_at",
    "metadata",
    "created_at",
])
def test_migration_has_required_columns(col):
    sql = _migration_path().read_text().lower()
    assert col in sql, f"prompt_proposals missing column: {col}"


def test_migration_constrains_stage_enum():
    sql = _migration_path().read_text().lower()
    # CHECK constraint must enforce stage_1_system / stage_9_system.
    assert "stage_1_system" in sql and "stage_9_system" in sql
    assert "check (stage in" in sql or "check(stage in" in sql


def test_migration_constrains_status_enum():
    sql = _migration_path().read_text().lower()
    for status in (
        "pending_eval_gate",
        "failed_eval_gate",
        "pending_operator_review",
        "accepted",
        "applied",
        "rejected",
    ):
        assert status in sql, f"status enum missing {status}"


def test_migration_default_status_is_pending_eval_gate():
    """A/B harness runs FIRST — proposals start at pending_eval_gate so
    the dashboard never sees a candidate that hasn't been D-103 scored."""
    sql = _migration_path().read_text().lower()
    assert "default 'pending_eval_gate'" in sql


def test_migration_has_pending_review_index():
    """The dashboard's primary query filters status='pending_operator_review';
    a partial index keeps it cheap as failed_eval_gate / rejected rows
    accumulate."""
    sql = _migration_path().read_text().lower()
    assert "idx_prompt_proposals_pending" in sql
    assert "pending_operator_review" in sql


def test_migration_has_failed_gate_index():
    sql = _migration_path().read_text().lower()
    assert "idx_prompt_proposals_failed_gate" in sql


def test_migration_has_approval_consistency_check():
    sql = _migration_path().read_text().lower()
    assert "prompt_proposals_approval_consistent" in sql


def test_migration_has_rejection_consistency_check():
    sql = _migration_path().read_text().lower()
    assert "prompt_proposals_rejection_consistent" in sql


def test_migration_fks_to_prompt_versions():
    """current_prompt_version_id + applied_prompt_version_id must FK to
    prompt_versions so cascading prompt_versions cleanup doesn't orphan
    audit rows."""
    sql = _migration_path().read_text().lower()
    # Two FKs to prompt_versions, ON DELETE SET NULL
    assert sql.count("references public.prompt_versions(id)") >= 2
    assert sql.count("on delete set null") >= 2


# ---------------------------------------------------------------------------
# Cowork skill: prompt_retrospective
# ---------------------------------------------------------------------------

def _skill_path() -> Path:
    return REPO_ROOT / ".claude" / "skills" / "prompt_retrospective.md"


def test_prompt_retrospective_skill_exists():
    assert _skill_path().exists(), (
        f"prompt_retrospective skill missing at {_skill_path()}. "
        f"Must be committed in the sibling conan-cowork-skills repo."
    )


def test_skill_writes_only_pending_eval_gate_never_applies():
    """Skill must NOT mutate prompt_versions or runtime.py directly — it
    only writes to prompt_proposals at the pending_eval_gate status. The
    A/B harness owns the next status transition; the operator owns the
    one after that; manual PR owns the final apply step."""
    body = _skill_path().read_text().lower()
    assert "prompt_proposals" in body
    assert "pending_eval_gate" in body
    # Must explicitly disclaim auto-deploy.
    assert ("do not edit `runtime.py`" in body
            or "does not auto-deploy" in body
            or "does not auto-apply" in body
            or "manual code change" in body)


def test_skill_enforces_quarter_boundary_gate():
    """The skill is quarterly cadence, not weekly. A no-op if <90 days
    since the last proposal — the cron runs weekly but the skill must
    short-circuit unless a quarter has elapsed."""
    body = _skill_path().read_text().lower()
    assert "quarter" in body
    assert ("quarter_boundary" in body or "90 days" in body)


def test_skill_enforces_d103_cohort_floor():
    """Phase 9e's D-103 gate needs n>=200. Proposing a candidate that
    can't even be scored is wasted work — skill must skip thin cohorts."""
    body = _skill_path().read_text().lower()
    assert ("cohort_too_thin_for_d103_gate" in body or "n>=200" in body
            or "n_eval_cases" in body or "200" in body)


def test_skill_enforces_no_price_gate_covenant():
    """v4 covenant: market_cap / stock_price / price_pct as hard gates
    is banned across rubrics AND prompts."""
    body = _skill_path().read_text().lower()
    assert ("no-price-gate" in body or "stock_price" in body
            or "market_cap" in body or "price_pct" in body)


def test_skill_enforces_one_stage_per_proposal():
    """Stage 1 and Stage 9 have different blast radii — bundling them
    obscures the operator's evaluation. One stage per proposal."""
    body = _skill_path().read_text().lower()
    assert ("one stage per proposal" in body
            or "don't propose simultaneous" in body)


def test_skill_emits_structured_prompt_diff():
    """Dashboard render is deterministic only if the diff is structured.
    Skill must emit {added, removed, changed} shape."""
    body = _skill_path().read_text().lower()
    assert "added" in body and "removed" in body and "changed" in body
    assert "prompt_diff" in body


def test_skill_defers_to_calibration_and_rubric_layers():
    """Prompt changes are third-tier. Calibration drift → D-104. Category
    weight issues → Phase 7 retro. Skill must explicitly disclaim these
    failure-mode buckets so it doesn't relitigate them."""
    body = _skill_path().read_text().lower()
    assert "calibration" in body and "rubric" in body
    # Must reference the layered-defenses concept explicitly.
    assert ("defer to" in body or "calibration layer" in body
            or "rubric weight" in body)
