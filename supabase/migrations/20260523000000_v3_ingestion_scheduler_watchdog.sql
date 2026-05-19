-- v3 ingestion scheduler watchdog.
--
-- Production was observed with the two front-door ingestion jobs disabled in
-- cron.job (`v3-asset-linker-pass1 active=false`, `v3-fact-extractor
-- active=false`). The repo contains migrations that create these schedules, but
-- no intentional kill switch or watchdog that keeps them enabled afterward.
--
-- This migration is repo-level only: it does not directly mutate production
-- outside the migration path. When applied, it:
--   1. Re-enables the critical v3 ingestion cron jobs if they exist but are
--      inactive.
--   2. Recreates any missing critical schedule using the canonical command body
--      from 20260511112403_v3_asset_linker_pg_cron.sql.
--   3. Schedules a lightweight watchdog that repeats the same check every
--      10 minutes and writes/clears a single operator_flags row so disabled
--      ingestion cannot remain silent.

CREATE EXTENSION IF NOT EXISTS pg_cron WITH SCHEMA extensions CASCADE;

CREATE OR REPLACE FUNCTION public.v3_ingestion_scheduler_watchdog()
RETURNS jsonb
LANGUAGE plpgsql
SET search_path = public, extensions
AS $$
DECLARE
  v_expected text[] := ARRAY[
    'v3-asset-linker-pass1',
    'v3-asset-linker-pass2',
    'v3-fact-extractor'
  ];
  v_disabled text[];
  v_missing text[];
  v_existing_flag uuid;
  v_now timestamptz := now();
  v_jobid bigint;
BEGIN
  SELECT COALESCE(array_agg(jobname ORDER BY jobname), ARRAY[]::text[])
    INTO v_disabled
  FROM cron.job
  WHERE jobname = ANY (v_expected)
    AND COALESCE(active, false) = false;

  SELECT COALESCE(array_agg(expected.jobname ORDER BY expected.jobname), ARRAY[]::text[])
    INTO v_missing
  FROM unnest(v_expected) AS expected(jobname)
  LEFT JOIN cron.job AS j ON j.jobname = expected.jobname
  WHERE j.jobid IS NULL;

  FOR v_jobid IN
    SELECT jobid
    FROM cron.job
    WHERE jobname = ANY (v_expected)
      AND COALESCE(active, false) = false
  LOOP
    PERFORM cron.alter_job(v_jobid, active := true);
  END LOOP;

  IF 'v3-asset-linker-pass1' = ANY (v_missing) THEN
    PERFORM cron.schedule(
      'v3-asset-linker-pass1',
      '*/15 * * * *',
      $cron$
        SELECT public._conan_modal_post_enqueue(
          'compute_v3',
          jsonb_build_object(
            'action', 'asset_linker_run',
            'args',   '{}'::jsonb
          )
        );
      $cron$
    );
  END IF;

  IF 'v3-asset-linker-pass2' = ANY (v_missing) THEN
    PERFORM cron.schedule(
      'v3-asset-linker-pass2',
      '10,40 * * * *',
      $cron$
        SELECT public._conan_modal_post_enqueue(
          'compute_v3',
          jsonb_build_object(
            'action', 'asset_linker_pass2_run',
            'args',   '{}'::jsonb
          )
        );
      $cron$
    );
  END IF;

  IF 'v3-fact-extractor' = ANY (v_missing) THEN
    PERFORM cron.schedule(
      'v3-fact-extractor',
      '20 * * * *',
      $cron$
        SELECT public._conan_modal_post_enqueue(
          'compute_v3',
          jsonb_build_object(
            'action', 'fact_extractor_run',
            'args',   '{}'::jsonb
          )
        );
      $cron$
    );
  END IF;

  SELECT id INTO v_existing_flag
  FROM public.operator_flags
  WHERE source = 'v3_pipeline_watchdog'
    AND kind = 'v3_ingestion_cron_repaired'
    AND resolved_at IS NULL
  LIMIT 1;

  IF cardinality(v_disabled) > 0 OR cardinality(v_missing) > 0 THEN
    IF v_existing_flag IS NULL THEN
      INSERT INTO public.operator_flags (
        severity,
        source,
        kind,
        title,
        body,
        evidence
      )
      VALUES (
        'warn',
        'v3_pipeline_watchdog',
        'v3_ingestion_cron_repaired',
        'v3 ingestion cron jobs were repaired',
        'A watchdog found disabled or missing v3 ingestion cron jobs and restored the repo-declared schedules.',
        jsonb_build_object(
          'disabled_jobs', to_jsonb(v_disabled),
          'missing_jobs', to_jsonb(v_missing),
          'repaired_at', v_now
        )
      );
    ELSE
      UPDATE public.operator_flags
         SET title = 'v3 ingestion cron jobs were repaired',
             body = 'A watchdog found disabled or missing v3 ingestion cron jobs and restored the repo-declared schedules.',
             severity = 'warn',
             evidence = jsonb_build_object(
               'disabled_jobs', to_jsonb(v_disabled),
               'missing_jobs', to_jsonb(v_missing),
               'repaired_at', v_now
             )
       WHERE id = v_existing_flag;
    END IF;
  ELSE
    UPDATE public.operator_flags
       SET resolved_at = v_now,
           resolved_note = 'v3 ingestion cron jobs are present and active.'
     WHERE source = 'v3_pipeline_watchdog'
       AND kind = 'v3_ingestion_cron_repaired'
       AND resolved_at IS NULL;
  END IF;

  RETURN jsonb_build_object(
    'disabled_jobs_reenabled', v_disabled,
    'missing_jobs_recreated', v_missing,
    'checked_at', v_now
  );
END;
$$;

REVOKE ALL ON FUNCTION public.v3_ingestion_scheduler_watchdog() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.v3_ingestion_scheduler_watchdog() FROM anon;
REVOKE ALL ON FUNCTION public.v3_ingestion_scheduler_watchdog() FROM authenticated;

COMMENT ON FUNCTION public.v3_ingestion_scheduler_watchdog() IS
  'Repairs disabled/missing v3 ingestion pg_cron jobs and writes an operator flag when recovery was needed.';

-- Run once during migration application to repair the observed active=false
-- state, then schedule ongoing protection against silent disables.
SELECT public.v3_ingestion_scheduler_watchdog();

DO $$
DECLARE
  v_existing_jobid bigint;
BEGIN
  SELECT jobid INTO v_existing_jobid
  FROM cron.job
  WHERE jobname = 'v3-ingestion-scheduler-watchdog';

  IF v_existing_jobid IS NOT NULL THEN
    PERFORM cron.unschedule(v_existing_jobid);
  END IF;

  PERFORM cron.schedule(
    'v3-ingestion-scheduler-watchdog',
    '*/10 * * * *',
    $cron$
      SELECT public.v3_ingestion_scheduler_watchdog();
    $cron$
  );
END
$$;
