-- v3 orchestrator drain — pg_cron every 5 min.
--
-- Why pg_cron and not @modal.Period(minutes=5): Modal free tier caps schedule
-- decorators at 5 per workspace; conan-v2 already uses 5. Routing the drain
-- trigger through pg_cron + the existing compute_v3 multiplex keeps the cron
-- registration in Supabase and consumes zero Modal cron slots.
--
-- The pg_cron job calls `_conan_modal_post_enqueue('compute_v3', body)` with
-- body `{"action":"orchestrator_drain_queue","args":{}}`. The compute_v3
-- multiplex endpoint's `orchestrator_drain_queue` branch
-- (modal_workers/orchestrator_app.py) looks up orchestrator_drain_queue in
-- the deployed conan-v3-orchestrator app via Function.from_name and
-- .spawn()s it fire-and-forget so pg_net never blocks on the up-to-3600s
-- drain. The handle's function_call_id is dropped — pg_net's
-- net._http_response is the only paper trail.
--
-- Prereqs (must be true BEFORE this migration is applied):
--   1. conan-v3-orchestrator is redeployed with `orchestrator_drain_queue`
--      registered in COMPUTE_V3_ACTIONS + _dispatch_compute_v3_action.
--      Without that, the pg_cron POST will 400 with "unknown action".
--   2. `internal_config.modal_url_compute_v3` is seeded (already true since
--      Phase 4B compute RPCs migration 20260427010000).
--   3. `_conan_modal_post_enqueue` exists (already true; created by Phase 4B
--      migration 20260429020000_compute_rpcs_split_call.sql).
--
-- Rollback: `select cron.unschedule('v3-orchestrator-drain');`. Leaves the
-- queue without a consumer — re-enable Modal's @modal.Period schedule or
-- run the function manually until a new trigger is in place.

create extension if not exists pg_cron with schema extensions cascade;

do $$
declare
  v_existing_jobid bigint;
begin
  select jobid into v_existing_jobid
    from cron.job
   where jobname = 'v3-orchestrator-drain';

  if v_existing_jobid is not null then
    perform cron.unschedule(v_existing_jobid);
  end if;

  perform cron.schedule(
    'v3-orchestrator-drain',
    '*/5 * * * *',
    $cron$
      select public._conan_modal_post_enqueue(
        'compute_v3',
        jsonb_build_object(
          'action', 'orchestrator_drain_queue',
          'args',   '{}'::jsonb
        )
      );
    $cron$
  );
end
$$;
