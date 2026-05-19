-- 20260531000040_v3_operator_flags_aging_review_source.sql
-- Extend operator_flags.source CHECK to allow the v3 fda_aging_review skill
-- to write its consecutive_failures>=3 warnings. The skill is the only writer
-- using this source. Mirrors the pattern from
-- 20260510000010_v3_stream6_safety_and_cleanup.sql:194.

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
    'aging_review'
  ));
