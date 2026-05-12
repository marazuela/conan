-- fact_extractor_runs — per-invocation observability row for the Sonnet
-- fact extractor (sonnet_fact_extractor.py).
--
-- Context: 2026-05-12 cost-defense pass. After the asset_linker $48 incident
-- the orchestrator and asset_linker each got their own per-component 24h
-- hard-halt (cost_budget.check_*_hard_halt). The fact_extractor had no
-- equivalent because it had no cost-bearing runs table — stats were kept in
-- memory and dropped at the end of main(). This table is the storage half
-- of the fact_extractor halt.
--
-- Shape mirrors asset_linker_runs so the v_cost_24h_by_worker view and any
-- future per-worker dashboards can union them with a uniform projection.

CREATE TABLE IF NOT EXISTS public.fact_extractor_runs (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    model               text NOT NULL,
    started_at          timestamptz NOT NULL DEFAULT now(),
    completed_at        timestamptz,
    status              text NOT NULL DEFAULT 'running'
                        CHECK (status IN ('running', 'completed', 'failed',
                                          'budget_exceeded', 'halted_24h')),
    docs_seen           integer NOT NULL DEFAULT 0,
    docs_extracted      integer NOT NULL DEFAULT 0,
    facts_inserted      integer NOT NULL DEFAULT 0,
    api_calls           integer NOT NULL DEFAULT 0,
    errors              integer NOT NULL DEFAULT 0,
    input_tokens        bigint  NOT NULL DEFAULT 0,
    output_tokens       bigint  NOT NULL DEFAULT 0,
    cache_read_tokens   bigint  NOT NULL DEFAULT 0,
    cache_creation_tokens bigint NOT NULL DEFAULT 0,
    cost_usd            numeric(10,4) NOT NULL DEFAULT 0,
    notes               text
);

-- At-most-one running row, same pattern as asset_linker_runs concurrency
-- guard so a second cron tick that overlaps a long run exits cleanly via
-- 409-conflict instead of double-billing.
CREATE UNIQUE INDEX IF NOT EXISTS fact_extractor_runs_one_running
    ON public.fact_extractor_runs (status)
    WHERE status = 'running';

-- Time-range scan index for the 24h cost rollup query
-- (check_fact_extractor_hard_halt → cost_budget.fact_extractor_24h_cost_usd).
CREATE INDEX IF NOT EXISTS fact_extractor_runs_started_at_idx
    ON public.fact_extractor_runs (started_at DESC);

COMMENT ON TABLE public.fact_extractor_runs IS
  'Per-invocation observability for sonnet_fact_extractor. Mirror of '
  'asset_linker_runs schema so the v_cost_24h_by_worker view can union '
  'them uniformly. Added 2026-05-12 as the storage half of the fact_'
  'extractor 24h hard-halt cost-defense.';
