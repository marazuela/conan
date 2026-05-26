"""Static checks for the shared skill-run tracker migration.

Run: python -m pytest modal_workers/tests/test_skill_run_tracker_migration.py -v
"""
from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION = (
    REPO_ROOT
    / "supabase"
    / "migrations"
    / "20260613002000_skill_run_tracker.sql"
)


def _sql() -> str:
    return MIGRATION.read_text()


def test_skill_runs_ledger_shape() -> None:
    sql = _sql()

    assert "CREATE TABLE IF NOT EXISTS public.skill_runs" in sql
    assert "skill_name text NOT NULL" in sql
    assert "started_at timestamptz NOT NULL DEFAULT now()" in sql
    assert "last_heartbeat_at timestamptz NOT NULL DEFAULT now()" in sql
    assert "completed_at timestamptz" in sql
    assert "status IN ('running', 'completed', 'failed', 'skipped', 'cancelled', 'timeout')" in sql
    assert "metadata jsonb NOT NULL DEFAULT '{}'::jsonb" in sql
    assert "skill_runs_run_key_unique" in sql
    assert "WHERE run_key IS NOT NULL" in sql


def test_expectations_drive_silent_and_stale_alerts() -> None:
    sql = _sql()

    assert "CREATE TABLE IF NOT EXISTS public.skill_run_expectations" in sql
    assert "expected_interval interval" in sql
    assert "max_silence interval" in sql
    assert "stale_running_after interval NOT NULL DEFAULT interval '2 hours'" in sql
    assert "CREATE OR REPLACE VIEW public.v_skill_run_health" in sql
    assert "health_status" in sql
    assert "silent" in sql
    assert "stale_running" in sql


def test_helper_functions_cover_start_heartbeat_finish() -> None:
    sql = _sql()

    assert "CREATE OR REPLACE FUNCTION public.skill_run_start" in sql
    assert "ON CONFLICT (skill_name, run_key) WHERE run_key IS NOT NULL" in sql
    assert "CREATE OR REPLACE FUNCTION public.skill_run_heartbeat" in sql
    assert "AND status = 'running'" in sql
    assert "CREATE OR REPLACE FUNCTION public.skill_run_finish" in sql
    assert "p_status NOT IN ('completed', 'failed', 'skipped', 'cancelled', 'timeout')" in sql


def test_tracker_is_not_publicly_writable() -> None:
    sql = _sql()

    assert "ALTER TABLE public.skill_runs ENABLE ROW LEVEL SECURITY" in sql
    assert "ALTER TABLE public.skill_run_expectations ENABLE ROW LEVEL SECURITY" in sql
    assert "FOR SELECT" in sql
    assert "TO authenticated" in sql
    assert "REVOKE ALL ON FUNCTION public.skill_run_start" in sql
    assert "REVOKE ALL ON FUNCTION public.skill_run_heartbeat" in sql
    assert "REVOKE ALL ON FUNCTION public.skill_run_finish" in sql


def test_watchdog_writes_operator_flags_and_autoresolves() -> None:
    sql = _sql()

    assert "CREATE OR REPLACE FUNCTION public._skill_run_watchdog()" in sql
    assert "'skill_watchdog'" in sql
    assert "'skill_silent:' || v_row.skill_name" in sql
    assert "'skill_stale_running:' || v_row.skill_name" in sql
    assert "ON CONFLICT DO NOTHING" in sql
    assert "resolved_note = 'auto-resolved by _skill_run_watchdog" in sql
    assert "cron.schedule(" in sql
    assert "'skill-run-watchdog'" in sql

