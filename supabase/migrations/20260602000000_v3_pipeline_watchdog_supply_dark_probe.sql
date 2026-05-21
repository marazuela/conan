-- v3 pipeline watchdog: primary+material asset_documents supply-dark probe.
--
-- Today's 2026-05-19→2026-05-20 outage looked like a reactor enqueue bug —
-- "11 asset_documents/24h yielded 0 new_doc orchestrator_runs" — but the
-- reactor was correctly idle. The asset_documents trigger only fires when
-- link_type='primary' AND is_material=true (see migration
-- 20260507100000_v3_alert_triggers.sql), and the upstream asset-linker
-- (now a Cowork skill since the cutover in 20260601000000) had emitted
-- zero rows matching that predicate for 31h. Nothing in the existing
-- watchdog catches this — _v3_pipeline_watchdog probes the linker queue
-- depth and burn rate, but not the *output* arriving on the reactor side.
--
-- This probe watches the trigger predicate directly. If it fires, the
-- on-call playbook is: check the Cowork skill session (asset_linker is
-- running? backlog drained?), not the reactor.

CREATE OR REPLACE FUNCTION public._v3_pipeline_watchdog_supply_dark()
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'public'
AS $function$
DECLARE
  v_n integer;
  v_last_seen timestamptz;
BEGIN
  SELECT count(*), max(created_at)
    INTO v_n, v_last_seen
    FROM public.asset_documents
   WHERE created_at > now() - interval '24 hours'
     AND link_type = 'primary'
     AND is_material = true;

  IF v_n = 0 THEN
    INSERT INTO public.operator_flags (
      severity, source, kind, title, body, evidence
    )
    VALUES (
      'warn',
      'v3_pipeline_watchdog',
      'asset_documents_primary_dark_24h',
      'No primary+material asset_documents in 24h',
      'Zero asset_documents (link_type=primary AND is_material=true) in the last 24h. '
      'Reactor enqueue path is correctly idle but Tier-1 throughput beyond the '
      'catalyst-proximity sweep is blocked. Usually means the asset-linker Cowork '
      'skill has stopped emitting (operator session offline, halted, or queue empty).',
      jsonb_build_object(
        'count', v_n,
        'window_hours', 24,
        'last_primary_material_at', v_last_seen
      )
    )
    ON CONFLICT DO NOTHING;
  ELSE
    UPDATE public.operator_flags
       SET resolved_at = now(),
           resolved_note = 'auto-resolved by _v3_pipeline_watchdog_supply_dark: at least one primary+material asset_document in last 24h',
           updated_at = now()
     WHERE source = 'v3_pipeline_watchdog'
       AND kind = 'asset_documents_primary_dark_24h'
       AND resolved_at IS NULL;
  END IF;

  RETURN jsonb_build_object(
    'asset_documents_primary_dark_24h',
    jsonb_build_object('count', v_n, 'last_primary_material_at', v_last_seen)
  );
END;
$function$;

COMMENT ON FUNCTION public._v3_pipeline_watchdog_supply_dark() IS
  'Hourly probe (cron v3-pipeline-watchdog-supply-dark @ :07). Warns when no '
  'primary+material asset_documents in 24h — the upstream-supply failure mode '
  'that masquerades as a reactor enqueue bug. Same source/kind contract as '
  '_v3_pipeline_watchdog so the dashboard treats it uniformly.';

DO $$
DECLARE
  v_existing_jobid bigint;
BEGIN
  SELECT jobid INTO v_existing_jobid
    FROM cron.job
   WHERE jobname = 'v3-pipeline-watchdog-supply-dark';
  IF v_existing_jobid IS NOT NULL THEN
    PERFORM cron.unschedule(v_existing_jobid);
  END IF;
  PERFORM cron.schedule(
    'v3-pipeline-watchdog-supply-dark',
    '7 * * * *',
    'select public._v3_pipeline_watchdog_supply_dark();'
  );
END $$;
