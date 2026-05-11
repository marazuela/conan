-- Smoke test for the FDA agent review enqueue trigger.
--
-- Exercises the end-to-end flow proven by migration
-- 20260520000000_v3_fda_agent_review_enqueue.sql:
--
--   fixture asset → fixture pending pdufa event → trigger fires →
--   3 queued rows in fda_agent_reviews → resolution-type insert → 0 new rows
--   → re-insert same canonical event → 0 new rows (idempotency)
--
-- Run with:
--   supabase db execute --file supabase/tests/fda_agent_review_enqueue_smoke.sql
--
-- All writes happen inside a transaction that ROLLBACKs at the end, so the
-- target database is unaffected. Failed assertions RAISE EXCEPTION, which the
-- ROLLBACK then propagates to the caller's exit code.

BEGIN;

DO $$
DECLARE
  v_asset_id uuid;
  v_pending_event_id uuid;
  v_resolution_event_id uuid;
  v_review_count integer;
  v_kinds text[];
  v_inserted_again integer;
BEGIN
  -- ---- Setup: fixture asset + a pending opportunity event ----
  INSERT INTO public.fda_assets (
    ticker, drug_name, application_number, sponsor_name, indication
  )
  VALUES (
    'TST', 'testdrug', 'TEST-001', 'Smoke Pharma', 'test indication'
  )
  RETURNING id INTO v_asset_id;

  INSERT INTO public.fda_regulatory_events (
    asset_id, event_type, event_date, event_status, source_content_hash, notes
  )
  VALUES (
    v_asset_id, 'pdufa', '2026-12-01', 'pending', 'smoke-hash-pdufa-1', 'smoke fixture'
  )
  RETURNING id INTO v_pending_event_id;

  -- ---- Assert 1: trigger enqueued 3 'queued' rows, one per primary agent_kind ----
  SELECT count(*), array_agg(agent_kind ORDER BY agent_kind)
    INTO v_review_count, v_kinds
    FROM public.fda_agent_reviews
   WHERE event_id = v_pending_event_id;

  IF v_review_count <> 3 THEN
    RAISE EXCEPTION 'smoke: expected 3 review rows, got %', v_review_count;
  END IF;
  IF v_kinds IS DISTINCT FROM ARRAY['medical','microstructure','regulatory']::text[] THEN
    RAISE EXCEPTION 'smoke: unexpected kinds %', v_kinds;
  END IF;

  -- All three rows must be 'queued' with matching snapshot_hash + 'pending' version.
  IF EXISTS (
    SELECT 1 FROM public.fda_agent_reviews
     WHERE event_id = v_pending_event_id
       AND (status <> 'queued' OR snapshot_hash <> 'smoke-hash-pdufa-1' OR version <> 'pending')
  ) THEN
    RAISE EXCEPTION 'smoke: queued rows have wrong status / snapshot_hash / version';
  END IF;

  -- ---- Assert 2: a resolution-type event (approval) MUST NOT enqueue reviews ----
  INSERT INTO public.fda_regulatory_events (
    asset_id, event_type, event_date, event_status, source_content_hash
  )
  VALUES (
    v_asset_id, 'approval', '2026-12-15', 'pending', 'smoke-hash-approval-1'
  )
  RETURNING id INTO v_resolution_event_id;

  IF EXISTS (SELECT 1 FROM public.fda_agent_reviews WHERE event_id = v_resolution_event_id) THEN
    RAISE EXCEPTION 'smoke: resolution event % should not have enqueued reviews', v_resolution_event_id;
  END IF;

  -- ---- Assert 3: helper is idempotent for the pending event ----
  v_inserted_again := public.enqueue_fda_agent_reviews(v_pending_event_id, 'smoke-hash-pdufa-1');
  IF v_inserted_again <> 0 THEN
    RAISE EXCEPTION 'smoke: re-enqueue with same snapshot_hash should insert 0, got %', v_inserted_again;
  END IF;

  -- ---- Assert 4: a NEW snapshot_hash (manual-refresh style) DOES enqueue 3 more rows ----
  v_inserted_again := public.enqueue_fda_agent_reviews(v_pending_event_id, 'manual:smoketest');
  IF v_inserted_again <> 3 THEN
    RAISE EXCEPTION 'smoke: refresh with new snapshot_hash should insert 3, got %', v_inserted_again;
  END IF;

  SELECT count(*) INTO v_review_count
    FROM public.fda_agent_reviews
   WHERE event_id = v_pending_event_id;
  IF v_review_count <> 6 THEN
    RAISE EXCEPTION 'smoke: expected 6 rows after refresh enqueue, got %', v_review_count;
  END IF;

  RAISE NOTICE 'smoke: all assertions passed';
END;
$$;

ROLLBACK;
