-- F-303: extend _scanner_liveness_watchdog with catalyst-universe fetcher
-- freshness checks.
--
-- Background
-- ----------
-- The F-200 watchdog (migration 20260521000000) intentionally excludes
-- catalyst-universe fetchers (tool_path LIKE 'modal_workers/fetchers/universe/%')
-- because they don't write scanner_runs — so a "no scanner_run in cadence
-- window" alarm would always false-positive on them.
--
-- But that means fetcher failures (rate-limit, SEC user-agent rejected,
-- openFDA endpoint changed, EDGAR full-text-search broken) are silent — the
-- only liveness signal is catalyst_universe row freshness, which nothing
-- currently watches.
--
-- This migration adds a second pass to _scanner_liveness_watchdog that
-- checks catalyst_universe MAX(fetched_at) per source_feed and flags any
-- source_feed older than its threshold.
--
-- Source feeds (hardcoded mapping; expand here when new fetchers ship):
--   • openfda_drugsfda      ← fda_adcomm_pdufa fetcher (daily, 13 UTC)
--   • edgar_8k_mna_search   ← sec_8k_mna fetcher (daily, 13 UTC)
--
-- Threshold: 36h for daily-cadence fetchers (matches the existing daily
-- threshold for scanner-runs liveness in the same watchdog).
--
-- Dedup
-- -----
-- Uses operator_flags_open_uniq partial index. The flag is keyed by
-- (source='scanner_liveness', kind='fetcher_overdue', scanner_id=NULL,
--  signal_id=source_feed). Putting the source_feed into signal_id is a
-- minor reuse of that column — there's no natural column for it on
-- operator_flags. Alternatives would have required a new column or jsonb
-- key in evidence; signal_id is text-typed and currently nullable, so it
-- works as a dedup discriminator without schema change.

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
  v_feed_row record;
  v_max_fetched timestamptz;
  v_feed_age_hours numeric;
BEGIN
  -- ------------------------------------------------------------------
  -- Pass 1: scanner_runs freshness (existing F-200 logic).
  -- ------------------------------------------------------------------
  FOR v_row IN
    SELECT id, name, cadence, last_run_utc
      FROM public.scanners
     WHERE status = 'operational'
       AND cadence IN ('3h','daily','weekly')
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
      ('warn', 'scanner_liveness', 'cadence_overdue', v_row.id,
       format('Scanner %s overdue: cadence=%s, last run %s h ago',
              v_row.name, v_row.cadence, COALESCE(v_age_hours::text, 'never')),
       'Scanner has not written a scanner_runs row within its cadence-implied '
         'freshness window (3h:9h, daily:36h, weekly:8d). Most likely causes: '
         'dispatch cron silently skipped it (registry lookup returned a list '
         'that excluded this scanner), scanners.status flipped mid-cycle, or '
         'the worker is crashing before close_scanner_run. Cross-check '
         'scanner_runs entries vs scanners.last_run_utc to disambiguate.',
       jsonb_build_object(
         'scanner', v_row.name, 'cadence', v_row.cadence,
         'last_run_utc', v_row.last_run_utc, 'age_hours', v_age_hours,
         'threshold', v_threshold::text))
    ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS v_inserted_n = ROW_COUNT;

    v_results := v_results || jsonb_build_object(
      v_row.name,
      jsonb_build_object('inserted', v_inserted_n = 1, 'age_hours', v_age_hours, 'cadence', v_row.cadence));
  END LOOP;

  -- ------------------------------------------------------------------
  -- Pass 2 (F-303): catalyst-universe fetcher freshness.
  -- Iterate over the known source_feeds and check MAX(fetched_at) on
  -- catalyst_universe. Threshold matches daily cadence (36h).
  -- ------------------------------------------------------------------
  FOR v_feed_row IN
    SELECT * FROM (VALUES
      ('openfda_drugsfda',    'fda_adcomm_pdufa'),
      ('edgar_8k_mna_search', 'sec_8k_mna')
    ) AS feeds(source_feed, fetcher_name)
  LOOP
    SELECT MAX(fetched_at) INTO v_max_fetched
      FROM public.catalyst_universe
     WHERE source_feed = v_feed_row.source_feed;

    -- A feed with zero rows is technically "infinitely stale". Flag it; ops
    -- can verify whether the fetcher has ever run.
    v_feed_age_hours := round(
      extract(epoch FROM (NOW() - COALESCE(v_max_fetched, '1970-01-01'::timestamptz)))/3600.0,
      1
    );

    IF v_max_fetched IS NULL OR v_feed_age_hours > 36 THEN
      -- Manual per-source_feed dedup. The operator_flags_open_uniq partial
      -- index discriminates on (source, kind, scanner_id, entity_id,
      -- signal_id, candidate_id), none of which carry the source_feed
      -- identity, and signal_id is FK to public.signals so we can't reuse
      -- it as a freeform key. Instead: skip the INSERT if an unresolved
      -- row with matching evidence->>'source_feed' already exists.
      SELECT EXISTS (
        SELECT 1 FROM public.operator_flags
         WHERE source='scanner_liveness'
           AND kind='fetcher_overdue'
           AND resolved_at IS NULL
           AND evidence->>'source_feed' = v_feed_row.source_feed
      ) INTO v_existing_unresolved;

      IF NOT v_existing_unresolved THEN
        INSERT INTO public.operator_flags
          (severity, source, kind, scanner_id, title, body, evidence)
        VALUES
          ('warn', 'scanner_liveness', 'fetcher_overdue', NULL,
           format('Fetcher %s overdue: source_feed=%s, last row %s h ago',
                  v_feed_row.fetcher_name, v_feed_row.source_feed,
                  CASE WHEN v_max_fetched IS NULL THEN 'never'
                       ELSE v_feed_age_hours::text END),
           'Catalyst-universe fetcher has not written a new row within 36h. '
             'Fetchers bypass scanner_runs by design, so this is the only '
             'liveness signal for them. Likely causes: rate limit, source-side '
             'endpoint change, SEC user-agent rejection (sec_8k_mna), or '
             'openFDA API outage (openfda_drugsfda). Trigger the fetcher '
             'manually to confirm: modal run modal_workers/app.py::' || v_feed_row.fetcher_name || '_once',
           jsonb_build_object(
             'fetcher_name', v_feed_row.fetcher_name,
             'source_feed', v_feed_row.source_feed,
             'last_fetched_at', v_max_fetched,
             'age_hours', v_feed_age_hours,
             'threshold_hours', 36));
        v_inserted_n := 1;
      ELSE
        v_inserted_n := 0;
      END IF;

      v_results := v_results || jsonb_build_object(
        v_feed_row.source_feed,
        jsonb_build_object('inserted', v_inserted_n = 1,
                           'age_hours', v_feed_age_hours,
                           'kind', 'fetcher_overdue'));
    END IF;
  END LOOP;

  RETURN v_results;
END;
$function$;

COMMENT ON FUNCTION public._scanner_liveness_watchdog() IS
  'Daily cadence-overdue check (pg_cron scanner-liveness-watchdog at 13:30 UTC). '
  'Two passes: (1) scanner_runs freshness for non-fetcher scanners, '
  '(2) catalyst_universe.fetched_at freshness per source_feed for fetchers '
  '(F-303). Idempotent via operator_flags_open_uniq.';
