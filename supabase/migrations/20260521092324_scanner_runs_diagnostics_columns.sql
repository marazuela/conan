-- Conan scanner observability — split diagnostics out of scanner_runs.errors.
--
-- Historically scanner_base stored warnings and run metrics inside the `errors`
-- jsonb array because scanner_runs had no better place for them. That made clean
-- runs render as "1 error" whenever a metrics payload was present.
--
-- Keep `errors` for actual failures only. New writer code fills these columns;
-- scanner-health keeps a fallback parser for old rows.

ALTER TABLE public.scanner_runs
  ADD COLUMN IF NOT EXISTS warnings jsonb NOT NULL DEFAULT '[]'::jsonb,
  ADD COLUMN IF NOT EXISTS run_metrics jsonb NOT NULL DEFAULT '{}'::jsonb;

COMMENT ON COLUMN public.scanner_runs.errors IS
  'Actual scanner failures only. Warnings and metrics live in scanner_runs.warnings/run_metrics.';

COMMENT ON COLUMN public.scanner_runs.warnings IS
  'Non-fatal warning strings produced by a scanner run.';

COMMENT ON COLUMN public.scanner_runs.run_metrics IS
  'Structured per-run metrics, not errors.';
