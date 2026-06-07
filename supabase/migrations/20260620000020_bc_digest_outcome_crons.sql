-- =============================================================================
-- 20260620000020_bc_digest_outcome_crons.sql  — Phase-3 cron wiring (APPLY LAST)
--
-- Conan-ledger port of bc-fda/db/migrations/010_bc_digest_outcome_crons.sql, with
-- the secret-source ADAPTED to conan's convention.
--
-- ⚠️ ADAPTATION vs the bc-fda source (the must-fix): bc-fda's 010 read the bearer
-- from vault.decrypted_secrets (service_role_key / compute_secret). Conan's vault
-- holds NEITHER — every conan cron reads internal_config.compute_secret (see
-- bc-class-precedent-refresh-daily, enrich-fda-asset-designations-daily). Both jobs
-- below therefore read internal_config.compute_secret and send it as the bearer.
--   * bc-digest-daily        @15 UTC -> internal_config.bc_digest_function_url
--                                       (edge fn gates on x-service-key == the
--                                        digest's BC_DIGEST_TRIGGER_KEY, set = compute_secret).
--   * bc-outcome-labeler-daily @22 UTC -> internal_config.modal_url_bc_outcome_labeler
--                                       (Modal endpoint gates on x-conan-compute-secret).
-- Both EXIT CLEAN when their target URL row is empty (pre-deploy-ordering idiom), so
-- this migration is safe to apply before the endpoints are deployed — but APPLY IT
-- ONLY ONCE the digest fn URL is set, so the daily chain doesn't no-op silently.
--
-- Kill switches (the strangle off-ramp):
--   UPDATE cron.job SET active=false WHERE jobname IN ('bc-digest-daily','bc-outcome-labeler-daily');
--
-- IDEMPOTENT: CREATE EXTENSION IF NOT EXISTS; each job is unschedule-if-exists then
-- schedule. Re-run replaces the job definitions cleanly.
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS pg_cron WITH SCHEMA extensions CASCADE;

-- ── bc-digest-daily @ 15:00 UTC ───────────────────────────────────────────────
-- >= 1h after the daily monitor so today's bc_thesis_updates exist. Reads the
-- edge-fn URL from internal_config; exits clean if empty. Sends compute_secret in
-- the x-service-key header (matches the digest fn's BC_DIGEST_TRIGGER_KEY gate).
DO $$
DECLARE
  v_existing_jobid bigint;
BEGIN
  SELECT jobid INTO v_existing_jobid FROM cron.job WHERE jobname = 'bc-digest-daily';
  IF v_existing_jobid IS NOT NULL THEN
    PERFORM cron.unschedule(v_existing_jobid);
  END IF;

  PERFORM cron.schedule(
    'bc-digest-daily',
    '0 15 * * *',
    $cron$
      DO $job$
      DECLARE
        v_url    text;
        v_secret text;
      BEGIN
        SELECT value INTO v_url
          FROM public.internal_config
          WHERE key = 'bc_digest_function_url';

        IF v_url IS NULL OR v_url = '' THEN
          RAISE NOTICE 'bc-digest-daily: function URL not configured; skipping';
          RETURN;
        END IF;

        SELECT value INTO v_secret
          FROM public.internal_config
          WHERE key = 'compute_secret';

        PERFORM net.http_post(
          url := v_url,
          body := jsonb_build_object('source', 'pg_cron'),
          headers := jsonb_build_object(
            'Content-Type', 'application/json',
            'x-service-key', COALESCE(v_secret, '')
          ),
          timeout_milliseconds := 60000
        );
      END
      $job$;
    $cron$
  );
END
$$;

-- ── bc-outcome-labeler-daily @ 22:00 UTC ──────────────────────────────────────
-- After US close so the t+1 bar exists for any same-day-resolved PDUFA. Reads the
-- Modal endpoint URL from internal_config.modal_url_bc_outcome_labeler; sends
-- compute_secret as the compute bearer. Exits clean if empty. Independent of the
-- digest's success.
DO $$
DECLARE
  v_existing_jobid bigint;
BEGIN
  SELECT jobid INTO v_existing_jobid FROM cron.job WHERE jobname = 'bc-outcome-labeler-daily';
  IF v_existing_jobid IS NOT NULL THEN
    PERFORM cron.unschedule(v_existing_jobid);
  END IF;

  PERFORM cron.schedule(
    'bc-outcome-labeler-daily',
    '0 22 * * *',
    $cron$
      DO $job$
      DECLARE
        v_url    text;
        v_secret text;
      BEGIN
        SELECT value INTO v_url
          FROM public.internal_config
          WHERE key = 'modal_url_bc_outcome_labeler';

        IF v_url IS NULL OR v_url = '' THEN
          RAISE NOTICE 'bc-outcome-labeler-daily: modal URL not configured; skipping';
          RETURN;
        END IF;

        SELECT value INTO v_secret
          FROM public.internal_config
          WHERE key = 'compute_secret';

        PERFORM net.http_post(
          url := v_url,
          body := jsonb_build_object('source', 'pg_cron'),
          headers := jsonb_build_object(
            'Content-Type', 'application/json',
            'x-conan-compute-secret', COALESCE(v_secret, ''),
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
