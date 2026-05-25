"""Phase 3 tests: thesis_transcriber Cowork skill + thesis_emitted_at migration.

Phase 3 demotes thesis_writer to pure transcription on the v4 path. The
implementation lands as:
  - A new Cowork skill (.claude/skills/thesis_transcriber.md) that drains
    v4 convergence_assessments without re-reasoning.
  - A migration adding convergence_assessments.thesis_emitted_at +
    a partial index for the transcription queue scan.

These tests lock down the file artifacts so the skill's invariants
(no reasoning, no web research, no challenger pass) can't silently drift
once Pedro starts editing the markdown.

Plan: ~/.claude/plans/proud-booping-seal.md (Phase 3).
Run: python3 -m pytest modal_workers/tests/test_orchestrator_v4_phase3.py -v
"""
from __future__ import annotations

import os
import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

def test_thesis_emitted_at_migration_exists():
    path = REPO_ROOT / "supabase" / "migrations" / "20260613006000_v4_thesis_emitted_at.sql"
    assert path.exists(), f"Phase 3 migration missing at {path}"


def test_migration_adds_column_to_convergence_assessments():
    path = REPO_ROOT / "supabase" / "migrations" / "20260613006000_v4_thesis_emitted_at.sql"
    sql = path.read_text()

    # ADD COLUMN, IF NOT EXISTS for idempotency, timestamptz type.
    assert re.search(
        r"ALTER TABLE public\.convergence_assessments\s+ADD COLUMN IF NOT EXISTS thesis_emitted_at timestamptz",
        sql,
    ), "migration must add thesis_emitted_at timestamptz with IF NOT EXISTS"


def test_migration_creates_pending_transcription_index():
    """The transcriber polls this index every tick; without it, the scan is
    O(all v4 rows) and gets slow as v4 ramps."""
    path = REPO_ROOT / "supabase" / "migrations" / "20260613006000_v4_thesis_emitted_at.sql"
    sql = path.read_text()

    assert "idx_convergence_assessments_v4_pending_transcription" in sql
    # Partial-index WHERE clause must scope to v4 + immediate + pass-gated + pending.
    assert "orchestrator_version_v4 = true" in sql
    assert "band = 'immediate'" in sql
    assert "alert_gate_status = 'pass'" in sql
    assert "thesis_emitted_at IS NULL" in sql


# ---------------------------------------------------------------------------
# thesis_transcriber skill (Cowork-side, lives in conan-cowork-skills repo
# via the .claude/skills symlink)
# ---------------------------------------------------------------------------

def _skill_path() -> Path:
    """Resolve the thesis_transcriber skill path via the symlink to the
    sibling conan-cowork-skills repo. The skill IS the contract."""
    return REPO_ROOT / ".claude" / "skills" / "thesis_transcriber.md"


def test_thesis_transcriber_skill_exists():
    path = _skill_path()
    assert path.exists(), (
        f"thesis_transcriber skill missing at {path}. "
        f"Must be committed in the sibling conan-cowork-skills repo."
    )


def test_skill_declares_pure_transcription_invariant():
    """The whole point of Phase 3: no new reasoning downstream of the
    orchestrator. If the skill body drifts toward 'polish' or 'extend',
    we've collapsed back to the old multi-reasoning-layer architecture."""
    body = _skill_path().read_text()

    # Must explicitly disclaim reasoning + web research + challenger.
    must_have_disclaimer = [
        "do not reason",
        "do not add information",
        "do not run web research",
        "do not invoke a challenger pass",
    ]
    body_lower = body.lower()
    for term in must_have_disclaimer:
        assert term in body_lower, (
            f"Phase 3 invariant missing from skill body: '{term}'"
        )


def test_skill_queues_only_v4_pass_gated_immediate_rows():
    """The transcriber's input query is the load-bearing filter. Wrong
    filter = v3 rows leak into v4 path or low-band rows get promoted."""
    body = _skill_path().read_text()

    for filter_clause in (
        "orchestrator_version_v4 = true",
        "band = 'immediate'",
        "alert_gate_status = 'pass'",
        "thesis_emitted_at IS NULL",
    ):
        assert filter_clause in body, (
            f"transcriber queue scan missing filter: {filter_clause!r}"
        )


def test_skill_shares_daily_quota_with_v3():
    """The 15/day cap is an operator-facing alert-volume guardrail. If v4
    skips it, the dashboard floods. Sharing the count keeps total promotion
    volume bounded across the v3→v4 transition."""
    body = _skill_path().read_text()

    # Must reference thesis_jobs (v3 promotions) AND thesis_emitted_at (v4)
    # in a combined count.
    assert "thesis_jobs" in body, (
        "shared quota query must include v3 thesis_jobs count"
    )
    assert "thesis_emitted_at" in body, (
        "shared quota query must include v4 transcription count"
    )
    # 15/day cap must still be the stated ceiling.
    assert "15" in body, "operator-facing 15/day cap must be documented"


def test_skill_stamps_thesis_emitted_at_idempotently():
    """Phase 3 invariant: re-runs must not double-transcribe. The UPDATE's
    `WHERE thesis_emitted_at IS NULL` guard is what makes this idempotent."""
    body = _skill_path().read_text()

    assert "thesis_emitted_at = now()" in body
    # The IS NULL guard prevents the WHERE-zero-rows path from looking
    # like a successful concurrent transcription.
    assert "thesis_emitted_at IS NULL" in body


def test_skill_declares_no_short_positioning_path():
    """v4 only runs binary_catalyst (FDA). Short signals stay on v3.
    A skill that accidentally promotes short_positioning rows breaks the
    sub-quota logic in thesis_writer.md."""
    body = _skill_path().read_text()

    assert "short_positioning" in body, (
        "skill must explicitly state it does not handle short_positioning"
    )
    # The short_positioning mention must sit under the explicit
    # "does NOT do" section header rather than in a how-to-promote
    # context. The section header is the canonical marker.
    explicit_exclusion_header = "## What this skill explicitly does NOT do"
    explicit_exclusion_section = body[body.find(explicit_exclusion_header):]
    assert "short_positioning" in explicit_exclusion_section, (
        "short_positioning mention must sit under the explicit "
        "'## What this skill explicitly does NOT do' section header"
    )
