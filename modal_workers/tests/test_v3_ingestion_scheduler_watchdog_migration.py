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
    / "20260601000000_skill_asset_linker_cutover.sql"
)


def _sql() -> str:
    return MIGRATION.read_text()


def test_watchdog_protects_v3_ingestion_crons_after_skill_cutover() -> None:
    """Post-skill-cutover + edge-prefilter migration, the watchdog protects
    two zero-LLM-cost crons: the deterministic doc/asset prefilter and the
    alias refresh. Asset-linker and fact-extractor crons are intentionally
    absent after the cutover."""
    sql = _sql()

    assert "'v3-doc-asset-prefilter'" in sql
    assert "'v3-asset-alias-weekly-refresh'" in sql

    # The v_expected array must contain both protected crons. We don't
    # pin the exact whitespace layout — just that each name appears inside
    # the array literal between the DECLARE block and the BEGIN.
    decl_start = sql.index("v_expected text[] := ARRAY[")
    decl_end = sql.index("BEGIN", decl_start)
    decl = sql[decl_start:decl_end]
    assert "'v3-doc-asset-prefilter'" in decl
    assert "'v3-asset-alias-weekly-refresh'" in decl

    # The legacy LLM crons must NOT be re-protected.
    assert "'v3-asset-linker-pass1'" not in decl
    assert "'v3-asset-linker-pass2'" not in decl
    assert "'v3-fact-extractor'" not in decl


def test_watchdog_does_not_recreate_asset_linker_schedules() -> None:
    sql = _sql()

    assert "cron.alter_job(v_jobid, active := true)" in sql
    assert "cron.unschedule(v_jobid)" in sql
    schedule_body = sql.split("CREATE OR REPLACE FUNCTION public.v3_ingestion_scheduler_watchdog()", 1)[1]
    assert "'asset_linker_run'" not in schedule_body
    assert "'asset_linker_pass2_run'" not in schedule_body
    assert "'fact_extractor_run'" not in schedule_body


def test_watchdog_surfaces_recovery_in_operator_flags() -> None:
    sql = _sql()

    assert "'v3_pipeline_watchdog'" in sql
    assert "'v3_ingestion_cron_repaired'" in sql
    assert "disabled_jobs" in sql
    assert "missing_jobs" in sql
    assert "asset_linker_mode" in sql
    # Operator flag body still calls out that LLM ingestion is disabled; the
    # exact phrasing also explains that the queue is edge-shaped.
    assert "intentionally disabled" in sql
    assert "cursor_skill_edge_queue" in sql


def test_watchdog_function_is_not_publicly_executable() -> None:
    sql = _sql()

    assert "REVOKE ALL ON FUNCTION public.v3_ingestion_scheduler_watchdog() FROM PUBLIC" in sql
    assert "REVOKE ALL ON FUNCTION public.v3_ingestion_scheduler_watchdog() FROM anon" in sql
    assert "REVOKE ALL ON FUNCTION public.v3_ingestion_scheduler_watchdog() FROM authenticated" in sql
