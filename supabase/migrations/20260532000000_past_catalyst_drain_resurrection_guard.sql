-- Resurrection guard + pdufa_approaching coverage for the past-catalyst drain.
--
-- Follow-up to 20260514120000_past_catalyst_signal_drain.sql (already applied;
-- not edited in place — applied migrations are immutable). This CREATE OR
-- REPLACE redefines public._past_catalyst_signal_drain with two changes:
--
--   1. signal_type scope adds 'pdufa_approaching'. It was emitted after the
--      original drain shipped and is subject to the same past-catalyst
--      staleness; it falls into the pdufa_date/catalyst_date extraction
--      branch (ELSE), so no per-type date logic is needed.
--
--   2. band_with_bonus is forced to 'archive' UNCONDITIONALLY. The original
--      only stamped band_with_bonus when it was already non-NULL. But the
--      orphan_convergence_sweeper (modal_workers/observability.py) selects
--      signals where band_with_bonus IS NULL and re-invokes the reactor,
--      which re-stamps a live band from score+convergence_bonus; the
--      dashboard's v_thesis_inbox uses coalesce(band_with_bonus, band), so a
--      NULL left after drain resurrects the row. ~20 of 23 drainable rows on
--      2026-05-14 carried band_with_bonus IS NULL (orphan pattern). Forcing
--      'archive' drops them out of the sweeper's IS NULL filter for good.
--
-- The cron schedule from 20260514120000 keeps calling this function by name;
-- nothing else changes. Idempotent: re-running is safe (_drain_reason guard).

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
          ELSE  -- pdufa_watchlist, fda_decision, pdufa_imminent, pdufa_approaching, pdufa_date_advanced
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
           -- Unconditional: even when band_with_bonus was NULL (orphan
           -- pattern, ~20 of 23 drainable rows on 2026-05-14), stamp
           -- 'archive' so orphan_convergence_sweeper's `band_with_bonus
           -- IS NULL` filter doesn't pick the row up and re-invoke the
           -- reactor, resurrecting a drained signal.
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
  'Daily past-catalyst drain. Archives immediate/watchlist signals whose '
  'catalyst date is >p_days_threshold past with no terminal event. '
  'Forces band_with_bonus=archive unconditionally (resurrection guard vs '
  'orphan_convergence_sweeper). Covers pdufa_approaching as of '
  '20260532000000.';
