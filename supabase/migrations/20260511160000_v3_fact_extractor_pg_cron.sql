-- v3 fact_extractor scheduling — restore the pg_cron job dropped from
-- migration 20260511120911 (the second-attempt apply of v3_asset_linker_pg_cron
-- only scheduled pass-1 and pass-2, not fact_extractor, despite the source file
-- at 20260511112403_v3_asset_linker_pg_cron.sql including all three).
--
-- Problem: fact_extractor has been silent since 2026-05-08T11:04 UTC.
-- `extracted_facts` has not grown in 3 days; watchdog kind=fact_extractor_stalled
-- has been warn since today's deploy. Asset_linker pass-1/pass-2 are linking
-- docs, but no facts are being extracted from them.
--
-- Fix mirrors the v3-asset-linker-pass1 / pass-2 pattern: pg_cron POSTs to
-- compute_v3 multiplex; multiplex spawns fire-and-forget so pg_net returns
-- in <1s. No Modal cron slot consumed. Schedule is :20 hourly per the
-- 20260511112403 spec — sits between pass-2 (:10/:40) and pass-1 (:00/:15/:30/:45).
--
-- Worker side is already ready:
--   • fact_extractor_run registered in COMPUTE_V3_ACTIONS (orchestrator_app.py:645)
--   • _dispatch_compute_v3_action routes it (orchestrator_app.py:744)
--
-- Rollback:
--   select cron.unschedule('v3-fact-extractor');

do $$
declare
  v_existing_jobid bigint;
begin
  select jobid into v_existing_jobid
    from cron.job
   where jobname = 'v3-fact-extractor';

  if v_existing_jobid is not null then
    perform cron.unschedule(v_existing_jobid);
  end if;

  perform cron.schedule(
    'v3-fact-extractor',
    '20 * * * *',
    $cron$
      select public._conan_modal_post_enqueue(
        'compute_v3',
        jsonb_build_object(
          'action', 'fact_extractor_run',
          'args',   '{}'::jsonb
        )
      );
    $cron$
  );
end
$$;
