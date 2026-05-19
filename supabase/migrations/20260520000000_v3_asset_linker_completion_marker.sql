-- v3 asset_linker completion marker + run-level cost observability
--
-- Fixes the no-progress loop where docs returning zero links were never
-- marked as "classified" and got re-Sonneted every 15 minutes.
--
-- 1. documents.linker_classified_at / linker_classified_result — terminal
--    state on every classification attempt that paid for a Sonnet call OR
--    was deterministically skipped by the prefilter. Transient API errors
--    still leave the columns NULL so the next cron run retries.
--
-- 2. asset_linker_runs — per-cron-invocation cost rollup so the budget
--    guardrails in cost_budget.py and the dashboard burn KPI can see linker
--    spend. Mirrors the LinkerStats / Pass2Stats dataclasses 1:1.

ALTER TABLE public.documents
  ADD COLUMN IF NOT EXISTS linker_classified_at timestamptz,
  ADD COLUMN IF NOT EXISTS linker_classified_result text;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.constraint_column_usage
    WHERE table_schema='public' AND table_name='documents'
      AND constraint_name='documents_linker_classified_result_check'
  ) THEN
    ALTER TABLE public.documents
      ADD CONSTRAINT documents_linker_classified_result_check
      CHECK (linker_classified_result IS NULL
             OR linker_classified_result IN ('linked','no_match','parse_error'));
  END IF;
END$$;

-- Partial index covers the hot path: "next batch to classify, newest first".
CREATE INDEX IF NOT EXISTS documents_linker_unclassified_idx
  ON public.documents (published_at DESC)
  WHERE linker_classified_at IS NULL;


CREATE TABLE IF NOT EXISTS public.asset_linker_runs (
  id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  pass                    text NOT NULL CHECK (pass IN ('pass1','pass2')),
  model                   text NOT NULL,
  started_at              timestamptz NOT NULL DEFAULT now(),
  completed_at            timestamptz,
  status                  text NOT NULL
                           CHECK (status IN ('running','completed','failed','budget_exceeded')),
  docs_seen               int NOT NULL DEFAULT 0,
  prefilter_passed        int NOT NULL DEFAULT 0,
  prefilter_skipped       int NOT NULL DEFAULT 0,
  api_calls               int NOT NULL DEFAULT 0,
  errors                  int NOT NULL DEFAULT 0,
  links_inserted          int NOT NULL DEFAULT 0,
  links_dedup_skipped     int NOT NULL DEFAULT 0,
  input_tokens            bigint NOT NULL DEFAULT 0,
  output_tokens           bigint NOT NULL DEFAULT 0,
  cache_read_tokens       bigint NOT NULL DEFAULT 0,
  cache_creation_tokens   bigint NOT NULL DEFAULT 0,
  cost_usd                numeric(10,4) NOT NULL DEFAULT 0,
  notes                   text
);

CREATE INDEX IF NOT EXISTS asset_linker_runs_started_idx
  ON public.asset_linker_runs (started_at DESC);

CREATE INDEX IF NOT EXISTS asset_linker_runs_completed_idx
  ON public.asset_linker_runs (completed_at DESC NULLS FIRST)
  WHERE status = 'completed';

COMMENT ON TABLE public.asset_linker_runs IS
  'Per-invocation rollup for asset_linker pass-1/pass-2. cost_usd is summed '
  'into the global 24h budget check in cost_budget.global_24h_cost_usd().';

COMMENT ON COLUMN public.documents.linker_classified_at IS
  'Set after every terminal pass-1 outcome (linked / no_match / parse_error). '
  'NULL = retry on next cron run. Transient API errors leave this NULL.';
