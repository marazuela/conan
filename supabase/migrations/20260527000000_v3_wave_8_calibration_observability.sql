-- 20260527000000_v3_wave_8_calibration_observability.sql
-- Wave 8 (orchestrator polish plan) — calibration observability surface.
--
-- Two changes:
--
-- 1. Add `calibration_status` enum-checked column to convergence_assessments.
--    Today `calibration_curve_version` is NULL both when:
--      (a) there is no is_active=true row in calibration_curves at all
--          (cold-start, expected on a fresh project), and
--      (b) the active row exists but its curve_data has no knots
--          (degenerate state, should never happen but might if the refit
--          script half-wrote a row).
--    A NULL version can't distinguish (a) from (b), and neither shows up
--    on the dashboard as "your conviction was NOT calibrated" — operators
--    silently see un-calibrated convictions branded as if they were. This
--    column makes the state explicit.
--
-- 2. Add 'calibration_refit' and 'tier2_quality' to the operator_flags.source
--    whitelist. Wave 8.3 emits source='calibration_refit' when the D-103
--    nightly gate fails (so a stale curve doesn't go unnoticed). The Phase 4B
--    Tier-2 quality gate at modal_workers/scripts/nightly_calibration_refit.py
--    already emits source='tier2_quality' — but that value was never in the
--    whitelist, so every Phase 4B emit since deploy has been silently
--    failing the 23514 CHECK and getting swallowed by its emit-time try/except.
--    Adding it here unblocks the latent surface.

-- Part 1 — calibration_status column
ALTER TABLE public.convergence_assessments
  ADD COLUMN IF NOT EXISTS calibration_status text;

-- CHECK constraint after column creation. CHECK constraints are non-blocking
-- for NULL, so historical rows keep their NULL value (we'll backfill via a
-- separate dashboard query if needed; per-row provenance isn't recoverable
-- for runs that pre-date this column).
ALTER TABLE public.convergence_assessments
  DROP CONSTRAINT IF EXISTS convergence_assessments_calibration_status_check;

ALTER TABLE public.convergence_assessments
  ADD CONSTRAINT convergence_assessments_calibration_status_check
  CHECK (
    calibration_status IS NULL
    OR calibration_status IN ('applied', 'no_active_curve', 'no_curve_data')
  );

COMMENT ON COLUMN public.convergence_assessments.calibration_status IS
  'Wave 8.1: whether Stage 8 isotonic calibration ran. NULL on pre-Wave-8 rows; '
  'thereafter one of: applied (curve had knots, conviction transformed), '
  'no_active_curve (no is_active=true row in calibration_curves), '
  'no_curve_data (active row had empty curve_data).';

-- Part 2 — operator_flags.source whitelist additions
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
    'calibration_refit',
    'tier2_quality'
  ));
