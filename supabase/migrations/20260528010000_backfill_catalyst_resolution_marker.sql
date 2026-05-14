-- 20260528010000_backfill_catalyst_resolution_marker.sql
-- Wave 4 deep-fix Phase C.2 — backfill the catalyst_resolution_marker column.
--
-- Why:
--   20260526000010 added the column nullable. Every row inserted before the
--   Phase B Python deploys to Modal will have NULL.  Without a backfill,
--   the nightly_calibration_refit consumer (Phase C.3) has to treat NULL as
--   "unknown source" forever, which mixes catalyst-anchored stubs with
--   default-window stubs in the regression and loses signal.
--
-- Strategy:
--   1. JOIN post_mortem_queue (catalyst_resolution_marker IS NULL) to
--      fda_regulatory_events by asset_id + a window overlap heuristic.
--      The "best match" event is the eligible one whose event_date sits
--      between (pmq.created_at - 7d) and (pmq.outcome_window_end + 2d).
--      We accept either pending or resolved status (Phase C.1 lookback policy)
--      and ALL eligible event types (CATALYST_EVENT_TYPES in runtime.py).
--      Take the row's id as the marker tag.
--   2. Any remaining NULLs (no matching event) get 'unknown_legacy' so the
--      consumer can deterministically filter them.
--
-- Idempotent: the UPDATE only touches rows where marker IS NULL.
--
-- Rollback path: leave the column populated. NULL→string transitions are
-- not reversible without dropping the column (which the prior migration
-- already added safely).

BEGIN;

-- ============================================================================
-- (1) Backfill from fda_regulatory_events where a window overlap exists.
-- ============================================================================
-- Marker grammar matches runtime.py _resolve_catalyst_window: <event_type>:<id>.
-- LATERAL pulls the soonest eligible event per pmq row, preferring pending
-- over resolved, then earliest event_date — same order the runtime uses.

UPDATE public.post_mortem_queue pmq
SET catalyst_resolution_marker = lateral_pick.marker
FROM (
  SELECT
    pmq_inner.id AS pmq_id,
    (best.event_type || ':' || best.id::text) AS marker
  FROM public.post_mortem_queue pmq_inner
  CROSS JOIN LATERAL (
    SELECT fre.id, fre.event_type, fre.event_date, fre.event_status
    FROM public.fda_regulatory_events fre
    WHERE fre.asset_id = pmq_inner.asset_id
      AND fre.event_type IN ('pdufa', 'advisory_committee', 'eop2', 'readout')
      AND fre.event_status IN ('pending', 'resolved')
      AND fre.event_date BETWEEN (pmq_inner.created_at::date - interval '7 days')
                             AND (pmq_inner.outcome_window_end::date + interval '2 days')
    ORDER BY
      (CASE WHEN fre.event_status = 'pending' THEN 0 ELSE 1 END),
      fre.event_date ASC NULLS LAST
    LIMIT 1
  ) best
  WHERE pmq_inner.catalyst_resolution_marker IS NULL
) lateral_pick
WHERE pmq.id = lateral_pick.pmq_id
  AND pmq.catalyst_resolution_marker IS NULL;

-- ============================================================================
-- (2) Anything still NULL gets the 'unknown_legacy' sentinel so the refit
--     consumer can filter deterministically.
-- ============================================================================

UPDATE public.post_mortem_queue
SET catalyst_resolution_marker = 'unknown_legacy'
WHERE catalyst_resolution_marker IS NULL;

-- ============================================================================
-- (3) Sanity report — counts by marker class. Surface in the migration log
--     so the operator sees the backfill outcome without a follow-up query.
-- ============================================================================

DO $$
DECLARE
  v_total bigint;
  v_pdufa bigint;
  v_adcom bigint;
  v_eop2 bigint;
  v_readout bigint;
  v_default bigint;
  v_unknown bigint;
BEGIN
  SELECT count(*) INTO v_total FROM public.post_mortem_queue;
  SELECT count(*) INTO v_pdufa FROM public.post_mortem_queue
    WHERE catalyst_resolution_marker LIKE 'pdufa:%';
  SELECT count(*) INTO v_adcom FROM public.post_mortem_queue
    WHERE catalyst_resolution_marker LIKE 'advisory_committee:%';
  SELECT count(*) INTO v_eop2 FROM public.post_mortem_queue
    WHERE catalyst_resolution_marker LIKE 'eop2:%';
  SELECT count(*) INTO v_readout FROM public.post_mortem_queue
    WHERE catalyst_resolution_marker LIKE 'readout:%';
  SELECT count(*) INTO v_default FROM public.post_mortem_queue
    WHERE catalyst_resolution_marker LIKE 'default_%';
  SELECT count(*) INTO v_unknown FROM public.post_mortem_queue
    WHERE catalyst_resolution_marker = 'unknown_legacy';
  RAISE NOTICE
    'post_mortem_queue marker backfill: total=% pdufa=% adcom=% eop2=% readout=% default=% unknown_legacy=%',
    v_total, v_pdufa, v_adcom, v_eop2, v_readout, v_default, v_unknown;
END $$;

-- Future-proof: once the consumer is filtering on this column, consider
-- adding `ALTER TABLE post_mortem_queue ADD CONSTRAINT … CHECK (catalyst_resolution_marker IS NOT NULL)`
-- in a follow-up migration to prevent new NULLs from sneaking back in. Left
-- nullable for now so a brief Python-vs-migration race window can't 23514.

COMMIT;
