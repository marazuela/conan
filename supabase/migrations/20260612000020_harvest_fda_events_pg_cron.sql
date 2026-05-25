-- =============================================================================
-- WI-7 — pg_cron: daily FDA event harvest
--
-- Fires at 04:00 UTC (before Stage A 05:55 UTC and the calendar refreshers
-- at 06:10 / 06:15). The Modal endpoint URL lives in
-- internal_config.modal_url_fda_event_harvest_daily and is empty by default;
-- the cron body short-circuits when unconfigured.
--
-- Once Modal hosts harvest_fda_events.py at that URL, the daily POST advances
-- the openfda checkpoint by one day and upserts new event rows.
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS pg_cron WITH SCHEMA extensions CASCADE;

INSERT INTO public.internal_config (key, value, updated_at)
VALUES ('modal_url_fda_event_harvest_daily', '', now())
ON CONFLICT (key) DO NOTHING;

DO $$
DECLARE
  v_existing_jobid bigint;
BEGIN
  SELECT jobid INTO v_existing_jobid
    FROM cron.job WHERE jobname = 'fda-event-harvest-daily';

  IF v_existing_jobid IS NOT NULL THEN
    PERFORM cron.unschedule(v_existing_jobid);
  END IF;

  PERFORM cron.schedule(
    'fda-event-harvest-daily',
    '0 4 * * *',
    $cron$
      DO $job$
      DECLARE
        v_url text;
        v_secret text;
      BEGIN
        SELECT value INTO v_url
          FROM public.internal_config
          WHERE key = 'modal_url_fda_event_harvest_daily';

        IF v_url IS NULL OR v_url = '' THEN
          RAISE NOTICE 'fda-event-harvest-daily: modal URL not configured; skipping';
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
          timeout_milliseconds := 120000
        );
      END
      $job$;
    $cron$
  );
END
$$;
