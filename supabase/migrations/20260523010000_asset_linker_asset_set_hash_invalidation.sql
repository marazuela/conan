-- Asset-set-hash invalidation for asset_linker pass-1
--
-- Problem: PR #29 added documents.linker_classified_at as a terminal marker
-- to stop the re-Sonnet loop. PR #29's commit message acknowledged a missing
-- piece: "If new assets are added later, a separate trigger should reset
-- linker_classified_at = NULL for docs needing re-evaluation." That trigger
-- was never built. Result: every doc that pass-1 ever processed against a
-- subset of the eventual asset universe is permanently excluded, even after
-- new assets are added. Today's failure mode: 8,906 docs stamped 'no_match'
-- against an evolving asset set, 99 stamped 'linked', pipeline starved.
--
-- Fix: stamp a hash of the active asset id set alongside linker_classified_at.
-- Pass-1's doc-loading filter widens to:
--   linker_classified_at IS NULL
--     OR linker_classified_asset_set_hash IS NULL
--     OR linker_classified_asset_set_hash <> <current_hash>
-- Adding or removing assets changes the hash and auto-invalidates the
-- previously-stamped corpus. On deploy, every existing stamped row has
-- asset_set_hash IS NULL → naturally re-pulled. One-time recovery, no
-- separate backfill SQL.
--
-- Also updates _v3_pipeline_watchdog check #8 (asset_linker_burn_no_output).
-- The old threshold (3 of 5 runs each >$1 cost, 0 links) was invisible to the
-- 2026-05-12 incident: $0.05-$0.17/run sustained, 0 links/run, undetected for
-- 30+ hours. New rule: last 4 completed pass-1 runs each made >0 Sonnet calls
-- AND inserted 0 links — fires after ~1h of dead output regardless of cost.

ALTER TABLE public.documents
  ADD COLUMN IF NOT EXISTS linker_classified_asset_set_hash text;

COMMENT ON COLUMN public.documents.linker_classified_asset_set_hash IS
  'md5(sorted_asset_ids) at the time linker_classified_at was set. When the '
  'active fda_assets set changes, the hash differs from current and pass-1 '
  're-evaluates the doc. NULL = legacy stamp (pre-hash) — gets re-evaluated '
  'on next run. See modal_workers/extractor/asset_linker.py:_active_asset_set_hash.';


-- Partial index for the steady-state hot path: most docs have hash matching
-- current and are excluded; the remaining "needs re-eval" set is the union
-- of (linker_classified_at IS NULL) and (hash IS NULL or mismatched). The
-- existing documents_linker_unclassified_idx covers the IS NULL branch.
-- The mismatch branch is rare in steady state — sequential scan acceptable.


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

  -- 3. Pass-1 asset_linker backlog (WARN). Counts docs that have never been
  --    classified OR are legacy pre-hash rows (asset_set_hash IS NULL) that
  --    pass-1 will re-evaluate. Hash-stale rows where the hash IS set but
  --    differs from current are a transient (only spike when the asset set
  --    changes) and not tracked here — keeps the check independent of any
  --    runtime config row.
  select count(*) into v_n
    from public.documents d
   where d.fetched_at < now() - interval '6 hours'
     and (
       d.linker_classified_at is null
       or d.linker_classified_asset_set_hash is null
     );
  if v_n > 500 then
    insert into public.operator_flags (severity, source, kind, title, body, evidence)
    values ('warn', 'v3_pipeline_watchdog', 'asset_linker_pass1_backlog',
            'Sonnet asset_linker pass-1 falling behind ingestion',
            v_n || ' documents older than 6h need pass-1 evaluation '
              '(linker_classified_at IS NULL or legacy pre-hash stamp). '
              'Pass-1 cron job v3-asset-linker-pass1 fires every 15 min with '
              '200 docs/batch — may need higher max_docs, more frequent cron, '
              'or there is a Sonnet failure mode leaving docs unmarked.',
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

  -- 8. asset_linker burn-without-output (CRITICAL). REVISED 2026-05-23:
  --    Old threshold (3 of last 5 runs each >$1 cost AND 0 links) was
  --    invisible to the 2026-05-12 incident — the actual pattern was
  --    sustained $0.05-$0.17/run for >30h with 0 links/run, never tripping
  --    the $1 cost floor. New rule: last 4 completed pass-1 runs each made
  --    >0 Sonnet calls AND inserted 0 links. Fires after ~1h of dead output.
  --    api_calls > 0 excludes "prefilter-skipped everything" runs (legitimate
  --    no-FDA-content windows that should not alert).
  select count(*) into v_n
    from (
      select id, cost_usd, links_inserted, api_calls
        from public.asset_linker_runs
       where pass = 'pass1'
         and status = 'completed'
         and completed_at > now() - interval '2 hours'
       order by completed_at desc
       limit 4
    ) s
   where s.links_inserted = 0
     and s.api_calls > 0;
  if v_n >= 4 then
    select coalesce(jsonb_agg(jsonb_build_object(
             'run_id', id, 'completed_at', completed_at,
             'cost_usd', cost_usd, 'docs_seen', docs_seen,
             'prefilter_passed', prefilter_passed, 'api_calls', api_calls,
             'links_inserted', links_inserted
           ) order by completed_at desc), '[]'::jsonb)
      into v_sample
      from public.asset_linker_runs
     where pass = 'pass1' and status = 'completed'
       and completed_at > now() - interval '2 hours'
     limit 4;
    insert into public.operator_flags (severity, source, kind, title, body, evidence)
    values ('critical', 'v3_pipeline_watchdog', 'asset_linker_burn_no_output',
            'asset_linker burning Sonnet calls with zero link output',
            'Last 4 completed pass1 runs each made >0 Sonnet API calls and '
              'produced 0 links. This is the 2026-05-12 incident signature '
              '(quiet $0.05-$0.17/run burn). Either the prompt regressed, the '
              'prefilter is letting noise through, or the asset-set-hash is '
              'churning every run. PAUSE the cron with '
              'cron.alter_job(active := false) on v3-asset-linker-pass1 and '
              'inspect recent asset_linker_runs rows + the latest hash.',
            jsonb_build_object('match_count', v_n, 'sample', v_sample,
                               'threshold_runs', 4,
                               'threshold_window', '2 hours'))
    on conflict do nothing;
  end if;
  v_results := v_results || jsonb_build_object('asset_linker_burn_no_output', v_n);

  -- 9. asset_linker hourly burn-rate ceiling (WARN). Catches a runaway long
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
  'Check #8 (asset_linker_burn_no_output) revised 2026-05-23: now fires on '
  'last 4 runs with >0 api_calls and 0 links_inserted regardless of cost, '
  'after the 2026-05-12 incident proved the cost-based threshold was blind.';
