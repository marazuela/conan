"""Static regression checks for the v3 ingestion scheduler watchdog migration.

Run: python3 -m pytest modal_workers/tests/test_v3_ingestion_scheduler_watchdog_migration.py -v
"""
from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION = (
    REPO_ROOT
    / "supabase"
    / "migrations"
    / "20260523000000_v3_ingestion_scheduler_watchdog.sql"
)


def _sql() -> str:
    return MIGRATION.read_text()


def test_watchdog_tracks_all_critical_v3_ingestion_jobs() -> None:
    sql = _sql()

    assert "'v3-asset-linker-pass1'" in sql
    assert "'v3-asset-linker-pass2'" in sql
    assert "'v3-fact-extractor'" in sql


def test_watchdog_repairs_disabled_and_missing_schedules() -> None:
    sql = _sql()

    assert "cron.alter_job(v_jobid, active := true)" in sql
    assert "'asset_linker_run'" in sql
    assert "'asset_linker_pass2_run'" in sql
    assert "'fact_extractor_run'" in sql


def test_watchdog_surfaces_recovery_in_operator_flags() -> None:
    sql = _sql()

    assert "'v3_pipeline_watchdog'" in sql
    assert "'v3_ingestion_cron_repaired'" in sql
    assert "disabled_jobs" in sql
    assert "missing_jobs" in sql
    assert "resolved_note = 'v3 ingestion cron jobs are present and active.'" in sql


def test_watchdog_function_is_not_publicly_executable() -> None:
    sql = _sql()

    assert "REVOKE ALL ON FUNCTION public.v3_ingestion_scheduler_watchdog() FROM PUBLIC" in sql
    assert "REVOKE ALL ON FUNCTION public.v3_ingestion_scheduler_watchdog() FROM anon" in sql
    assert "REVOKE ALL ON FUNCTION public.v3_ingestion_scheduler_watchdog() FROM authenticated" in sql
