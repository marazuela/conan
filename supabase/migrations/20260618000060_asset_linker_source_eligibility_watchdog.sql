-- Watchdog: flag active fda_assets whose program_status is NOT covered by
-- asset_linker_source_eligibility. Issue #54 acceptance criterion.
--
-- The eligibility table (20260618000050) makes source-routing a data decision,
-- but introduces a new silent-failure mode: if an asset enters a program_status
-- the rule table does not know about, that status contributes NOTHING to the
-- resolved eligible-sources set, so the linker may quietly stop covering its
-- sources. load_eligible_sources() falls back to clinicaltrials when the whole
-- set resolves empty, so the linker never goes fully dark — but a NEW status
-- that is simply *narrower* than reality (e.g. an 'approved' asset whose status
-- string is mis-cased) would silently lose dailymed/openfda coverage. This
-- watchdog surfaces the coverage gap so an operator adds the missing rows.
--
-- NULL/'' program_status is NOT an orphan: it folds onto the seeded '_unset'
-- sentinel (same COALESCE the eligible-sources view uses), so this only fires
-- on genuinely-unknown taxonomy values.
--
-- Reuses operator_flags.source='v3_pipeline_watchdog' (already allow-listed) so
-- no source-CHECK surgery is needed; disambiguated by
-- kind='asset_linker_source_eligibility_orphan'. Standalone function (not folded
-- into _v3_pipeline_watchdog) to keep blast radius off that 9-check function.

CREATE OR REPLACE FUNCTION public._asset_linker_source_eligibility_watchdog()
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
DECLARE
  v_n      integer;
  v_sample jsonb;
BEGIN
  SELECT count(*),
         COALESCE(jsonb_agg(jsonb_build_object(
           'program_status', status,
           'active_asset_count', cnt
         ) ORDER BY cnt DESC), '[]'::jsonb)
    INTO v_n, v_sample
    FROM (
      SELECT COALESCE(NULLIF(a.program_status, ''), '_unset') AS status,
             count(*) AS cnt
        FROM public.fda_assets a
       WHERE a.is_active = true
         AND NOT EXISTS (
           SELECT 1
             FROM public.asset_linker_source_eligibility e
            WHERE e.program_status = COALESCE(NULLIF(a.program_status, ''), '_unset')
         )
       GROUP BY 1
    ) s;

  IF v_n > 0 THEN
    INSERT INTO public.operator_flags (severity, source, kind, title, body, evidence)
    VALUES (
      'warn',
      'v3_pipeline_watchdog',
      'asset_linker_source_eligibility_orphan',
      'Active fda_assets have a program_status with no source-eligibility rule',
      v_n || ' distinct active program_status value(s) have no row in '
        'asset_linker_source_eligibility. Documents for assets in those statuses '
        'contribute nothing to the resolved eligible-sources set, so asset_linker '
        'pass-1 may skip their sources. Add the missing (program_status, source) '
        'rows to public.asset_linker_source_eligibility.',
      jsonb_build_object('orphan_status_count', v_n, 'orphans', v_sample)
    )
    ON CONFLICT DO NOTHING;
  ELSE
    UPDATE public.operator_flags
       SET resolved_at = now(),
           resolved_note = 'auto-resolved by _asset_linker_source_eligibility_watchdog: '
                           'all active program_status values are covered',
           updated_at = now()
     WHERE source = 'v3_pipeline_watchdog'
       AND kind = 'asset_linker_source_eligibility_orphan'
       AND resolved_at IS NULL;
  END IF;

  RETURN jsonb_build_object(
    'asset_linker_source_eligibility_orphans', v_n,
    'sample', v_sample
  );
END;
$function$;

COMMENT ON FUNCTION public._asset_linker_source_eligibility_watchdog() IS
  'Daily check (cron asset-linker-source-eligibility-watchdog @ 13:45 UTC): '
  'warns when active fda_assets carry a program_status not covered by '
  'asset_linker_source_eligibility. NULL/empty folds onto the ''_unset'' '
  'sentinel and is not flagged. Issue #54.';

-- Idempotent re-apply: unschedule existing job (by name) before re-scheduling.
DO $cron$
DECLARE
  v_existing_jobid bigint;
BEGIN
  SELECT jobid INTO v_existing_jobid
    FROM cron.job
   WHERE jobname = 'asset-linker-source-eligibility-watchdog';

  IF v_existing_jobid IS NOT NULL THEN
    PERFORM cron.unschedule(v_existing_jobid);
  END IF;

  PERFORM cron.schedule(
    'asset-linker-source-eligibility-watchdog',
    '45 13 * * *',
    'SELECT public._asset_linker_source_eligibility_watchdog();'
  );
END
$cron$;
