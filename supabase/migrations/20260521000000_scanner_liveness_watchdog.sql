-- 2026-05-11 — scanner_liveness watchdog
--
-- Closes the silent-skip gap that hid the pre_phase3_readout_scanner outage
-- (2026-04-27 → 2026-05-11). dispatch_weekly fired on 2026-05-04 12:00 UTC
-- but pre_phase3 was not in the spawn list — no scanner_runs row written, no
-- error logged. Operators only noticed two weeks later when a downstream
-- signal-count question surfaced.
--
-- Detection model: each operational scanner has a cadence-implied freshness
-- window. If NOW() - scanners.last_run_utc exceeds the window, write an
-- operator_flags row (one per scanner, dedup'd via operator_flags_open_uniq).
--
--   cadence='3h'     → 9h window  (3x cadence, allows one missed tick + slack)
--   cadence='daily'  → 36h window (1.5x cadence)
--   cadence='weekly' → 8d window  (1 day past cadence)
--
-- The watchdog uses source='scanner_liveness' (already in the
-- operator_flags_source_check allowlist on live; constraint extension was
-- applied out-of-band — see operator_flags_source_check live state).
--
-- Verification: temporarily mark a weekly scanner as 'paused' (or stale-set
-- last_run_utc), trigger the watchdog, expect an operator_flags row with
-- kind='cadence_overdue'.

CREATE OR REPLACE FUNCTION public._scanner_liveness_watchdog()
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'public'
AS $function$
DECLARE
  v_results jsonb := '{}'::jsonb;
  v_row record;
  v_age_hours numeric;
  v_threshold interval;
  v_inserted_n int;
BEGIN
  FOR v_row IN
    SELECT id, name, cadence, last_run_utc
      FROM public.scanners
     WHERE status = 'operational'
       AND cadence IN ('3h','daily','weekly')
       -- Catalyst-universe fetchers (modal_workers/fetchers/universe/*) write
       -- directly to catalyst_universe and never touch scanner_runs / last_run_utc.
       -- They're registered for dispatch routing only; liveness for them must
       -- be measured against catalyst_universe row freshness, not this signal.
       AND (tool_path IS NULL OR tool_path NOT LIKE 'modal_workers/fetchers/universe/%')
       AND (NOW() - COALESCE(last_run_utc, '1970-01-01'::timestamptz)) >
           CASE cadence
             WHEN '3h'     THEN INTERVAL '9 hours'
             WHEN 'daily'  THEN INTERVAL '36 hours'
             WHEN 'weekly' THEN INTERVAL '8 days'
           END
  LOOP
    v_threshold := CASE v_row.cadence
                     WHEN '3h'     THEN INTERVAL '9 hours'
                     WHEN 'daily'  THEN INTERVAL '36 hours'
                     WHEN 'weekly' THEN INTERVAL '8 days'
                   END;
    v_age_hours := round(
      extract(epoch FROM (NOW() - COALESCE(v_row.last_run_utc, '1970-01-01'::timestamptz)))/3600.0,
      1
    );

    INSERT INTO public.operator_flags
      (severity, source, kind, scanner_id, title, body, evidence)
    VALUES
      ('warn',
       'scanner_liveness',
       'cadence_overdue',
       v_row.id,
       format('Scanner %s overdue: cadence=%s, last run %s h ago',
              v_row.name, v_row.cadence,
              COALESCE(v_age_hours::text, 'never')),
       'Scanner has not written a scanner_runs row within its cadence-implied '
         'freshness window (3h:9h, daily:36h, weekly:8d). Most likely causes: '
         'dispatch cron silently skipped it (registry lookup returned a list '
         'that excluded this scanner), scanners.status flipped mid-cycle, or '
         'the worker is crashing before close_scanner_run. Cross-check '
         'scanner_runs entries vs scanners.last_run_utc to disambiguate.',
       jsonb_build_object(
         'scanner',       v_row.name,
         'cadence',       v_row.cadence,
         'last_run_utc',  v_row.last_run_utc,
         'age_hours',     v_age_hours,
         'threshold',     v_threshold::text
       ))
    ON CONFLICT DO NOTHING;

    GET DIAGNOSTICS v_inserted_n = ROW_COUNT;

    v_results := v_results || jsonb_build_object(
      v_row.name,
      jsonb_build_object(
        'inserted',  v_inserted_n = 1,
        'age_hours', v_age_hours,
        'cadence',   v_row.cadence
      )
    );
  END LOOP;

  RETURN v_results;
END;
$function$;

COMMENT ON FUNCTION public._scanner_liveness_watchdog() IS
  'Daily cadence-overdue check (pg_cron job scanner-liveness-watchdog). '
  'Writes operator_flags(source=scanner_liveness, kind=cadence_overdue) when '
  'an operational scanner has no scanner_runs entry within its cadence window. '
  'Idempotent via operator_flags_open_uniq partial index.';

-- Schedule: daily at 13:30 UTC. The 13:00 UTC daily dispatch tick is the
-- busiest; running 30 min later lets daily scanners update last_run_utc
-- before we check. 3h-cadence scanners (e.g. fda_signal_bridge) are checked
-- once a day too — a 9h threshold tolerates the 3 daily checkpoints either
-- way and won't false-alarm.
--
-- Idempotent re-apply: unschedule existing job (by name) before re-scheduling.

DO $cron$
DECLARE
  v_existing_jobid bigint;
BEGIN
  SELECT jobid INTO v_existing_jobid
    FROM cron.job
   WHERE jobname = 'scanner-liveness-watchdog';

  IF v_existing_jobid IS NOT NULL THEN
    PERFORM cron.unschedule(v_existing_jobid);
  END IF;

  PERFORM cron.schedule(
    'scanner-liveness-watchdog',
    '30 13 * * *',
    'SELECT public._scanner_liveness_watchdog();'
  );
END
$cron$;
