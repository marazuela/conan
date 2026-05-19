-- v3 pipeline watchdog — hourly silent-failure surfacing.
--
-- Why: On 2026-05-11 we discovered ~3 of the 6 v3 orchestrator stages
-- had been silently stalled for days (commented-out @modal.Period
-- decorator + missing pg_cron entries + missing per-asset bootstrap).
-- Nobody noticed until VERA's PDUFA-watchlist signal was flagged
-- manually. Hours wasted on root-cause investigation.
--
-- This watchdog runs hourly and writes `operator_flags` rows (already
-- surfaced by the dashboard) when any of seven known failure modes
-- trip. Dedup via `operator_flags_open_uniq` partial unique index —
-- a flag stays open until an operator resolves it; next cycle
-- re-trips after resolution only if the condition recurs.
--
-- Checks:
--   1. drainer_tier1_pending_too_long    critical  Tier-1 rows pending >15min
--   2. drainer_tier2_pending_too_long    warn      Tier-2 rows pending >6h
--   3. asset_linker_pass1_backlog        warn      >500 docs unlinked >6h
--   4. asset_linker_pass2_backlog        info      >50 low-conf links unverified >2h
--   5. fda_assets_no_docs                warn      >5 active assets >3d-old with 0 docs
--   6. compute_v3_400_recent             critical  >0 400s in the last hour
--   7. fact_extractor_stalled            warn      No facts in 24h despite material links
--
-- Thresholds chosen empirically from the 2026-05-11 state.
--
-- Rollback:
--   select cron.unschedule('v3-pipeline-watchdog');
--   drop function if exists public._v3_pipeline_watchdog();
--   -- Source allowlist update is best left in place; v3 pipeline
--   -- flags would have nowhere to write otherwise.

create extension if not exists pg_cron with schema extensions cascade;

-- --------------------------------------------------------------------
-- Extend operator_flags.source CHECK to allow 'v3_pipeline_watchdog'.
-- Idempotent: drop-and-recreate.
-- --------------------------------------------------------------------

alter table public.operator_flags
  drop constraint if exists operator_flags_source_check;

alter table public.operator_flags
  add constraint operator_flags_source_check
  check (source = any (array[
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
    'v3_pipeline_watchdog'
  ]));

-- --------------------------------------------------------------------
-- Function: _v3_pipeline_watchdog
-- --------------------------------------------------------------------

create or replace function public._v3_pipeline_watchdog()
returns jsonb
language plpgsql
security definer
set search_path = public
as $fn$
declare
  v_results jsonb := '{}'::jsonb;
  v_n integer;
  v_sample jsonb;
begin
  -- 1. Tier=1 drainer not consuming queue (CRITICAL)
  select count(*),
         coalesce(jsonb_agg(jsonb_build_object(
           'run_id', id, 'asset_id', asset_id,
           'scheduled_at', scheduled_at,
           'age_seconds', extract(epoch from (now() - scheduled_at))::int
         )) filter (where rn <= 5), '[]'::jsonb)
    into v_n, v_sample
    from (
      select id, asset_id, scheduled_at,
             row_number() over (order by scheduled_at) as rn
        from public.orchestrator_runs
       where status = 'pending' and tier = 1
         and scheduled_at < now() - interval '15 minutes'
    ) s;
  if v_n > 0 then
    insert into public.operator_flags (severity, source, kind, title, body, evidence)
    values ('critical', 'v3_pipeline_watchdog', 'drainer_tier1_pending_too_long',
            'Tier-1 orchestrator drainer not consuming queue',
            v_n || ' tier=1 row(s) pending > 15min - drainer cron may be down, '
              'compute_v3 endpoint unreachable, or Modal function failing on dispatch. '
              'Check cron.job_run_details + net._http_response.',
            jsonb_build_object('count', v_n, 'sample', v_sample, 'threshold_minutes', 15))
    on conflict do nothing;
  end if;
  v_results := v_results || jsonb_build_object('drainer_tier1_pending_too_long', v_n);

  -- 2. Tier=2 drainer not consuming queue (WARN)
  select count(*),
         coalesce(jsonb_agg(jsonb_build_object(
           'run_id', id, 'asset_id', asset_id, 'scheduled_at', scheduled_at
         )) filter (where rn <= 5), '[]'::jsonb)
    into v_n, v_sample
    from (
      select id, asset_id, scheduled_at,
             row_number() over (order by scheduled_at) as rn
        from public.orchestrator_runs
       where status = 'pending' and tier = 2
         and scheduled_at < now() - interval '6 hours'
    ) s;
  if v_n > 0 then
    insert into public.operator_flags (severity, source, kind, title, body, evidence)
    values ('warn', 'v3_pipeline_watchdog', 'drainer_tier2_pending_too_long',
            'Tier-2 Cowork queue not draining',
            v_n || ' tier=2 row(s) pending > 6h - Cowork tier-2 worker is '
              'missing, paused, or failing. Tier-1 drainer ignores these by design '
              '(filter on orchestrator_app.py:361). Either build the Cowork worker '
              'or convert rows to tier=1 manually.',
            jsonb_build_object('count', v_n, 'sample', v_sample, 'threshold_hours', 6))
    on conflict do nothing;
  end if;
  v_results := v_results || jsonb_build_object('drainer_tier2_pending_too_long', v_n);

  -- 3. Pass-1 asset_linker backlog (WARN)
  select count(*) into v_n
    from public.documents d
   where d.fetched_at < now() - interval '6 hours'
     and not exists (
       select 1 from public.asset_documents ad where ad.document_id = d.id
     );
  if v_n > 500 then
    insert into public.operator_flags (severity, source, kind, title, body, evidence)
    values ('warn', 'v3_pipeline_watchdog', 'asset_linker_pass1_backlog',
            'Sonnet asset_linker pass-1 falling behind ingestion',
            v_n || ' documents older than 6h have no asset_documents row. '
              'Pass-1 cron job v3-asset-linker-pass1 fires every 15 min with '
              '200 docs/batch - may need higher max_docs, more frequent cron, '
              'or there is a Sonnet failure mode silently consuming budget.',
            jsonb_build_object('count', v_n, 'threshold', 500))
    on conflict do nothing;
  end if;
  v_results := v_results || jsonb_build_object('asset_linker_pass1_backlog', v_n);

  -- 4. Pass-2 verifier backlog (INFO)
  select count(*) into v_n
    from public.asset_documents ad
   where ad.extraction_confidence < 0.80
     and ad.verified_by_pass2 is not true
     and ad.created_at < now() - interval '2 hours';
  if v_n > 50 then
    insert into public.operator_flags (severity, source, kind, title, body, evidence)
    values ('info', 'v3_pipeline_watchdog', 'asset_linker_pass2_backlog',
            'Haiku verifier pass-2 backlog growing',
            v_n || ' low-confidence (<0.80) pass-1 links unverified > 2h. '
              'Pass-2 cron job v3-asset-linker-pass2 fires at :10/:40. '
              'Budget cap is $2/run - may be exhausting on a bad batch.',
            jsonb_build_object('count', v_n, 'threshold', 50))
    on conflict do nothing;
  end if;
  v_results := v_results || jsonb_build_object('asset_linker_pass2_backlog', v_n);

  -- 5. Active fda_assets that have never received documents (WARN)
  -- 3-day threshold: daily ingestion + 15-min linker should populate
  -- within 24-48h; 3 days is the buffer.
  select count(*),
         coalesce(jsonb_agg(jsonb_build_object(
           'asset_id', id, 'ticker', ticker, 'drug_name', drug_name,
           'watch_priority', watch_priority,
           'age_days', extract(day from (now() - created_at))::int
         )) filter (where rn <= 10), '[]'::jsonb)
    into v_n, v_sample
    from (
      select a.id, a.ticker, a.drug_name, a.watch_priority, a.created_at,
             row_number() over (order by a.watch_priority asc nulls last,
                                         a.created_at asc) as rn
        from public.fda_assets a
       where a.is_active = true
         and a.created_at < now() - interval '3 days'
         and not exists (
           select 1 from public.asset_documents ad where ad.asset_id = a.id
         )
    ) s;
  if v_n > 5 then
    insert into public.operator_flags (severity, source, kind, title, body, evidence)
    values ('warn', 'v3_pipeline_watchdog', 'fda_assets_no_docs',
            'Active fda_assets have no linked documents',
            v_n || ' active assets >3 days old have zero asset_documents. '
              'Ingestion adapters'' broad sweeps may not cover them; run '
              '`modal run modal_workers/orchestrator_app.py::ingest_asset_corpus '
              '--asset-id <id>` per asset to bootstrap.',
            jsonb_build_object('count', v_n, 'sample', v_sample, 'threshold', 5))
    on conflict do nothing;
  end if;
  v_results := v_results || jsonb_build_object('fda_assets_no_docs', v_n);

  -- 6. compute_v3 endpoint returning 400s (CRITICAL)
  -- Catches "drain_orchestrator_queue" typo class of bug: pg_cron action
  -- name doesn't match COMPUTE_V3_ACTIONS allowlist.
  select count(*),
         coalesce(jsonb_agg(distinct substring(content from 1 for 200)), '[]'::jsonb)
    into v_n, v_sample
    from net._http_response
   where status_code = 400 and created > now() - interval '1 hour';
  if v_n > 0 then
    insert into public.operator_flags (severity, source, kind, title, body, evidence)
    values ('critical', 'v3_pipeline_watchdog', 'compute_v3_400_recent',
            'compute_v3 endpoint rejecting requests',
            v_n || ' HTTP 400 response(s) from compute_v3 in last hour. '
              'Common cause: pg_cron action name does not match '
              'COMPUTE_V3_ACTIONS in deployed Modal app (orchestrator_app.py). '
              'Check cron.job command vs valid_actions in the 400 response body.',
            jsonb_build_object('count', v_n, 'sample_responses', v_sample))
    on conflict do nothing;
  end if;
  v_results := v_results || jsonb_build_object('compute_v3_400_recent', v_n);

  -- 7. fact_extractor stalled (WARN)
  -- extracted_facts uses `extracted_at`, not `created_at`.
  select case
    when not exists (select 1 from public.asset_documents where is_material = true) then 0
    when (select max(extracted_at) from public.extracted_facts) is null then 1
    when (select max(extracted_at) from public.extracted_facts) < now() - interval '24 hours' then 1
    else 0
  end into v_n;
  if v_n > 0 then
    insert into public.operator_flags (severity, source, kind, title, body, evidence)
    values ('warn', 'v3_pipeline_watchdog', 'fact_extractor_stalled',
            'Sonnet fact_extractor not producing facts',
            'Material asset_documents exist but no extracted_facts rows in '
              'last 24h. fact_extractor cron job v3-fact-extractor fires '
              'hourly at :20. Check Modal logs for the function.',
            jsonb_build_object(
              'latest_fact_at', coalesce((select max(extracted_at) from public.extracted_facts), null),
              'material_link_count', (select count(*) from public.asset_documents where is_material = true)
            ))
    on conflict do nothing;
  end if;
  v_results := v_results || jsonb_build_object('fact_extractor_stalled', v_n);

  return v_results;
end;
$fn$;

comment on function public._v3_pipeline_watchdog() is
  'Hourly silent-failure surfacing for the v3 orchestrator pipeline. '
  'Writes operator_flags rows when any of 7 known failure modes trip. '
  'Dedup via operator_flags_open_uniq partial unique index. '
  'Returns jsonb summary {check_kind: count_tripped} for cron logs. '
  'Driven by pg_cron job v3-pipeline-watchdog (hourly at :05).';

-- --------------------------------------------------------------------
-- Schedule: hourly at :05 (offset from drainer */5 and pass-2 :10/:40).
-- --------------------------------------------------------------------

do $$
declare v_existing_jobid bigint;
begin
  select jobid into v_existing_jobid from cron.job where jobname = 'v3-pipeline-watchdog';
  if v_existing_jobid is not null then perform cron.unschedule(v_existing_jobid); end if;
  perform cron.schedule('v3-pipeline-watchdog', '5 * * * *',
    'select public._v3_pipeline_watchdog();');
end $$;
