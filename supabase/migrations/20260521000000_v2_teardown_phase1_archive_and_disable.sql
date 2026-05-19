-- v2 teardown — Phase 1: archive + disable. Additive, fully reversible.
--
-- This migration is the safety net + producer halt for the v2 teardown. It does
-- NOT drop any v2 tables; that comes in Phase 2 once the conan-dashboard PR has
-- removed all v2 reads. Splitting was forced by:
--   - Dashboard lives in a separate repo (marazuela/conan-dashboard) and still
--     queries signals/candidates/thesis_jobs/etc. — dropping schema first would
--     break /candidates, /archive, /profiles, /convergence, /signals, /flags,
--     and Workspace/sidebar counts on the live site.
--   - Several v2 tables turned out to be shared with v3 (alert_deliveries has
--     assessment_id → convergence_assessments; watchlists + annotations are
--     kept per PRD §IV; signal_band enum is referenced by fda_event_state +
--     fda_signal_promote_to_thesis). Drops need surgical column-level cleanup,
--     not blanket CASCADE.
--
-- Phase 1 (this migration):
--   1. CREATE SCHEMA archive_v2 + clone 12 v2 tables with data (audit trail
--      retained for ≥30 days post-Phase-2).
--   2. UPDATE public.scanners SET status='deprecated' for the 2 v2-emitting
--      scanners still active (edgar_filing_monitor 'operational',
--      insider_form4_scanner 'paused'). The other 15 v2 scanners are already
--      status='deprecated'. ('deprecated' is the existing CHECK-allowed value
--      used by every prior v2 scanner deactivation.)
--   3. Seed public.internal_config keys:
--        band_thresholds_v3 = {"immediate":60,"emerging":45,"watch":30}
--        thesis_daily_cap   = 15
--      (additive — readers wire up in follow-up PRs; nothing reads them today.)
--   4. CREATE TRIGGER orchestrator_runs_daily_cap_check enforcing the 15/day
--      cap at the DB level (F-002).
--
-- Phase 2 (deferred follow-up PR — DO NOT include here):
--   - DROP TABLE signals/candidates/thesis_jobs/alerts/candidate_events/...
--   - DROP COLUMN alert_id, candidate_event_id from alert_deliveries.
--   - DROP COLUMN signal_id, candidate_id from operator_flags.
--   - DROP COLUMN signal_id, candidate_id, thesis_job_id, failure_id from operator_actions.
--   - Drop v2-only RPCs/views/functions.
--
-- Rollback for Phase 1: every change is reversible.
--   - DROP SCHEMA archive_v2 CASCADE; restores the additive disk footprint.
--   - UPDATE public.scanners SET status='operational' WHERE name='edgar_filing_monitor';
--     UPDATE public.scanners SET status='paused' WHERE name='insider_form4_scanner';
--   - DELETE FROM internal_config WHERE key IN ('band_thresholds_v3','thesis_daily_cap');
--   - DROP TRIGGER orchestrator_runs_daily_cap_check_trg ON public.orchestrator_runs;
--     DROP FUNCTION public.orchestrator_runs_daily_cap_check();


-- ===========================================================================
-- 1) archive_v2 schema — clone every v2 table that currently has rows.
-- ===========================================================================
-- Pattern is `CREATE TABLE archive_v2.<t> AS SELECT * FROM public.<t>` so the
-- clone snapshots data + column types but NOT constraints/indexes/triggers.
-- That's intentional: archive_v2 is a read-only cold-storage tier; Phase 2
-- restore (if ever needed) requires replaying DDL from the original migrations.
--
-- archive_v2 schema is granted to service_role only — no anon/authenticated.

CREATE SCHEMA IF NOT EXISTS archive_v2
  AUTHORIZATION postgres;

COMMENT ON SCHEMA archive_v2 IS
  'v2 cold storage. Populated by migration 20260521000000 as a safety net before Phase 2 drops. Retain ≥30 days post-Phase-2 merge before dropping this schema.';

REVOKE ALL ON SCHEMA archive_v2 FROM PUBLIC;
GRANT USAGE ON SCHEMA archive_v2 TO service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA archive_v2
  GRANT SELECT ON TABLES TO service_role;

-- 12 tables with rows as of 2026-05-11. Idempotent: IF NOT EXISTS skips
-- archives that were created by a prior partial run.

CREATE TABLE IF NOT EXISTS archive_v2.signals
  AS SELECT * FROM public.signals;

CREATE TABLE IF NOT EXISTS archive_v2.thesis_jobs
  AS SELECT * FROM public.thesis_jobs;

CREATE TABLE IF NOT EXISTS archive_v2.alerts
  AS SELECT * FROM public.alerts;

CREATE TABLE IF NOT EXISTS archive_v2.candidates
  AS SELECT * FROM public.candidates;

CREATE TABLE IF NOT EXISTS archive_v2.candidate_events
  AS SELECT * FROM public.candidate_events;

CREATE TABLE IF NOT EXISTS archive_v2.outcomes
  AS SELECT * FROM public.outcomes;

CREATE TABLE IF NOT EXISTS archive_v2.signal_price_snapshots
  AS SELECT * FROM public.signal_price_snapshots;

CREATE TABLE IF NOT EXISTS archive_v2.phase3_base_rates
  AS SELECT * FROM public.phase3_base_rates;

CREATE TABLE IF NOT EXISTS archive_v2.thesis_drafting_failures
  AS SELECT * FROM public.thesis_drafting_failures;

CREATE TABLE IF NOT EXISTS archive_v2.alert_deliveries_v2only
  AS SELECT * FROM public.alert_deliveries
   WHERE assessment_id IS NULL;
-- alert_deliveries is shared (v3 uses assessment_id). Archive only the v2-only
-- rows; the v3 rows stay in public.alert_deliveries.

CREATE TABLE IF NOT EXISTS archive_v2.candidate_rationales
  AS SELECT * FROM public.candidate_rationales;

CREATE TABLE IF NOT EXISTS archive_v2.rubrics
  AS SELECT * FROM public.rubrics;


-- ===========================================================================
-- 2) Disable v2-emitting scanners (status='deprecated').
-- ===========================================================================
-- The other 15 v2 scanners are already status='deprecated'. The two below
-- still emit to public.signals: edgar_filing_monitor (currently 'operational')
-- and insider_form4_scanner (currently 'paused'; explicit demote so a future
-- unpause doesn't accidentally resurrect a v2 emitter). Moving to 'deprecated'
-- stops dispatcher from spawning them; Phase 2 can then drop public.signals
-- without crashing live scanner runs.
--
-- catalyst_universe fetchers (sec_8k_mna, fda_adcomm_pdufa) are unaffected —
-- they write to scanner_runs + catalyst_universe, not signals.
--
-- ('disabled' is not a valid scanners.status; the CHECK allows operational |
-- planned | deprecated | experimental | paused | shadow | shadow_with_emit.)

UPDATE public.scanners
   SET status = 'deprecated'
 WHERE name IN ('edgar_filing_monitor', 'insider_form4_scanner');


-- ===========================================================================
-- 3) Seed internal_config — band thresholds + thesis daily cap.
-- ===========================================================================
-- Both keys are additive. No code reads them yet; readers wire up in
-- follow-up PRs (rubric_engine, convergence.ts, reactor for thresholds; the
-- orchestrator_runs cap trigger below reads thesis_daily_cap).
--
-- Threshold values (60/45/30 on conviction_pct) reflect v3 PRD §V intent:
-- convergence_assessments.band is derived from conviction_pct ∈ [0,100].
-- v2's 30/20/10 thresholds applied to weighted scores ∈ [0,100] and aren't
-- relevant once v2 emission stops.

INSERT INTO public.internal_config (key, value) VALUES
  ('band_thresholds_v3',
   '{"immediate": 60, "emerging": 45, "watch": 30}'::jsonb),
  ('thesis_daily_cap',
   '15'::jsonb)
ON CONFLICT (key) DO UPDATE
   SET value = EXCLUDED.value,
       updated_at = now();


-- ===========================================================================
-- 4) 15/day cap trigger on orchestrator_runs (F-002).
-- ===========================================================================
-- Reads thesis_daily_cap from internal_config; raises on the (cap+1)th INSERT
-- of the current UTC day. Modal worker catches the SQLSTATE and writes a
-- failed_reactor_events row with source='cap'.
--
-- Only counts non-terminal runs (status IN ('pending','running','retrying'))
-- so a flood of completed historical backfills doesn't lock out today's work.

CREATE OR REPLACE FUNCTION public.orchestrator_runs_daily_cap_check()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
DECLARE
  v_cap     int;
  v_count   int;
BEGIN
  SELECT (value::text)::int
    INTO v_cap
    FROM public.internal_config
   WHERE key = 'thesis_daily_cap';

  IF v_cap IS NULL THEN
    -- No cap configured — bypass trigger entirely.
    RETURN NEW;
  END IF;

  SELECT count(*)
    INTO v_count
    FROM public.orchestrator_runs
   WHERE created_at::date = (now() AT TIME ZONE 'utc')::date
     AND status IN ('pending', 'running', 'retrying');

  IF v_count >= v_cap THEN
    RAISE EXCEPTION 'orchestrator_daily_cap_exceeded: % runs already today (cap=%)',
      v_count, v_cap
      USING ERRCODE = 'check_violation';
  END IF;

  RETURN NEW;
END;
$function$;

COMMENT ON FUNCTION public.orchestrator_runs_daily_cap_check() IS
  'F-002 enforcement: blocks INSERT when today already has thesis_daily_cap non-terminal orchestrator_runs (counts pending+running+retrying). Cap value reads from internal_config.thesis_daily_cap. Worker is expected to catch SQLSTATE 23514 and write failed_reactor_events with source=''cap''.';

DROP TRIGGER IF EXISTS orchestrator_runs_daily_cap_check_trg
  ON public.orchestrator_runs;

CREATE TRIGGER orchestrator_runs_daily_cap_check_trg
BEFORE INSERT ON public.orchestrator_runs
FOR EACH ROW
EXECUTE FUNCTION public.orchestrator_runs_daily_cap_check();
