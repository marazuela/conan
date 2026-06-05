-- Smoke test: cleanup_orphaned_assessments() must NOT delete a row that another
-- row references via superseded_by. convergence_assessments_superseded_by_fkey
-- is ON DELETE NO ACTION, so deleting a supersede-target raises
-- foreign_key_violation and fails the whole orphan-sweep (pg_cron job 14).
--
-- This pins the guard restored in 20260618000070 after 20260602000010 dropped
-- it. On the unguarded function this test FAILS — either with an explicit FK
-- violation or by deleting the referenced row.
--
-- Run with:
--   supabase db execute --file supabase/tests/orphan_sweeper_supersede_guard_smoke.sql
--
-- All writes happen inside a transaction that ROLLBACKs at the end.

BEGIN;

DO $$
DECLARE
  v_asset_id uuid;
  v_target_id uuid;     -- the orphan that is referenced as a superseded_by target
  v_referrer_id uuid;   -- a row whose superseded_by points at the orphan
  v_deleted int;
BEGIN
  INSERT INTO public.fda_assets (
    ticker, drug_name, application_number, sponsor_name, indication,
    program_status, watch_priority
  ) VALUES (
    'SUPGRD', 'supersedeguardmab', 'SUPGRD-001', 'Supersede Guard Bio',
    'test indication', 'phase3', 1
  ) RETURNING id INTO v_asset_id;

  -- Target: a zero-metric, >15-min-old row → would be classified an orphan.
  INSERT INTO public.convergence_assessments (
    asset_id, tier, orchestrator_version, model_id, trigger_type,
    document_window_start, document_window_end, document_ids, fact_ids,
    band, gate_status, created_at
  ) VALUES (
    v_asset_id, 1, 'v0', 'claude-sonnet-4-6', 'scheduled',
    now() - interval '2 hours', now() - interval '1 hour',
    ARRAY[]::uuid[], ARRAY[]::uuid[], 'immediate', 'pass',
    now() - interval '30 minutes'
  ) RETURNING id INTO v_target_id;

  -- Referrer: a valid row (has a stage metric) that points at the target.
  INSERT INTO public.convergence_assessments (
    asset_id, tier, orchestrator_version, model_id, trigger_type,
    document_window_start, document_window_end, document_ids, fact_ids,
    band, gate_status, created_at, superseded_by
  ) VALUES (
    v_asset_id, 1, 'v0', 'claude-sonnet-4-6', 'scheduled',
    now() - interval '3 hours', now() - interval '2 hours',
    ARRAY[]::uuid[], ARRAY[]::uuid[], 'immediate', 'pass',
    now() - interval '40 minutes', v_target_id
  ) RETURNING id INTO v_referrer_id;

  INSERT INTO public.assessment_stage_metrics (
    assessment_id, stage_name, model, cost_usd, latency_ms, status, notes
  ) VALUES (
    v_referrer_id, 'stage_1_rag', 'claude-sonnet-4-6', 0.01, 1200, 'completed',
    jsonb_build_object('tier', 1)
  );

  -- Must not raise (FK violation) and must not delete the supersede target.
  SELECT public.cleanup_orphaned_assessments() INTO v_deleted;

  IF NOT EXISTS (
    SELECT 1 FROM public.convergence_assessments WHERE id = v_target_id
  ) THEN
    RAISE EXCEPTION 'Supersede-target orphan was incorrectly swept (id=%)', v_target_id;
  END IF;

  IF v_deleted <> 0 THEN
    RAISE EXCEPTION 'Expected 0 deletions (target is supersede-protected), got %', v_deleted;
  END IF;

  RAISE NOTICE 'orphan sweeper supersede-guard smoke test passed';
END $$;

ROLLBACK;
