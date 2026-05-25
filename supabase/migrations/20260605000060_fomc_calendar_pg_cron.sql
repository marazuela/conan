-- =============================================================================
-- Phase 3a — pg_cron: monthly fomc_calendar refresh
--
-- Fires at 06:15 UTC on the 1st of each month. FOMC scheduled meetings happen
-- 8 times/year; minutes ~3 weeks after each. Monthly is sufficient to catch
-- emergency rate actions and the annual schedule release.
--
-- Modal endpoint scrapes federalreserve.gov/monetarypolicy/fomccalendars.htm
-- and upserts into fomc_calendar. See the earnings-calendar comments for
-- the empty-URL guard pattern.
--
-- See plan: /Users/Pico/.claude/plans/plan-it-thoroughly-unified-scroll.md
-- (Phase 3a)
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS pg_cron WITH SCHEMA extensions CASCADE;

DO $$
DECLARE
  v_existing_jobid bigint;
BEGIN
  SELECT jobid INTO v_existing_jobid
    FROM cron.job
    WHERE jobname = 'fomc-calendar-monthly';

  IF v_existing_jobid IS NOT NULL THEN
    PERFORM cron.unschedule(v_existing_jobid);
  END IF;

  PERFORM cron.schedule(
    'fomc-calendar-monthly',
    '15 6 1 * *',
    $cron$
      DO $job$
      DECLARE
        v_url text;
        v_secret text;
      BEGIN
        SELECT value INTO v_url
          FROM public.internal_config
          WHERE key = 'modal_url_fomc_calendar_refresh';

        IF v_url IS NULL OR v_url = '' THEN
          RAISE NOTICE 'fomc-calendar-monthly: modal URL not configured; skipping';
          RETURN;
        END IF;

        SELECT decrypted_secret INTO v_secret
          FROM vault.decrypted_secrets
          WHERE name = 'compute_secret'
          LIMIT 1;

        PERFORM net.http_post(
          url := v_url,
          body := '{}'::jsonb,
          headers := jsonb_build_object(
            'Content-Type', 'application/json',
            'Authorization', 'Bearer ' || COALESCE(v_secret, '')
          ),
          timeout_milliseconds := 30000
        );
      END
      $job$;
    $cron$
  );
END
$$;
