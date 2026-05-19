-- v3 ingestion run-summary rollups.
--
-- Production already has these operational tables, but they were missing from
-- repo migrations. The Modal asset linker and fact extractor write one row at
-- the end of each run so audits can distinguish "worker did nothing" from
-- "worker failed before summary persistence".

CREATE TABLE IF NOT EXISTS public.asset_linker_runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  pass text NOT NULL,
  model text NOT NULL,
  started_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  status text NOT NULL,
  docs_seen int NOT NULL DEFAULT 0,
  prefilter_passed int NOT NULL DEFAULT 0,
  prefilter_skipped int NOT NULL DEFAULT 0,
  api_calls int NOT NULL DEFAULT 0,
  errors int NOT NULL DEFAULT 0,
  links_inserted int NOT NULL DEFAULT 0,
  links_dedup_skipped int NOT NULL DEFAULT 0,
  input_tokens bigint NOT NULL DEFAULT 0,
  output_tokens bigint NOT NULL DEFAULT 0,
  cache_read_tokens bigint NOT NULL DEFAULT 0,
  cache_creation_tokens bigint NOT NULL DEFAULT 0,
  cost_usd numeric NOT NULL DEFAULT 0,
  notes text
);

ALTER TABLE public.asset_linker_runs
  ADD COLUMN IF NOT EXISTS pass text,
  ADD COLUMN IF NOT EXISTS model text,
  ADD COLUMN IF NOT EXISTS started_at timestamptz DEFAULT now(),
  ADD COLUMN IF NOT EXISTS completed_at timestamptz,
  ADD COLUMN IF NOT EXISTS status text,
  ADD COLUMN IF NOT EXISTS docs_seen int DEFAULT 0,
  ADD COLUMN IF NOT EXISTS prefilter_passed int DEFAULT 0,
  ADD COLUMN IF NOT EXISTS prefilter_skipped int DEFAULT 0,
  ADD COLUMN IF NOT EXISTS api_calls int DEFAULT 0,
  ADD COLUMN IF NOT EXISTS errors int DEFAULT 0,
  ADD COLUMN IF NOT EXISTS links_inserted int DEFAULT 0,
  ADD COLUMN IF NOT EXISTS links_dedup_skipped int DEFAULT 0,
  ADD COLUMN IF NOT EXISTS input_tokens bigint DEFAULT 0,
  ADD COLUMN IF NOT EXISTS output_tokens bigint DEFAULT 0,
  ADD COLUMN IF NOT EXISTS cache_read_tokens bigint DEFAULT 0,
  ADD COLUMN IF NOT EXISTS cache_creation_tokens bigint DEFAULT 0,
  ADD COLUMN IF NOT EXISTS cost_usd numeric DEFAULT 0,
  ADD COLUMN IF NOT EXISTS notes text;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'public.asset_linker_runs'::regclass
      AND conname = 'asset_linker_runs_pass_check'
  ) THEN
    ALTER TABLE public.asset_linker_runs
      ADD CONSTRAINT asset_linker_runs_pass_check
      CHECK (pass IN ('pass1','pass2','cowork_backfill'));
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'public.asset_linker_runs'::regclass
      AND conname = 'asset_linker_runs_status_check'
  ) THEN
    ALTER TABLE public.asset_linker_runs
      ADD CONSTRAINT asset_linker_runs_status_check
      CHECK (status IN ('running','completed','failed','budget_exceeded'));
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS asset_linker_runs_started_idx
  ON public.asset_linker_runs(started_at DESC);

CREATE TABLE IF NOT EXISTS public.fact_extractor_runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  model text NOT NULL,
  started_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  status text NOT NULL DEFAULT 'running',
  docs_seen int NOT NULL DEFAULT 0,
  docs_extracted int NOT NULL DEFAULT 0,
  facts_inserted int NOT NULL DEFAULT 0,
  api_calls int NOT NULL DEFAULT 0,
  errors int NOT NULL DEFAULT 0,
  input_tokens bigint NOT NULL DEFAULT 0,
  output_tokens bigint NOT NULL DEFAULT 0,
  cache_read_tokens bigint NOT NULL DEFAULT 0,
  cache_creation_tokens bigint NOT NULL DEFAULT 0,
  cost_usd numeric NOT NULL DEFAULT 0,
  notes text
);

ALTER TABLE public.fact_extractor_runs
  ADD COLUMN IF NOT EXISTS model text,
  ADD COLUMN IF NOT EXISTS started_at timestamptz DEFAULT now(),
  ADD COLUMN IF NOT EXISTS completed_at timestamptz,
  ADD COLUMN IF NOT EXISTS status text DEFAULT 'running',
  ADD COLUMN IF NOT EXISTS docs_seen int DEFAULT 0,
  ADD COLUMN IF NOT EXISTS docs_extracted int DEFAULT 0,
  ADD COLUMN IF NOT EXISTS facts_inserted int DEFAULT 0,
  ADD COLUMN IF NOT EXISTS api_calls int DEFAULT 0,
  ADD COLUMN IF NOT EXISTS errors int DEFAULT 0,
  ADD COLUMN IF NOT EXISTS input_tokens bigint DEFAULT 0,
  ADD COLUMN IF NOT EXISTS output_tokens bigint DEFAULT 0,
  ADD COLUMN IF NOT EXISTS cache_read_tokens bigint DEFAULT 0,
  ADD COLUMN IF NOT EXISTS cache_creation_tokens bigint DEFAULT 0,
  ADD COLUMN IF NOT EXISTS cost_usd numeric DEFAULT 0,
  ADD COLUMN IF NOT EXISTS notes text;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'public.fact_extractor_runs'::regclass
      AND conname = 'fact_extractor_runs_status_check'
  ) THEN
    ALTER TABLE public.fact_extractor_runs
      ADD CONSTRAINT fact_extractor_runs_status_check
      CHECK (status IN ('running','completed','failed','budget_exceeded','halted_24h'));
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS fact_extractor_runs_started_idx
  ON public.fact_extractor_runs(started_at DESC);

COMMENT ON TABLE public.asset_linker_runs IS
  'Operational rollups for v3 asset linker pass-1/pass-2 runs.';
COMMENT ON TABLE public.fact_extractor_runs IS
  'Operational rollups for v3 structured fact extractor runs.';
