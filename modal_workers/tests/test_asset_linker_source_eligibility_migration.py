"""Static checks for the asset_linker source-eligibility migrations (issue #54).

Two migrations:
  20260618000050_asset_linker_source_eligibility.sql            — table + seed + view
  20260618000060_asset_linker_source_eligibility_watchdog.sql   — orphan watchdog + cron

These assert the load-bearing invariants that, if they regressed, would either
make the linker go dark (seed maps the wrong program_status taxonomy) or break
the watchdog's NULL handling. They parse the SQL text — no DB needed.

Run: python -m pytest modal_workers/tests/test_asset_linker_source_eligibility_migration.py -v
"""
from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
MIG_DIR = REPO_ROOT / "supabase" / "migrations"
TABLE_MIG = MIG_DIR / "20260618000050_asset_linker_source_eligibility.sql"
WATCHDOG_MIG = MIG_DIR / "20260618000060_asset_linker_source_eligibility_watchdog.sql"


def _table_sql() -> str:
    return TABLE_MIG.read_text()


def _watchdog_sql() -> str:
    return WATCHDOG_MIG.read_text()


# ---------------------------------------------------------------------------
# Table + seed
# ---------------------------------------------------------------------------

def test_table_created_with_composite_pk() -> None:
    sql = _table_sql()
    assert "CREATE TABLE IF NOT EXISTS public.asset_linker_source_eligibility" in sql
    assert "program_status text NOT NULL" in sql
    assert "source         text NOT NULL" in sql
    assert "PRIMARY KEY (program_status, source)" in sql


def test_table_has_rls_enabled() -> None:
    """Matches peer operational tables (service-role-only): RLS on, no policies."""
    sql = _table_sql()
    assert ("ALTER TABLE public.asset_linker_source_eligibility ENABLE ROW LEVEL SECURITY"
            in sql)


def test_seed_uses_live_program_status_taxonomy_not_issue_hypothetical() -> None:
    """The critical correctness invariant: seed the LIVE taxonomy (phase2/phase3/
    filed/approved + the _unset sentinel), NOT the issue body's hypothetical
    phase_1/phase_2/under_review/etc. Seeding the hypothetical values would match
    zero active assets and the linker would go dark."""
    sql = _table_sql()
    # Live trial-stage values must be present and map to clinicaltrials.
    assert "('phase2',         'clinicaltrials'" in sql
    assert "('phase3',         'clinicaltrials'" in sql
    assert "('filed',          'clinicaltrials'" in sql
    # The hypothetical taxonomy from the issue must NOT have been seeded verbatim.
    for ghost in ("'phase_1'", "'phase_2'", "'phase_3'", "'pre_submission'",
                  "'under_review'", "'preclinical'"):
        assert ghost not in sql, (
            f"{ghost} is the issue's hypothetical taxonomy; production "
            "fda_assets.program_status never carries it — seeding it is a no-op "
            "that risks a dark linker"
        )


def test_seed_maps_null_sentinel_to_clinicaltrials() -> None:
    """NULL/'' program_status folds onto '_unset' (29 active assets as of
    2026-06-05); it must keep clinicaltrials eligible and must be a seeded row so
    the orphan watchdog does not flag NULL as unknown."""
    sql = _table_sql()
    assert "('_unset',         'clinicaltrials'" in sql


def test_seed_widens_post_approval_to_pharmacovigilance_sources() -> None:
    sql = _table_sql()
    for src in ("dailymed", "openfda", "federal_register"):
        assert f"('approved',       '{src}'" in sql
        assert f"('post_marketing', '{src}'" in sql
    # edgar must NOT be eligible for any status (gold set: 0/100 positives).
    assert "'edgar'" not in sql


def test_seed_is_idempotent() -> None:
    sql = _table_sql()
    assert "ON CONFLICT (program_status, source) DO NOTHING" in sql


# ---------------------------------------------------------------------------
# Resolved-sources view
# ---------------------------------------------------------------------------

def test_view_resolves_distinct_sources_for_active_assets() -> None:
    sql = _table_sql()
    assert "CREATE OR REPLACE VIEW public.asset_linker_eligible_sources AS" in sql
    assert "SELECT DISTINCT e.source" in sql
    assert "JOIN public.asset_linker_source_eligibility e" in sql
    # NULL/'' status folds onto the _unset sentinel.
    assert "COALESCE(NULLIF(a.program_status, ''), '_unset')" in sql
    assert "WHERE a.is_active = true" in sql


# ---------------------------------------------------------------------------
# Orphan watchdog
# ---------------------------------------------------------------------------

def test_watchdog_function_and_flag_shape() -> None:
    sql = _watchdog_sql()
    assert ("CREATE OR REPLACE FUNCTION public._asset_linker_source_eligibility_watchdog()"
            in sql)
    # Reuses an already-allow-listed operator_flags.source; disambiguated by kind.
    assert "'v3_pipeline_watchdog'" in sql
    assert "'asset_linker_source_eligibility_orphan'" in sql


def test_watchdog_excludes_null_via_sentinel() -> None:
    """NULL/'' status must fold onto '_unset' (which is seeded) so it is NOT
    reported as an orphan — only genuinely-unknown statuses fire."""
    sql = _watchdog_sql()
    assert "COALESCE(NULLIF(a.program_status, ''), '_unset')" in sql
    assert "WHERE a.is_active = true" in sql
    assert "NOT EXISTS" in sql


def test_watchdog_auto_resolves_when_clear() -> None:
    """Mirror the established watchdog pattern: resolve the open flag when no
    orphans remain."""
    sql = _watchdog_sql()
    assert "resolved_at = now()" in sql
    assert "kind = 'asset_linker_source_eligibility_orphan'" in sql
    assert "resolved_at IS NULL" in sql


def test_watchdog_scheduled_idempotently() -> None:
    sql = _watchdog_sql()
    assert "asset-linker-source-eligibility-watchdog" in sql
    assert "cron.unschedule" in sql
    assert "cron.schedule" in sql
    assert "SELECT public._asset_linker_source_eligibility_watchdog();" in sql
