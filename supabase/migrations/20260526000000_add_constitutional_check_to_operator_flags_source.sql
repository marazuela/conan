-- 20260526000000_add_constitutional_check_to_operator_flags_source.sql
-- Add 'constitutional_check' to operator_flags.source CHECK whitelist.
--
-- Why: Wave 2 of the orchestrator polish plan emits an operator_flag from
-- modal_workers/orchestrator_app.py when a Stage 7 ConstitutionalFailure
-- aborts a run. The orchestrator_runs.status='failed_constitutional' patch
-- alone isn't surfaced on the dashboard's operator-flags page — the flag
-- gives the operator a discoverable, dismissible record with the finding
-- checks attached as evidence.
--
-- The current CHECK constraint (last redefined in 20260511115011 +
-- 20260513000000 + 20260520000010) does not include 'constitutional_check',
-- so the INSERT raises 23514. Adding the value here unblocks the emission.

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
    'constitutional_check'
  ));
