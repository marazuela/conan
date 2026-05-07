-- 20260513000000_add_thesis_jobs_to_operator_flags_source.sql
-- Add 'thesis_jobs' to operator_flags.source CHECK whitelist.
--
-- Why: thesis_jobs_sla_sweeper (modal_workers/observability.py) writes flags
-- with source='thesis_jobs' (sla_breach_{status} kinds + the F-216
-- thesis_jobs_needs_scoring_aged kind). The source CHECK constraint
-- redefined in 20260507203610_v3_stream6_safety_and_cleanup.sql did not
-- include 'thesis_jobs', so every sweeper INSERT has been raising 23514
-- silently swallowed by dispatch_observability's try/except. F-216 deploy
-- (2026-05-08) hit this on smoke and exposed the regression.

ALTER TABLE public.operator_flags
  DROP CONSTRAINT IF EXISTS operator_flags_source_check;

ALTER TABLE public.operator_flags
  ADD CONSTRAINT operator_flags_source_check
  CHECK (source IN (
    'translation_health',
    'scanner_probe',
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
    'manual'
  ));
