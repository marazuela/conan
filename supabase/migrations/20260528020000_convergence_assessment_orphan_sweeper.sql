-- 20260528020000_convergence_assessment_orphan_sweeper.sql
-- Wave 4 deep-fix Phase D.1 — defensive sweeper for orphan parent rows.
--
-- Why:
--   Even with the Phase B atomic RPC in place, a few low-probability
--   failure modes can still leak an orphan `convergence_assessments` row:
--
--     1. A Python-side bug between the RPC's "INSERT parent" and its
--        downstream INSERTs (e.g. someone refactors the RPC and breaks
--        atomicity).
--     2. A pre-Phase-B run that landed via the OLD two-call path with a
--        DELETE that subsequently failed (the "ROLLBACK FAILED" branch
--        in stage_10_persist that logged loudly but left the orphan).
--     3. An operator DELETE of secondary rows for diagnostics that
--        leaves the parent dangling.
--
--   Definition of "orphan":  a convergence_assessments row that's older
--   than 15 minutes (well past any pipeline's wall-clock) AND has zero
--   assessment_stage_metrics children. A real assessment ALWAYS writes
--   at least Stage 4 (`stage_4_reference_class_anchor`, deterministic)
--   into assessment_stage_metrics, so "0 children + > 15 min old" is a
--   strong signal of an orphan, not a partially-running assessment.
--
-- This migration:
--   1. Adds `orphan_sweeper` to the operator_flags.source CHECK whitelist
--      so the sweeper can emit a flag when it cleans something up.
--   2. Defines `cleanup_orphaned_assessments()` returning the deleted count.
--   3. Schedules the function on pg_cron every 15 minutes.
--
-- Rollback path:
--   SELECT cron.unschedule('orphan-sweep');
--   DROP FUNCTION cleanup_orphaned_assessments();
--   (Leave the operator_flags source whitelist alone — additive is safe.)

BEGIN;

-- ============================================================================
-- (1) Extend the operator_flags source whitelist with 'orphan_sweeper'
-- ============================================================================
-- Mirrors the pattern from 20260526000000 (constitutional_check +
-- memory_writeback). The full whitelist is the union of the prior list +
-- the new value.

ALTER TABLE public.operator_flags
  DROP CONSTRAINT IF EXISTS operator_flags_source_check;

ALTER TABLE public.operator_flags
  ADD CONSTRAINT operator_flags_source_check
  CHECK (source IN (
    'translation_health',
    'scanner_probe',
    'scanner_liveness',
    'convergence_qa',
    'candidate_aging',
    'thesis_writer',
    'reactor',
    'reporting_weekly',
    'litigation_baselines',
    'edgar_runtime_health',
    'scanner_failure_streak',
    'rollback_monitor',
    'orchestrator_cost',
    'thesis_jobs',
    'manual',
    'v3_pipeline_watchdog',
    'aging_review',
    'challenger_retro',
    'constitutional_check',
    'memory_writeback',
    'tier2_quality',
    'orphan_sweeper'
  ));

-- ============================================================================
-- (2) The sweeper function
-- ============================================================================
-- Returns the count of orphan rows deleted. Emits a single operator_flag
-- per run when the count is > 0 (no flag when zero — avoids dashboard noise).

CREATE OR REPLACE FUNCTION public.cleanup_orphaned_assessments()
RETURNS int
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_deleted_ids uuid[];
  v_count int;
BEGIN
  -- Find + delete in one statement so the sweeper is atomic. ON DELETE
  -- CASCADE on assessment_stage_metrics / hypothesis_enumeration /
  -- premortem_assessments / post_mortem_queue / sub_agent_calls means we
  -- only need to remove the parent — any half-written secondaries get
  -- cleaned automatically. (Though "0 children" is the orphan signal in
  -- the first place, so the cascade should be a no-op.)
  WITH orphans AS (
    DELETE FROM public.convergence_assessments ca
    WHERE ca.created_at < now() - interval '15 minutes'
      AND NOT EXISTS (
        SELECT 1 FROM public.assessment_stage_metrics asm
        WHERE asm.assessment_id = ca.id
      )
    RETURNING ca.id
  )
  SELECT array_agg(id) FROM orphans INTO v_deleted_ids;

  v_count := COALESCE(array_length(v_deleted_ids, 1), 0);

  IF v_count > 0 THEN
    INSERT INTO public.operator_flags (
      severity, source, kind, title, body, evidence
    ) VALUES (
      'warn',
      'orphan_sweeper',
      'convergence_orphan_deleted',
      format('Cleaned up %s orphan convergence_assessments row(s)', v_count),
      'Orphan parent rows with zero assessment_stage_metrics children, '
        || 'older than 15 minutes. Most likely a Stage 10 atomicity escape; '
        || 'investigate `orchestrator_runs` rows that produced these ids.',
      jsonb_build_object(
        'deleted_ids', to_jsonb(v_deleted_ids),
        'deleted_count', v_count,
        'sweeper_run_at', to_jsonb(now())
      )
    );
  END IF;

  RETURN v_count;
END;
$$;

COMMENT ON FUNCTION public.cleanup_orphaned_assessments() IS
  'Wave 4 deep-fix Phase D.1 — DELETE convergence_assessments rows older '
  'than 15 minutes with zero assessment_stage_metrics children. Emits a '
  '''orphan_sweeper'' operator_flag when count > 0. Scheduled via pg_cron '
  'every 15 minutes.';

GRANT EXECUTE ON FUNCTION public.cleanup_orphaned_assessments()
  TO service_role;

-- ============================================================================
-- (3) Schedule via pg_cron every 15 minutes
-- ============================================================================
-- DO block lets us unschedule a prior run cleanly (idempotent). pg_cron's
-- schedule names are unique per database, not per migration, so we guard
-- against double-schedule.

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM cron.job WHERE jobname = 'orphan-sweep'
  ) THEN
    PERFORM cron.unschedule('orphan-sweep');
  END IF;
  PERFORM cron.schedule(
    'orphan-sweep',
    '*/15 * * * *',
    $cmd$ SELECT public.cleanup_orphaned_assessments(); $cmd$
  );
END $$;

COMMIT;
