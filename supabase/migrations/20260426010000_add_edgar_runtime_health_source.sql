-- Allow the EDGAR runtime observability sweep to write operator_flags.
-- This keeps EDGAR-specific degraded-run alerts inside the same operator_flags
-- surface as the rest of v2 observability.

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
    'manual'
  ));
