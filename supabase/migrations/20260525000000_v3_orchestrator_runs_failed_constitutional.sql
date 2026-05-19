-- Add 'failed_constitutional' to orchestrator_runs.status CHECK constraint.
--
-- Stage 7 (constitutional check) in the v3 orchestrator was previously
-- informational-only: pass/fail and findings were persisted to
-- assessment_stage_metrics + convergence_assessments, but the pipeline
-- proceeded to Stage 10 regardless. D-117 documented the intent
-- ("structural errors MUST gate the assessment") without enforcing it.
--
-- This migration adds the status value used by the new abort path. When
-- Stage 7 returns pass_=False (any error-severity finding from the
-- deterministic citation walker, promoted Stage 2/3 structural error,
-- or semantic check), orchestrator_runtime.runtime._run_one_inner now
-- raises ConstitutionalFailure before Stage 10 persist. The caller
-- (modal_workers/orchestrator_app.drain_queue) catches it and writes
-- status='failed_constitutional' so dashboards distinguish from generic
-- exceptions ('failed') and budget kills ('killed_budget').
--
-- No convergence_assessments row is created on this path.

ALTER TABLE public.orchestrator_runs
  DROP CONSTRAINT IF EXISTS orchestrator_runs_status_check;

ALTER TABLE public.orchestrator_runs
  ADD CONSTRAINT orchestrator_runs_status_check
  CHECK (status = ANY (ARRAY[
    'pending'::text,
    'running'::text,
    'completed'::text,
    'skipped_dedupe'::text,
    'skipped_budget'::text,
    'killed_budget'::text,
    'failed'::text,
    'failed_constitutional'::text
  ]));

COMMENT ON COLUMN public.orchestrator_runs.status IS
  'pending | running | completed | skipped_dedupe | skipped_budget | killed_budget | failed | failed_constitutional. The failed_constitutional value is set when Stage 7 produces an error-severity finding (unresolved citation, structural Stage 2/3 error, or semantic check fail) and the pipeline aborted before Stage 10. No convergence_assessments row exists for these runs; partial cost is recorded in cost_actual_usd.';
