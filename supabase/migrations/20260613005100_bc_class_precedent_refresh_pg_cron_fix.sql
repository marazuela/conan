-- =============================================================================
-- D-131 follow-up — fix bc-class-precedent-refresh-daily pg_cron body + secret
--
-- The original 20260613005000 migration matched the broken pattern used by
-- 20260612000020_harvest_fda_events_pg_cron.sql and
-- 20260605000060_fomc_calendar_pg_cron.sql:
--
--   1. Reads `compute_secret` from `vault.decrypted_secrets` (returns NULL on
--      this project — the secret actually lives in
--      `public.internal_config.compute_secret` per the compute_auth_setup
--      memory entry dated 2026-04-22).
--   2. POSTs an empty `{}` body with `Authorization: Bearer …`. The
--      compute_v3_dispatch endpoint expects `{action, args}` + a
--      `x-conan-compute-secret` header, so the request hits a 401.
--
-- Smoke-tested 2026-05-26: switching to internal_config + the proper body shape
-- returns 200 + `{"spawned": true, "function_call_id": "…"}`. The other Phase
-- 3a/3b crons reading from vault remain broken — separate follow-up.
-- =============================================================================

DO $$
DECLARE
  v_existing_jobid bigint;
BEGIN
  SELECT jobid INTO v_existing_jobid
    FROM cron.job
    WHERE jobname = 'bc-class-precedent-refresh-daily';

  IF v_existing_jobid IS NOT NULL THEN
    PERFORM cron.unschedule(v_existing_jobid);
  END IF;

  PERFORM cron.schedule(
    'bc-class-precedent-refresh-daily',
    '20 6 * * *',
    $cron$
      DO $job$
      DECLARE
        v_url text;
        v_secret text;
      BEGIN
        SELECT value INTO v_url
          FROM public.internal_config
          WHERE key = 'modal_url_bc_class_precedent_refresher';

        IF v_url IS NULL OR v_url = '' THEN
          RAISE NOTICE 'bc-class-precedent-refresh-daily: modal URL not configured; skipping';
          RETURN;
        END IF;

        SELECT value INTO v_secret
          FROM public.internal_config
          WHERE key = 'compute_secret';

        PERFORM net.http_post(
          url := v_url,
          body := jsonb_build_object(
            'action', 'bc_class_precedent_refresh',
            'args',   jsonb_build_object('apply', true)
          ),
          headers := jsonb_build_object(
            'Content-Type', 'application/json',
            'x-conan-compute-secret', COALESCE(v_secret, '')
          ),
          timeout_milliseconds := 60000
        );
      END
      $job$;
    $cron$
  );
END
$$;
