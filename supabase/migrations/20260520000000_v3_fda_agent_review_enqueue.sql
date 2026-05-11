-- v3 FDA agent review enqueue: source-event → fda_agent_reviews INSERT plumbing.
--
-- Problem: fda_agent_reviews has the table and Python validator
-- (modal_workers/shared/fda_agent_validator.py), and the canonical Cowork
-- wrappers exist for medical / microstructure / regulatory. But there is no
-- enqueue path — zero rows have ever landed in fda_agent_reviews, so the three
-- fda_*_review skills produce only empty-queue fast-exits. Scheduling them is
-- a no-op until something seeds the queue.
--
-- Fix: AFTER INSERT trigger on fda_regulatory_events that enqueues three
-- 'queued' rows per opportunity event (one each for medical / regulatory /
-- microstructure). Resolution events (approval / crl / presumed_crl /
-- withdrawal) and non-pending event_status are skipped — those are outcomes,
-- not setups the specialists assess.
--
-- snapshot_hash semantics: we use the event's own source_content_hash as the
-- initial snapshot_hash. The UNIQUE (event_id, agent_kind, snapshot_hash)
-- constraint then makes re-INSERT of the same event idempotent. When a
-- meaningful evidence change should re-trigger a refresh, the existing
-- public.fda_event_request_specialist_refresh RPC mints a fresh
-- 'manual:<8 hex>' snapshot_hash and re-enqueues — that path is unchanged.
--
-- Backfill: pending opportunity events that landed before this migration get
-- a one-shot INSERT...SELECT at apply time so the queue is non-empty before
-- the cron schedule fires (Phase 2 of the cron distribution per MEMORY).
--
-- Rollback:
--   DROP TRIGGER IF EXISTS enqueue_fda_agent_reviews_on_event_insert_tg
--     ON public.fda_regulatory_events;
--   DROP FUNCTION IF EXISTS public.enqueue_fda_agent_reviews_on_event_insert();
--   DROP FUNCTION IF EXISTS public.enqueue_fda_agent_reviews(uuid, text);

-- ============================================================================
-- 1. Helper function (callable from triggers, RPCs, and ad-hoc backfills)
-- ============================================================================
--
-- Inserts one 'queued' fda_agent_reviews row for each of the three primary
-- specialist kinds (medical / regulatory / microstructure). ON CONFLICT on the
-- UNIQUE (event_id, agent_kind, snapshot_hash) makes this idempotent — re-
-- calling with the same args is a no-op. Returns the number of rows actually
-- inserted (0..3) so callers can distinguish "first enqueue" from "already
-- there".
--
-- The three sub-agent kinds added in D-107 (literature / competitive / ic_memo)
-- are NOT enqueued here. Those land via the v3 orchestrator's Stage-10 IC memo
-- path (see 20260511000000_v3_fda_signal_promote_to_thesis.sql) and have a
-- different lifecycle.

CREATE OR REPLACE FUNCTION public.enqueue_fda_agent_reviews(
  p_event_id uuid,
  p_snapshot_hash text
) RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $func$
DECLARE
  v_inserted integer;
BEGIN
  IF p_event_id IS NULL THEN
    RAISE EXCEPTION 'enqueue_fda_agent_reviews: event_id is required';
  END IF;
  IF p_snapshot_hash IS NULL OR length(trim(p_snapshot_hash)) = 0 THEN
    RAISE EXCEPTION 'enqueue_fda_agent_reviews: snapshot_hash is required';
  END IF;

  WITH inserted AS (
    INSERT INTO public.fda_agent_reviews (
      event_id, agent_kind, version, snapshot_hash, status
    )
    SELECT p_event_id, kind, 'pending', p_snapshot_hash, 'queued'
      FROM unnest(ARRAY['medical','regulatory','microstructure']) AS kind
    ON CONFLICT (event_id, agent_kind, snapshot_hash) DO NOTHING
    RETURNING 1
  )
  SELECT count(*)::int INTO v_inserted FROM inserted;

  RETURN v_inserted;
END;
$func$;

REVOKE ALL ON FUNCTION public.enqueue_fda_agent_reviews(uuid, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.enqueue_fda_agent_reviews(uuid, text) TO service_role;

COMMENT ON FUNCTION public.enqueue_fda_agent_reviews(uuid, text) IS
  'Idempotently enqueue three fda_agent_reviews rows (medical / regulatory / '
  'microstructure) for the given event + snapshot_hash. Returns count inserted '
  '(0..3). UNIQUE (event_id, agent_kind, snapshot_hash) handles dedup.';

-- ============================================================================
-- 2. AFTER INSERT trigger on fda_regulatory_events
-- ============================================================================
--
-- Fires on every fda_regulatory_events INSERT. Short-circuits and returns NEW
-- without enqueueing when:
--   - event_status != 'pending' (only pending opportunities trigger reviews;
--     'resolved' / 'superseded' rows are post-fact bookkeeping)
--   - event_type IN (approval, crl, presumed_crl, withdrawal) — resolution
--     outcomes, not setups. Mirrors the guard in
--     fda_event_approve_for_thesis (20260505000040_fda_dashboard_rpcs.sql:61).
--
-- Snapshot_hash is the event's source_content_hash (NOT NULL on the table), so
-- a re-INSERT of the same canonical event (same hash) is a no-op via the
-- UNIQUE constraint inside enqueue_fda_agent_reviews.

CREATE OR REPLACE FUNCTION public.enqueue_fda_agent_reviews_on_event_insert()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $func$
BEGIN
  IF NEW.event_status IS DISTINCT FROM 'pending' THEN
    RETURN NEW;
  END IF;
  IF NEW.event_type IN ('approval','crl','presumed_crl','withdrawal') THEN
    RETURN NEW;
  END IF;

  PERFORM public.enqueue_fda_agent_reviews(NEW.id, NEW.source_content_hash);
  RETURN NEW;
END;
$func$;

DROP TRIGGER IF EXISTS enqueue_fda_agent_reviews_on_event_insert_tg
  ON public.fda_regulatory_events;
CREATE TRIGGER enqueue_fda_agent_reviews_on_event_insert_tg
  AFTER INSERT ON public.fda_regulatory_events
  FOR EACH ROW
  EXECUTE FUNCTION public.enqueue_fda_agent_reviews_on_event_insert();

-- ============================================================================
-- 3. Backfill pending opportunity events that landed pre-trigger
-- ============================================================================
--
-- Idempotent via the same UNIQUE (event_id, agent_kind, snapshot_hash) — re-
-- applying the migration on a database that already has the trigger installed
-- inserts nothing new.

INSERT INTO public.fda_agent_reviews (
  event_id, agent_kind, version, snapshot_hash, status
)
SELECT e.id, kind, 'pending', e.source_content_hash, 'queued'
  FROM public.fda_regulatory_events e
  CROSS JOIN unnest(ARRAY['medical','regulatory','microstructure']) AS kind
 WHERE e.event_status = 'pending'
   AND e.event_type NOT IN ('approval','crl','presumed_crl','withdrawal')
ON CONFLICT (event_id, agent_kind, snapshot_hash) DO NOTHING;
