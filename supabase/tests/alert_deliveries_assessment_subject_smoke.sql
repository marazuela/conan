-- Smoke test for v3 assessment-backed alert_deliveries rows.
--
-- Exercises the production bug fixed by
-- 20260519155459_alert_deliveries_assessment_subject_check.sql:
--
--   fixture fda_asset -> fixture convergence_assessment -> alert_deliveries
--   insert with assessment_id only succeeds; orphan delivery still fails.
--
-- Run with:
--   supabase db execute --file supabase/tests/alert_deliveries_assessment_subject_smoke.sql
--
-- All writes happen inside a transaction that ROLLBACKs at the end, so the
-- target database is unaffected. The fixture assessment is not immediate-band,
-- so it does not fire the fanout webhook trigger.

BEGIN;

DO $$
DECLARE
  v_asset_id uuid;
  v_assessment_id uuid;
  v_delivery_id uuid;
BEGIN
  INSERT INTO public.fda_assets (
    ticker, drug_name, application_number, sponsor_name, indication
  )
  VALUES (
    'ADSMK', 'Assessment Smoke Drug', 'ADSMK-001', 'Assessment Smoke Pharma', 'test indication'
  )
  RETURNING id INTO v_asset_id;

  INSERT INTO public.convergence_assessments (
    asset_id,
    orchestrator_version,
    model_id,
    trigger_type,
    document_window_start,
    document_window_end,
    document_ids,
    band
  )
  VALUES (
    v_asset_id,
    'smoke',
    'smoke-model',
    'manual',
    now() - interval '1 hour',
    now(),
    ARRAY[]::uuid[],
    'watchlist'
  )
  RETURNING id INTO v_assessment_id;

  INSERT INTO public.alert_deliveries (
    alert_id,
    candidate_event_id,
    candidate_id,
    assessment_id,
    channel,
    target,
    status
  )
  VALUES (
    NULL,
    NULL,
    NULL,
    v_assessment_id,
    'email',
    'ops@example.com',
    'queued'
  )
  RETURNING id INTO v_delivery_id;

  IF v_delivery_id IS NULL THEN
    RAISE EXCEPTION 'smoke: assessment-backed alert_deliveries insert returned no id';
  END IF;

  BEGIN
    INSERT INTO public.alert_deliveries (
      alert_id,
      candidate_event_id,
      candidate_id,
      assessment_id,
      channel,
      target,
      status
    )
    VALUES (
      NULL,
      NULL,
      NULL,
      NULL,
      'email',
      'orphan@example.com',
      'queued'
    );

    RAISE EXCEPTION 'smoke: orphan alert_deliveries row unexpectedly inserted';
  EXCEPTION
    WHEN check_violation THEN
      NULL;
  END;

  RAISE NOTICE 'smoke: all assertions passed';
END;
$$;

ROLLBACK;
