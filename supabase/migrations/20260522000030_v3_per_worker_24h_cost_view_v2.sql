-- Extend v_cost_24h_by_worker (created in 20260522000010) to include the
-- new fact_extractor_runs table. Same uniform projection so dashboards can
-- treat all per-worker cost streams identically.

CREATE OR REPLACE VIEW public.v_cost_24h_by_worker AS
WITH conv AS (
  SELECT
    'convergence_assessments'::text AS worker,
    COUNT(*) AS runs,
    COALESCE(SUM(total_input_tokens), 0)::bigint AS input_tokens,
    COALESCE(SUM(total_output_tokens), 0)::bigint AS output_tokens,
    COALESCE(SUM(total_cache_read_tokens), 0)::bigint AS cache_read_tokens,
    COALESCE(SUM(total_cache_creation_tokens), 0)::bigint AS cache_creation_tokens,
    ROUND(COALESCE(SUM(cost_usd), 0)::numeric, 4) AS cost_usd
  FROM public.convergence_assessments
  WHERE created_at >= now() - interval '24 hours'
),
linker AS (
  SELECT
    'asset_linker_runs'::text AS worker,
    COUNT(*) AS runs,
    COALESCE(SUM(input_tokens), 0)::bigint AS input_tokens,
    COALESCE(SUM(output_tokens), 0)::bigint AS output_tokens,
    COALESCE(SUM(cache_read_tokens), 0)::bigint AS cache_read_tokens,
    COALESCE(SUM(cache_creation_tokens), 0)::bigint AS cache_creation_tokens,
    ROUND(COALESCE(SUM(cost_usd), 0)::numeric, 4) AS cost_usd
  FROM public.asset_linker_runs
  WHERE COALESCE(completed_at, started_at) >= now() - interval '24 hours'
),
stage AS (
  SELECT
    'assessment_stage_metrics'::text AS worker,
    COUNT(*) AS runs,
    COALESCE(SUM(input_tokens), 0)::bigint AS input_tokens,
    COALESCE(SUM(output_tokens), 0)::bigint AS output_tokens,
    COALESCE(SUM(cache_read_tokens), 0)::bigint AS cache_read_tokens,
    COALESCE(SUM(cache_creation_tokens), 0)::bigint AS cache_creation_tokens,
    ROUND(COALESCE(SUM(cost_usd), 0)::numeric, 4) AS cost_usd
  FROM public.assessment_stage_metrics
  WHERE created_at >= now() - interval '24 hours'
),
fact AS (
  SELECT
    'fact_extractor_runs'::text AS worker,
    COUNT(*) AS runs,
    COALESCE(SUM(input_tokens), 0)::bigint AS input_tokens,
    COALESCE(SUM(output_tokens), 0)::bigint AS output_tokens,
    COALESCE(SUM(cache_read_tokens), 0)::bigint AS cache_read_tokens,
    COALESCE(SUM(cache_creation_tokens), 0)::bigint AS cache_creation_tokens,
    ROUND(COALESCE(SUM(cost_usd), 0)::numeric, 4) AS cost_usd
  FROM public.fact_extractor_runs
  WHERE COALESCE(completed_at, started_at) >= now() - interval '24 hours'
)
SELECT * FROM conv
UNION ALL SELECT * FROM linker
UNION ALL SELECT * FROM stage
UNION ALL SELECT * FROM fact;
