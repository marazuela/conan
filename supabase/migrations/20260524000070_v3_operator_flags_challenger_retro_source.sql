-- 20260524000070_v3_operator_flags_challenger_retro_source.sql
-- Extend operator_flags.source CHECK to allow the v3 fda_challenger_replay
-- skill to write its per-run + rolling-30d drift flags. Mirrors the v2 source
-- 'challenger_retro' which already lived in v2 candidate_aging metrics writes
-- but was never in the v3 source enum. Without this, the skill's UPSERTs
-- would fail the CHECK; the skill currently falls back to stdout warnings.

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
    'challenger_retro'
  ));
