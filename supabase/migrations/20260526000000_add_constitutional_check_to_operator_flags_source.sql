-- 20260526000000_add_constitutional_check_to_operator_flags_source.sql
-- Add 'constitutional_check' + 'memory_writeback' to operator_flags.source
-- CHECK whitelist.
--
-- Why: Wave 2 emits an operator_flag from modal_workers/orchestrator_app.py
-- when a Stage 7 ConstitutionalFailure aborts a run (source=
-- 'constitutional_check'). Wave 4.2 emits one from runtime.py when the
-- asset-scope memory writeback fails after a successful assessment
-- (source='memory_writeback'). Neither value was in the source whitelist,
-- so the inserts would raise 23514 and be silently swallowed by their
-- emit-time try/except. Adding both unblocks the surface.

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
    'memory_writeback'
  ));
