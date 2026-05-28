-- v_cost_24h_by_worker: add sub_agent_calls as a fifth source.
--
-- Yesterday (2026-05-27) sub_agent_calls burned $8.98 invisibly because the
-- cost view aggregates 4 tables — convergence_assessments, asset_linker_runs,
-- assessment_stage_metrics, fact_extractor_runs — and skipped sub_agent_calls.
-- See operator_flag 4fc126c0 and memory note sub_agent_schema_drift_2026-05-23.

create or replace view public.v_cost_24h_by_worker as
with conv as (
  select 'convergence_assessments'::text as worker,
         count(*) as runs,
         coalesce(sum(total_input_tokens), 0)::bigint as input_tokens,
         coalesce(sum(total_output_tokens), 0)::bigint as output_tokens,
         coalesce(sum(total_cache_read_tokens), 0)::bigint as cache_read_tokens,
         coalesce(sum(total_cache_creation_tokens), 0)::bigint as cache_creation_tokens,
         round(coalesce(sum(cost_usd), 0)::numeric, 4) as cost_usd
    from convergence_assessments
   where created_at >= now() - interval '24 hours'
), linker as (
  select 'asset_linker_runs'::text as worker,
         count(*) as runs,
         coalesce(sum(input_tokens), 0)::bigint as input_tokens,
         coalesce(sum(output_tokens), 0)::bigint as output_tokens,
         coalesce(sum(cache_read_tokens), 0)::bigint as cache_read_tokens,
         coalesce(sum(cache_creation_tokens), 0)::bigint as cache_creation_tokens,
         round(coalesce(sum(cost_usd), 0)::numeric, 4) as cost_usd
    from asset_linker_runs
   where coalesce(completed_at, started_at) >= now() - interval '24 hours'
), stage as (
  select 'assessment_stage_metrics'::text as worker,
         count(*) as runs,
         coalesce(sum(input_tokens), 0)::bigint as input_tokens,
         coalesce(sum(output_tokens), 0)::bigint as output_tokens,
         coalesce(sum(cache_read_tokens), 0)::bigint as cache_read_tokens,
         coalesce(sum(cache_creation_tokens), 0)::bigint as cache_creation_tokens,
         round(coalesce(sum(cost_usd), 0)::numeric, 4) as cost_usd
    from assessment_stage_metrics
   where created_at >= now() - interval '24 hours'
), fact as (
  select 'fact_extractor_runs'::text as worker,
         count(*) as runs,
         coalesce(sum(input_tokens), 0)::bigint as input_tokens,
         coalesce(sum(output_tokens), 0)::bigint as output_tokens,
         coalesce(sum(cache_read_tokens), 0)::bigint as cache_read_tokens,
         coalesce(sum(cache_creation_tokens), 0)::bigint as cache_creation_tokens,
         round(coalesce(sum(cost_usd), 0)::numeric, 4) as cost_usd
    from fact_extractor_runs
   where coalesce(completed_at, started_at) >= now() - interval '24 hours'
), sub_agent as (
  -- sub_agent_calls schema lacks split input/output token columns; only
  -- a single `tokens` integer is recorded. Surface as input_tokens to keep
  -- the union shape; output / cache columns stay 0.
  select ('sub_agent_calls:' || coalesce(role, 'unknown'))::text as worker,
         count(*) as runs,
         coalesce(sum(tokens), 0)::bigint as input_tokens,
         0::bigint as output_tokens,
         0::bigint as cache_read_tokens,
         0::bigint as cache_creation_tokens,
         round(coalesce(sum(cost_usd), 0)::numeric, 4) as cost_usd
    from sub_agent_calls
   where created_at >= now() - interval '24 hours'
   group by role
)
select * from conv
union all select * from linker
union all select * from stage
union all select * from fact
union all select * from sub_agent;
