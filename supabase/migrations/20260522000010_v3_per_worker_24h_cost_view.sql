-- 24h-per-worker cost rollup view.
--
-- Context: 2026-05-11 token-burn incident — asset_linker pass-1 burned
-- ~$48 in 6 hours while the dashboard's existing v_latest_assessments_by_asset
-- view only surfaced convergence_assessments spend (~$8). This view exposes
-- the three independent cost streams side-by-side so a future spike on any
-- single worker is immediately visible.
--
-- Reads from already-existing cost columns:
--   - convergence_assessments.cost_usd       (orchestrator end-to-end runs)
--   - asset_linker_runs.cost_usd             (asset linker pass-1 + pass-2)
--   - assessment_stage_metrics.cost_usd      (per-stage detail, subset of conv)
--
-- Returns one row per worker per (rolling) 24h window. The 24h window slides
-- with now() — no parameters needed by the caller.

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
)
SELECT * FROM conv
UNION ALL SELECT * FROM linker
UNION ALL SELECT * FROM stage;

COMMENT ON VIEW public.v_cost_24h_by_worker IS
  '24h rolling cost per Claude-API worker. Catches single-worker spikes that '
  'the per-asset rollup hides. Added 2026-05-11 after asset_linker burned '
  '$48 in 6 hours while per-asset views showed only $8.';
