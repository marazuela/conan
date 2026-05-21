-- Smoke test for Phase 4B Tier-2 fanout eligibility + orphan sweeper survival.
--
-- Run with:
--   supabase db execute --file supabase/tests/tier2_fanout_orphan_sweeper_smoke.sql
--
-- All writes happen inside a transaction that ROLLBACKs at the end. The test
-- disables the immediate fanout trigger only inside this transaction so no
-- external webhook is sent, while still asserting the trigger exists.

BEGIN;

DO $$
DECLARE
  v_asset_id uuid;
  v_assessment_id uuid;
  v_deleted int;
  v_trigger_count int;
BEGIN
  SELECT count(*) INTO v_trigger_count
  FROM pg_trigger t
  JOIN pg_class c ON c.oid = t.tgrelid
  WHERE c.relname = 'convergence_assessments'
    AND t.tgname = 'convergence_assessments_immediate_fanout_wh'
    AND NOT t.tgisinternal
    AND t.tgenabled <> 'D';

  IF v_trigger_count <> 1 THEN
    RAISE EXCEPTION 'Expected enabled Tier-2 fanout trigger, got %', v_trigger_count;
  END IF;

  -- Avoid a real net.http_post while this smoke inserts an immediate row.
  ALTER TABLE public.convergence_assessments
    DISABLE TRIGGER convergence_assessments_immediate_fanout_wh;

  INSERT INTO public.fda_assets (
    ticker, drug_name, application_number, sponsor_name, indication,
    program_status, watch_priority
  ) VALUES (
    'T2FAN', 'tier2fanmab', 'T2FAN-001', 'Tier2 Fanout Bio',
    'test indication', 'phase3', 1
  ) RETURNING id INTO v_asset_id;

  INSERT INTO public.convergence_assessments (
    asset_id,
    tier,
    orchestrator_version,
    model_id,
    trigger_type,
    document_window_start,
    document_window_end,
    document_ids,
    fact_ids,
    band,
    gate_status,
    created_at
  ) VALUES (
    v_asset_id,
    2,
    'bulk_v0',
    'claude-sonnet-4-6',
    'scheduled',
    now() - interval '2 hours',
    now() - interval '1 hour',
    ARRAY[]::uuid[],
    ARRAY[]::uuid[],
    'immediate',
    'tier2_skipped',
    now() - interval '30 minutes'
  ) RETURNING id INTO v_assessment_id;

  INSERT INTO public.assessment_stage_metrics (
    assessment_id,
    stage_name,
    model,
    cost_usd,
    latency_ms,
    status,
    notes
  ) VALUES (
    v_assessment_id,
    'tier2_bulk_synthesis',
    'claude-sonnet-4-6',
    0.42,
    45000,
    'completed',
    jsonb_build_object('tier', 2, 'gate_status', 'tier2_skipped')
  );

  SELECT public.cleanup_orphaned_assessments() INTO v_deleted;

  IF NOT EXISTS (
    SELECT 1 FROM public.convergence_assessments WHERE id = v_assessment_id
  ) THEN
    RAISE EXCEPTION 'Tier-2 immediate assessment was incorrectly swept';
  END IF;

  IF v_deleted <> 0 THEN
    RAISE EXCEPTION 'Expected no orphan deletions, got %', v_deleted;
  END IF;

  ALTER TABLE public.convergence_assessments
    ENABLE TRIGGER convergence_assessments_immediate_fanout_wh;

  RAISE NOTICE 'tier2 fanout/orphan sweeper smoke test passed';
END $$;

ROLLBACK;
