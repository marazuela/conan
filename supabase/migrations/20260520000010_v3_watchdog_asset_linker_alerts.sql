-- Extend _v3_pipeline_watchdog with two checks that would have caught the
-- 2026-05-11 incident before it ran for >2 hours undetected:
--
--   #8 asset_linker_burn_no_output   CRITICAL — 3+ recent pass1 runs each spent
--                                    >$1 and inserted 0 links. Smoking-gun
--                                    signature of the no-progress loop.
--   #9 asset_linker_burn_rate_high   WARN     — pass1+pass2 cost_usd sum in
--                                    last hour exceeds $10 (steady-state
--                                    should be ~$1-3/h post-fix).
--
-- Also FIXES existing check #3 (asset_linker_pass1_backlog): the old query
-- counted docs with no asset_documents row, which now permanently includes
-- every doc marked 'no_match'. Switched to documents.linker_classified_at IS
-- NULL so the alert measures genuine backlog, not legitimate no-match output.

CREATE OR REPLACE FUNCTION public._v3_pipeline_watchdog()
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare
  v_results jsonb := '{}'::jsonb;
  v_n integer;
  v_sample jsonb;
  v_cost numeric;
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

  -- 3. Pass-1 asset_linker backlog (WARN) — FIXED 2026-05-20:
  --    Old query "NOT EXISTS asset_documents" was wrong post-marker-fix:
  --    docs legitimately marked 'no_match' have no asset_documents row by
  --    design, so the alert would fire forever. Use the marker column now.
  select count(*) into v_n
    from public.documents d
   where d.fetched_at < now() - interval '6 hours'
     and d.linker_classified_at is null;
  if v_n > 500 then
    insert into public.operator_flags (severity, source, kind, title, body, evidence)
    values ('warn', 'v3_pipeline_watchdog', 'asset_linker_pass1_backlog',
            'Sonnet asset_linker pass-1 falling behind ingestion',
            v_n || ' documents older than 6h have linker_classified_at IS NULL. '
              'Pass-1 cron job v3-asset-linker-pass1 fires every 15 min with '
              '200 docs/batch - may need higher max_docs, more frequent cron, '
              'or there is a Sonnet failure mode (rate limits) leaving docs '
              'unmarked and re-queued.',
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

  -- 8. asset_linker burn-without-output (CRITICAL) — the 2026-05-11 signature.
  --    The fix in 20260520000000 makes infinite reprocessing impossible by
  --    construction (linker_classified_at is set after every paid call), but
  --    a regression in _mark_classified or a future schema change could
  --    silently revert that guarantee. Detect: 3 of the last 5 completed
  --    pass1 runs in the last hour each cost > $1.00 and inserted 0 links.
  select count(*) into v_n
    from (
      select id, cost_usd, links_inserted
        from public.asset_linker_runs
       where pass = 'pass1'
         and status = 'completed'
         and completed_at > now() - interval '1 hour'
       order by completed_at desc
       limit 5
    ) s
   where s.cost_usd > 1.00 and s.links_inserted = 0;
  if v_n >= 3 then
    select coalesce(jsonb_agg(jsonb_build_object(
             'run_id', id, 'completed_at', completed_at,
             'cost_usd', cost_usd, 'docs_seen', docs_seen,
             'prefilter_passed', prefilter_passed, 'api_calls', api_calls,
             'links_inserted', links_inserted
           ) order by completed_at desc), '[]'::jsonb)
      into v_sample
      from public.asset_linker_runs
     where pass = 'pass1' and status = 'completed'
       and completed_at > now() - interval '1 hour'
     limit 5;
    insert into public.operator_flags (severity, source, kind, title, body, evidence)
    values ('critical', 'v3_pipeline_watchdog', 'asset_linker_burn_no_output',
            'asset_linker burning $$$ with zero link output',
            v_n || ' of the last 5 pass1 runs each cost > $1.00 and produced '
              '0 links. This was the 2026-05-11 incident signature — the doc '
              'completion marker may have regressed. PAUSE the cron with '
              'cron.alter_job(active := false) on v3-asset-linker-pass1 and '
              'investigate documents.linker_classified_at updates.',
            jsonb_build_object('match_count', v_n, 'sample', v_sample,
                               'threshold_runs', 3, 'threshold_cost_usd', 1.00))
    on conflict do nothing;
  end if;
  v_results := v_results || jsonb_build_object('asset_linker_burn_no_output', v_n);

  -- 9. asset_linker hourly burn-rate ceiling (WARN) — catches a runaway long
  --    before the 24h $500 soft alert. Steady-state should be ~$1-3/hour;
  --    >$10/hour means something is wrong (regression, prefilter blew open,
  --    or asset population suddenly exploded).
  select coalesce(sum(cost_usd), 0) into v_cost
    from public.asset_linker_runs
   where status in ('completed', 'budget_exceeded')
     and completed_at > now() - interval '1 hour';
  if v_cost > 10.00 then
    insert into public.operator_flags (severity, source, kind, title, body, evidence)
    values ('warn', 'v3_pipeline_watchdog', 'asset_linker_burn_rate_high',
            'asset_linker hourly burn-rate exceeded',
            'asset_linker spent $' || to_char(v_cost, 'FM999.00') || ' in the '
              'last hour (threshold $10). Steady-state should be $1-3/hour. '
              'Check asset_linker_runs for an unusual links_inserted=0 streak, '
              'an outsized docs_seen, or a sudden prefilter_passed spike.',
            jsonb_build_object('cost_usd_1h', round(v_cost, 4),
                               'threshold_usd', 10.00))
    on conflict do nothing;
  end if;
  v_results := v_results || jsonb_build_object('asset_linker_burn_rate_1h_usd', round(v_cost, 4));

  return v_results;
end;
$function$;

COMMENT ON FUNCTION public._v3_pipeline_watchdog() IS
  'Hourly pipeline health check (cron v3-pipeline-watchdog @ :05). Inserts '
  'operator_flags for each breached threshold and returns a jsonb summary. '
  'Checks 8 and 9 added 2026-05-20 in response to the asset_linker no-progress '
  'incident — see operator_flags.kind asset_linker_burn_no_output / _burn_rate_high.';
