-- =============================================================================
-- D-131 follow-up — Phase 3a/3b cron-drift fix
--
-- Three pg_cron jobs were originally scheduled by:
--   20260605000050_earnings_calendar_pg_cron.sql      (earnings-calendar-daily)
--   20260605000060_fomc_calendar_pg_cron.sql          (fomc-calendar-monthly)
--   20260612000020_harvest_fda_events_pg_cron.sql     (fda-event-harvest-daily)
--
-- All three matched the broken pattern that 7e83319 fixed for
-- bc-class-precedent-refresh-daily:
--
--   1. Read `compute_secret` from `vault.decrypted_secrets` (returns NULL on
--      this project — secret lives in `public.internal_config.compute_secret`
--      per the compute_auth_setup memory dated 2026-04-22).
--   2. POST an empty `{}` body with `Authorization: Bearer …`. The
--      compute_v3_dispatch endpoint requires `x-conan-compute-secret` and
--      `{action, args}` body — so the request 401's silently.
--
-- The live database has already been patched (presumably by the PR #147 /
-- 054f428 compute_v3 multiplex work) to call `public._conan_modal_post_enqueue`,
-- which sources the secret from internal_config and sends the right header.
-- This migration reconciles the repo with that live state so that a fresh
-- replay of supabase/migrations/ doesn't reintroduce the broken vault pattern.
--
-- All three jobs are idempotently unscheduled + rescheduled. The body shape
-- mirrors the bc-class-precedent-refresh fix in 20260613005100.
-- =============================================================================

DO $$
DECLARE
  v_existing_jobid bigint;
BEGIN
  -- earnings-calendar-daily ------------------------------------------------
  SELECT jobid INTO v_existing_jobid
    FROM cron.job WHERE jobname = 'earnings-calendar-daily';
  IF v_existing_jobid IS NOT NULL THEN
    PERFORM cron.unschedule(v_existing_jobid);
  END IF;

  PERFORM cron.schedule(
    'earnings-calendar-daily',
    '10 6 * * *',
    $cron$
      SELECT public._conan_modal_post_enqueue(
        'compute_v3',
        jsonb_build_object(
          'action', 'earnings_calendar_fetch_daily',
          'args',   jsonb_build_object('window_days', 7, 'forward_days', 90)
        )
      );
    $cron$
  );

  -- fomc-calendar-monthly --------------------------------------------------
  SELECT jobid INTO v_existing_jobid
    FROM cron.job WHERE jobname = 'fomc-calendar-monthly';
  IF v_existing_jobid IS NOT NULL THEN
    PERFORM cron.unschedule(v_existing_jobid);
  END IF;

  PERFORM cron.schedule(
    'fomc-calendar-monthly',
    '15 6 1 * *',
    $cron$
      SELECT public._conan_modal_post_enqueue(
        'compute_v3',
        jsonb_build_object(
          'action', 'fomc_calendar_refresh',
          'args',   '{}'::jsonb
        )
      );
    $cron$
  );

  -- fda-event-harvest-daily ------------------------------------------------
  SELECT jobid INTO v_existing_jobid
    FROM cron.job WHERE jobname = 'fda-event-harvest-daily';
  IF v_existing_jobid IS NOT NULL THEN
    PERFORM cron.unschedule(v_existing_jobid);
  END IF;

  PERFORM cron.schedule(
    'fda-event-harvest-daily',
    '0 4 * * *',
    $cron$
      SELECT public._conan_modal_post_enqueue(
        'compute_v3',
        jsonb_build_object(
          'action', 'fda_event_harvest_daily',
          'args',   '{}'::jsonb
        )
      );
    $cron$
  );
END
$$;
