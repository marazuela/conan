-- 20260520000000_add_scanner_liveness_to_operator_flags_source.sql
-- Add 'scanner_liveness' to operator_flags.source CHECK whitelist for the new
-- scanner_liveness_sweep (F-204). Without it, every flag the sweep tries to
-- INSERT raises 23514 — the same regression class as F-216 (thesis_jobs).
--
-- Idempotent: DROP IF EXISTS + ADD.

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
    'manual'
  ));
