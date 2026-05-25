-- =============================================================================
-- Phase 3a — pg_cron: daily earnings_calendar refresh
--
-- Fires at 06:10 UTC (after Stage A aging 05:55 UTC, before the Cowork-side
-- skills land 06:00 UTC). Posts to the Modal endpoint named by
-- internal_config.modal_url_earnings_calendar_fetch_daily — when the row
-- is empty (initial state pre-Modal-deploy) the job exits cleanly with a
-- NOTICE so unscheduled days don't pile up cron errors.
--
-- The Modal endpoint pulls the union of tradeable tickers from eval_harness
-- + fda_assets, runs the yfinance/Polygon fetcher (modal_workers/fetchers/
-- universe/earnings_calendar.py) over a today-7d..today+90d window, and
-- upserts into earnings_calendar.
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
    WHERE jobname = 'earnings-calendar-daily';

  IF v_existing_jobid IS NOT NULL THEN
    PERFORM cron.unschedule(v_existing_jobid);
  END IF;

  PERFORM cron.schedule(
    'earnings-calendar-daily',
    '10 6 * * *',
    $cron$
      DO $job$
      DECLARE
        v_url text;
        v_secret text;
      BEGIN
        SELECT value INTO v_url
          FROM public.internal_config
          WHERE key = 'modal_url_earnings_calendar_fetch_daily';

        IF v_url IS NULL OR v_url = '' THEN
          RAISE NOTICE 'earnings-calendar-daily: modal URL not configured; skipping';
          RETURN;
        END IF;

        SELECT decrypted_secret INTO v_secret
          FROM vault.decrypted_secrets
          WHERE name = 'compute_secret'
          LIMIT 1;

        PERFORM net.http_post(
          url := v_url,
          body := jsonb_build_object('window_days', 7, 'forward_days', 90),
          headers := jsonb_build_object(
            'Content-Type', 'application/json',
            'Authorization', 'Bearer ' || COALESCE(v_secret, '')
          ),
          timeout_milliseconds := 60000
        );
      END
      $job$;
    $cron$
  );
END
$$;

COMMENT ON EXTENSION pg_cron IS
  'pg_cron schedules the Modal earnings_calendar refresher daily at 06:10 UTC. Job name: earnings-calendar-daily. Reads URL from internal_config.modal_url_earnings_calendar_fetch_daily; exits cleanly if unconfigured.';
