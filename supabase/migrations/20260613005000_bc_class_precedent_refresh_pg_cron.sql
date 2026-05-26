-- =============================================================================
-- D-129 WI-2 follow-up — nightly pg_cron: bc_class_precedent_refresh
--
-- Fires at 06:20 UTC daily. Reads fda_regulatory_events, aggregates approval
-- vs CRL/withdrawal counts per (moa_canonical, indication), and upserts into
-- fda_class_precedent_base_rates. Reactor's bc-pregate reads this table at
-- gate time (see supabase/functions/reactor/bc-pregate.ts).
--
-- Same empty-URL guard pattern as 20260605000060_fomc_calendar_pg_cron.sql:
-- migration is safe to apply before the Modal endpoint URL is filled in
-- (cron job is a no-op until `modal_url_bc_class_precedent_refresher` has
-- a non-empty value in public.internal_config). Operator sets the URL
-- when the worker is deployed.
--
-- Cadence: daily. Class peers don't shift much day-to-day, but daily
-- refresh keeps the base rates aligned with newly-harvested approval/CRL
-- events without forcing a manual flush after each ingest run.
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS pg_cron WITH SCHEMA extensions CASCADE;

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

        SELECT decrypted_secret INTO v_secret
          FROM vault.decrypted_secrets
          WHERE name = 'compute_secret'
          LIMIT 1;

        -- Body + header shape mirrors 20260612000020_harvest_fda_events_pg_cron.sql
        -- and 20260605000060_fomc_calendar_pg_cron.sql so the operator's
        -- modal_url_* workflow is uniform across Phase 3a/3b/4 + this follow-up.
        PERFORM net.http_post(
          url := v_url,
          body := '{}'::jsonb,
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
