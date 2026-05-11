-- v3 feedback-loop scheduling — pg_cron daily kickoff @ 02:00 UTC.
--
-- Why pg_cron and not @modal.Cron: Modal free tier caps schedule decorators
-- at 5 per workspace; conan-v2 already uses 5 (1 Period + 4 Cron) and
-- conan-v3-orchestrator's `orchestrator_drain_queue` consumes the 6th slot
-- via Period(minutes=5). A 7th `@modal.Cron` would push us over the cap.
--
-- The pg_cron job calls `_conan_modal_post_enqueue('compute_v3', body)`
-- with body `{"action":"feedback_loop_kickoff","args":{}}`. The compute_v3
-- multiplex endpoint dispatches by action; the new `feedback_loop_kickoff`
-- branch (modal_workers/orchestrator_app.py) looks up daily_feedback_loop
-- in the deployed conan-v3-feedback-loop app via Function.from_name and
-- .spawn()s it fire-and-forget so pg_net never blocks on the up-to-7200s
-- chain. The handle's function_call_id is dropped — pg_net's
-- net._http_response is the only paper trail (request_id from
-- _conan_modal_post_enqueue's bigint return, which pg_cron discards).
--
-- Drainer chain (drain_resolved_queue → rollback_monitor →
-- nightly_calibration_refit) lives in modal_workers/feedback_loop_app.py.
-- Each step has its own try/except so a failure in one doesn't gate the
-- others; see DECISIONS.md D-123 for the full contract.
--
-- Rollback: `select cron.unschedule('v3-feedback-loop-daily');` and (if
-- nothing else needs it) `drop extension pg_cron;`.

-- --------------------------------------------------------------------
-- 1. Enable pg_cron in the canonical extensions schema. Idempotent.
-- --------------------------------------------------------------------

create extension if not exists pg_cron with schema extensions cascade;

-- --------------------------------------------------------------------
-- 2. Schedule the daily 02:00 UTC kickoff.
--
-- cron.schedule returns a bigint job_id. Wrapped in a do-block so the
-- migration is idempotent (re-running unschedules the prior copy first).
-- --------------------------------------------------------------------

do $$
declare
  v_existing_jobid bigint;
begin
  select jobid into v_existing_jobid
    from cron.job
   where jobname = 'v3-feedback-loop-daily';

  if v_existing_jobid is not null then
    perform cron.unschedule(v_existing_jobid);
  end if;

  perform cron.schedule(
    'v3-feedback-loop-daily',
    '0 2 * * *',
    $cron$
      select public._conan_modal_post_enqueue(
        'compute_v3',
        jsonb_build_object(
          'action', 'feedback_loop_kickoff',
          'args',   '{}'::jsonb
        )
      );
    $cron$
  );
end
$$;

comment on extension pg_cron is
  'Used by v3-feedback-loop-daily (02:00 UTC). Sole pg_cron job in this DB '
  'as of 2026-05-08; safe to drop the extension if the job is unscheduled.';
