-- Retire the orphaned doc/asset edge-prefilter "cutover".
--
-- The 20260601000000 skill cutover added doc_asset_candidates + the
-- v_asset_linker_skill_queue view + the `v3-doc-asset-prefilter` cron (every 2 min,
-- fn_generate_doc_asset_candidates(2000)), intending the local asset-linker skill to
-- drain that pre-matched "edge queue" and stamp doc_asset_candidates.analyzed_at /
-- write document_asset_linker_attempts. That consumer was never built: the live
-- asset-linker-opus skill instead drains the documents.linker_classified_* MARKER
-- queue. So the edge queue accumulated 5,802 rows with **0 analyzed_at stamps in its
-- entire life** and only ever served to drive a false-positive _v3_pipeline_watchdog
-- 'asset_linker_skill_queue_backlog' flag (fires when the view count exceeds 500).
--
-- Retire it: (a) drop the producer from the self-healing scheduler watchdog so it is
-- not re-created, (b) unschedule the producer cron, (c) drain the orphaned rows so the
-- backlog flag auto-resolves, (d) resolve the currently-open flag. Idempotent.
-- Reversible: re-add 'v3-doc-asset-prefilter' to v_expected (+ its re-create branch)
-- and re-schedule the cron to bring the cutover back. v3-asset-alias-weekly-refresh is
-- intentionally KEPT — the marker-skill's asset_set_hash depends on the alias set.
--
-- NOTE: applied live via MCP on 2026-06-05; this file keeps disk == live for rebuilds.

-- (a) Scheduler watchdog no longer protects/re-creates the prefilter.
CREATE OR REPLACE FUNCTION public.v3_ingestion_scheduler_watchdog()
 RETURNS jsonb
 LANGUAGE plpgsql
 SET search_path TO 'public', 'extensions'
AS $function$
DECLARE
  v_expected text[] := ARRAY[
    'v3-asset-alias-weekly-refresh'
  ];
  v_disabled text[];
  v_missing text[];
  v_existing_flag uuid;
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
    SELECT jobid FROM cron.job
    WHERE jobname = ANY (v_expected) AND COALESCE(active, false) = false
  LOOP
    PERFORM cron.alter_job(v_jobid, active := true);
  END LOOP;

  IF 'v3-asset-alias-weekly-refresh' = ANY (v_missing) THEN
    PERFORM cron.schedule(
      'v3-asset-alias-weekly-refresh',
      '0 3 * * 1',
      $cron$
        SELECT public._conan_modal_post_enqueue(
          'compute_v3',
          jsonb_build_object('action', 'seed_fda_asset_aliases_refresh', 'args', '{}'::jsonb)
        );
      $cron$
    );
  END IF;

  SELECT id INTO v_existing_flag
  FROM public.operator_flags
  WHERE source = 'v3_pipeline_watchdog' AND kind = 'v3_ingestion_cron_repaired' AND resolved_at IS NULL
  ORDER BY created_at DESC LIMIT 1;

  IF array_length(v_disabled, 1) IS NOT NULL OR array_length(v_missing, 1) IS NOT NULL THEN
    IF v_existing_flag IS NULL THEN
      INSERT INTO public.operator_flags (severity, source, kind, title, body, evidence)
      VALUES ('info','v3_pipeline_watchdog','v3_ingestion_cron_repaired','v3 ingestion cron repaired',
        'Scheduler watchdog repaired the v3 asset-alias weekly refresh cron. LLM asset-linking/fact-extraction are intentionally disabled; the local asset-linker skill drains the documents.linker_classified_* marker queue (the doc/asset edge prefilter was retired 2026-06-05).',
        jsonb_build_object('disabled_jobs', v_disabled, 'missing_jobs', v_missing, 'protected_jobs', v_expected, 'asset_linker_mode', 'cursor_skill_marker_queue'));
    END IF;
  ELSIF v_existing_flag IS NOT NULL THEN
    UPDATE public.operator_flags
       SET resolved_at = now(),
           resolved_note = 'v3 ingestion crons present and active; LLM asset-linker and fact-extractor crons intentionally disabled for skill workflows.'
     WHERE id = v_existing_flag;
  END IF;

  RETURN jsonb_build_object('disabled_jobs', v_disabled, 'missing_jobs', v_missing, 'protected_jobs', v_expected, 'asset_linker_mode', 'cursor_skill_marker_queue');
END;
$function$;

-- (b) Stop the producer cron (guarded so re-runs are no-ops).
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM cron.job WHERE jobname = 'v3-doc-asset-prefilter') THEN
    PERFORM cron.unschedule('v3-doc-asset-prefilter');
  END IF;
END $$;

-- (c) Drain the orphaned edge queue so v_asset_linker_skill_queue <= 500 -> flag auto-resolves.
UPDATE public.doc_asset_candidates SET analyzed_at = now() WHERE analyzed_at IS NULL;

-- (d) Resolve the currently-open backlog flag.
UPDATE public.operator_flags
   SET resolved_at = now(),
       resolved_note = 'Orphaned doc/asset edge-prefilter cutover retired 2026-06-05; the queue never had a consumer (0 analyzed_at ever). Live linking uses the documents.linker_classified_* marker queue.',
       updated_at = now()
 WHERE source = 'v3_pipeline_watchdog' AND kind = 'asset_linker_skill_queue_backlog' AND resolved_at IS NULL;
