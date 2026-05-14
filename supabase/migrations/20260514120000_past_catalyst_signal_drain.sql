-- 2026-05-14 — past-catalyst signal drain (daily)
--
-- Demotes signals whose catalyst date is N+ days in the past from
-- immediate/watchlist → archive, stamping a drain marker in dimensions so
-- downstream consumers (dashboard inbox, thesis_writer) can distinguish
-- "drained because past-catalyst" from genuine archive demotions.
--
-- Scope:
--   - signal_type IN ('pre_phase3_readout','eop2_meeting','binary_catalyst',
--                     'pdufa_watchlist','fda_decision','pdufa_imminent',
--                     'pdufa_approaching','pdufa_date_advanced')
--   - band IN ('immediate','watchlist')
--   - catalyst date (raw_payload->>'primary_completion_date' or
--     'pdufa_date' or 'catalyst_date', signal_type-aware) < current_date - N
--   - dimensions does NOT already carry _drain_reason (idempotent)
--
-- Resurrection guard: orphan_convergence_sweeper
-- (modal_workers/observability.py:533) fetches signals where
-- `band_with_bonus IS NULL` and re-invokes the reactor — which would re-stamp
-- band_with_bonus to a live band based on score+convergence_bonus, and the
-- dashboard's v_thesis_inbox uses coalesce(band_with_bonus, band), so a NULL
-- left after drain would resurrect the row. We force band_with_bonus='archive'
-- unconditionally so drained rows fall out of the sweeper's `band_with_bonus
-- IS NULL` filter and stay archived.
--
-- Why a drain instead of a scanner-emit gate:
--   - fda_pdufa_pipeline intentionally emits fda_decision up to T+60d
--     (modal_workers/scanners/fda_pdufa_pipeline.py:1648) so that approvals
--     and CRLs landing post-PDUFA produce signals.
--   - pre_phase3_readout's READOUT_MIN_DAYS=-14 lets pre-readout signals stay
--     emittable for 2 weeks after the primary completion date so a late
--     readout still hits the radar.
--   - Both legitimate-at-emit windows are wider than the dashboard's "still
--     actionable" horizon. Once the catalyst is N+ days past and no terminal
--     event (CRL/approval/readout) has updated the row, the watchlist entry
--     is dead weight. Drain it.
--
-- Marker schema (matches the 2026-05-13 manual one-shot D-PIPELINE-FIX):
--   dimensions._drain_reason = 'past_catalyst_no_resolution'
--   dimensions._drained_at   = timestamp (ISO)
--   dimensions._drained_by   = 'cron:past_catalyst_signal_drain'
--   dimensions._drain_note   = 'Catalyst date <X> is <D> days past with no terminal event captured.'
--
-- The function does NOT touch signals.score and does NOT touch
-- dimensions._provenance, so the signals_update_wh trigger (which gates
-- reactor webhooks on score / _provenance changes) does NOT fire — no
-- cascading reactor work from the drain.
--
-- Schedule: daily 06:15 UTC, just after v3-fda-aging-stage-a (05:55).

CREATE OR REPLACE FUNCTION public._past_catalyst_signal_drain(
  p_days_threshold integer DEFAULT 2,
  p_limit integer DEFAULT 1000
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'public'
AS $function$
DECLARE
  v_now      timestamptz := now();
  v_today    date := current_date;
  v_drained  integer := 0;
  v_by_type  jsonb := '{}'::jsonb;
  v_row      record;
  v_count_in_type integer;
BEGIN
  -- Iterate over eligible rows. Per-row UPDATE keeps the dimensions merge
  -- straightforward and per-type counters cheap. Cap with p_limit so a
  -- pathological backlog never holds the cron worker forever.
  FOR v_row IN
    WITH parsed AS (
      SELECT
        s.signal_id,
        s.signal_type,
        s.band,
        s.band_with_bonus,
        s.dimensions,
        -- Per-type catalyst-date extraction. Each branch reads the
        -- raw_payload field for its preferred date source, parsing
        -- partial dates safely (YYYY-MM treated as YYYY-MM-01).
        CASE s.signal_type
          WHEN 'pre_phase3_readout' THEN
            COALESCE(
              CASE WHEN s.raw_payload->>'primary_completion_date' ~ '^\d{4}-\d{2}-\d{2}'
                THEN substring(s.raw_payload->>'primary_completion_date' FROM 1 FOR 10)::date END,
              CASE WHEN s.raw_payload->>'primary_completion_date' ~ '^\d{4}-\d{2}$'
                THEN ((s.raw_payload->>'primary_completion_date') || '-01')::date END,
              CASE WHEN s.raw_payload->>'catalyst_date' ~ '^\d{4}-\d{2}-\d{2}'
                THEN substring(s.raw_payload->>'catalyst_date' FROM 1 FOR 10)::date END
            )
          WHEN 'eop2_meeting' THEN
            CASE WHEN s.raw_payload->>'catalyst_date' ~ '^\d{4}-\d{2}-\d{2}'
              THEN substring(s.raw_payload->>'catalyst_date' FROM 1 FOR 10)::date END
          WHEN 'binary_catalyst' THEN
            COALESCE(
              CASE WHEN s.raw_payload->>'catalyst_date' ~ '^\d{4}-\d{2}-\d{2}'
                THEN substring(s.raw_payload->>'catalyst_date' FROM 1 FOR 10)::date END,
              CASE WHEN s.raw_payload->>'pdufa_date' ~ '^\d{4}-\d{2}-\d{2}'
                THEN substring(s.raw_payload->>'pdufa_date' FROM 1 FOR 10)::date END
            )
          ELSE  -- pdufa_watchlist, fda_decision, pdufa_imminent, pdufa_date_advanced
            COALESCE(
              CASE WHEN s.raw_payload->>'pdufa_date' ~ '^\d{4}-\d{2}-\d{2}'
                THEN substring(s.raw_payload->>'pdufa_date' FROM 1 FOR 10)::date END,
              CASE WHEN s.raw_payload->>'catalyst_date' ~ '^\d{4}-\d{2}-\d{2}'
                THEN substring(s.raw_payload->>'catalyst_date' FROM 1 FOR 10)::date END
            )
        END AS catalyst_d
      FROM public.signals s
      WHERE s.signal_type IN (
              'pre_phase3_readout','eop2_meeting','binary_catalyst',
              'pdufa_watchlist','fda_decision','pdufa_imminent',
              'pdufa_approaching','pdufa_date_advanced'
            )
        AND s.band IN ('immediate','watchlist')
        -- Idempotent: skip rows already drained.
        AND NOT (s.dimensions ? '_drain_reason')
    )
    SELECT signal_id, signal_type, band, band_with_bonus, dimensions, catalyst_d
      FROM parsed
     WHERE catalyst_d IS NOT NULL
       AND catalyst_d < (v_today - make_interval(days => p_days_threshold))
     ORDER BY catalyst_d ASC
     LIMIT p_limit
  LOOP
    UPDATE public.signals s
       SET band            = 'archive'::signal_band,
           -- Unconditional: even when band_with_bonus was NULL (orphan-pattern,
           -- ~20 of 23 drainable rows on 2026-05-14), stamp 'archive' so the
           -- orphan_convergence_sweeper's `band_with_bonus IS NULL` filter
           -- doesn't pick the row up and re-invoke the reactor.
           band_with_bonus = 'archive'::signal_band,
           dimensions = s.dimensions || jsonb_build_object(
             '_drain_reason', 'past_catalyst_no_resolution',
             '_drained_at',   v_now,
             '_drained_by',   'cron:past_catalyst_signal_drain',
             '_drain_note',   format(
               'Catalyst date %s is %s days past with no terminal event captured.',
               to_char(v_row.catalyst_d, 'YYYY-MM-DD'),
               (v_today - v_row.catalyst_d)::text
             )
           )
     WHERE s.signal_id = v_row.signal_id
       -- Defensive: skip if a concurrent writer beat us to it.
       AND NOT (s.dimensions ? '_drain_reason');

    IF FOUND THEN
      v_drained := v_drained + 1;
      v_count_in_type := COALESCE((v_by_type->>v_row.signal_type)::int, 0) + 1;
      v_by_type := v_by_type || jsonb_build_object(v_row.signal_type, v_count_in_type);
    END IF;
  END LOOP;

  RETURN jsonb_build_object(
    'drained',           v_drained,
    'by_signal_type',    v_by_type,
    'days_threshold',    p_days_threshold,
    'limit',             p_limit,
    'evaluated_at',      v_now
  );
END;
$function$;

COMMENT ON FUNCTION public._past_catalyst_signal_drain(integer, integer) IS
  'Daily drain of past-catalyst signals (pg_cron job past-catalyst-signal-drain). '
  'Demotes immediate/watchlist signals whose catalyst date (primary_completion_date '
  '/ pdufa_date / catalyst_date, per signal_type) is N+ days past, stamping '
  'dimensions._drain_reason=past_catalyst_no_resolution. Idempotent: skips rows '
  'already drained. Score and dimensions._provenance are untouched, so the '
  'signals_update_wh reactor trigger does not fire.';

-- ---------------------------------------------------------------------------
-- Cron schedule — daily at 06:15 UTC.
-- Sequenced after v3-fda-aging-stage-a (05:55) so any terminal-event updates
-- written by that step have settled before we evaluate "no resolution captured".
-- ---------------------------------------------------------------------------

DO $cron$
DECLARE
  v_existing_jobid bigint;
BEGIN
  SELECT jobid INTO v_existing_jobid
    FROM cron.job
   WHERE jobname = 'past-catalyst-signal-drain';

  IF v_existing_jobid IS NOT NULL THEN
    PERFORM cron.unschedule(v_existing_jobid);
  END IF;

  PERFORM cron.schedule(
    'past-catalyst-signal-drain',
    '15 6 * * *',
    'SELECT public._past_catalyst_signal_drain();'
  );
END
$cron$;
