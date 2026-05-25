-- Smoke test for the canonical v3 FDA ranking surface.
--
-- Run with:
--   supabase db execute --file supabase/tests/v3_fda_ranked_opportunities_smoke.sql

BEGIN;

DO $$
DECLARE
  v_entity_id uuid;
  v_active_asset_id uuid;
  v_inactive_asset_id uuid;
  v_active_rows int;
  v_inactive_rows int;
  v_score numeric;
  v_status text;
  v_inactive_candidate_score numeric;
BEGIN
  INSERT INTO public.entities (
    name, primary_ticker, primary_mic, country
  ) VALUES (
    'Rank Smoke Bio', 'RSMK', 'XNAS', 'US'
  ) RETURNING id INTO v_entity_id;

  -- Legacy v2 candidate context exists and has a high score, mimicking the
  -- stale-score failure mode. It must not be the live ranking source.
  INSERT INTO public.candidates (
    ticker, mic, entity_id, state, scoring_profile,
    current_score, current_band, next_catalyst_date
  ) VALUES (
    'RSMK', 'XNAS', v_entity_id, 'active', 'binary_catalyst',
    99.00, 'immediate', current_date + 30
  );

  INSERT INTO public.fda_assets (
    ticker, mic, entity_id, drug_name, application_number,
    sponsor_name, indication, program_status, is_active, watch_priority
  ) VALUES (
    'RSMK', 'XNAS', v_entity_id, 'rankmab-active', 'RSMK-ACT',
    'Rank Smoke Bio', 'test indication', 'filed', true, 1
  ) RETURNING id INTO v_active_asset_id;

  INSERT INTO public.fda_assets (
    ticker, mic, entity_id, drug_name, application_number,
    sponsor_name, indication, program_status, is_active, watch_priority
  ) VALUES (
    'RSMK', 'XNAS', v_entity_id, 'rankmab-expired', 'RSMK-OLD',
    'Rank Smoke Bio', 'test indication', 'approved', false, 3
  ) RETURNING id INTO v_inactive_asset_id;

  INSERT INTO public.convergence_assessments (
    asset_id, orchestrator_version, model_id, trigger_type,
    document_window_start, document_window_end, document_ids, fact_ids,
    raw_conviction_pct, conviction_pct_calibrated, conviction_pct,
    thesis_direction, thesis_summary, evidence_quality, band, tier
  ) VALUES (
    v_active_asset_id, 'smoke', 'claude-sonnet-4-5-20250929', 'manual',
    now() - interval '1 day', now(), '{}'::uuid[], '{}'::uuid[],
    88.00, 82.00, 82.00,
    'long', 'smoke assessment', 0.80, 'immediate', 1
  );

  SELECT count(*), max(ranking_score), max(ranking_status)
    INTO v_active_rows, v_score, v_status
    FROM public.v_fda_ranked_opportunities
   WHERE asset_id = v_active_asset_id;

  IF v_active_rows <> 1 THEN
    RAISE EXCEPTION 'Expected active asset to appear exactly once in ranked view, got %', v_active_rows;
  END IF;

  IF v_score <> 82.00 THEN
    RAISE EXCEPTION 'Expected ranking_score to use v3 conviction_pct_calibrated=82.00, got %', v_score;
  END IF;

  IF v_status <> 'assessed' THEN
    RAISE EXCEPTION 'Expected assessed ranking_status, got %', v_status;
  END IF;

  SELECT count(*)
    INTO v_inactive_rows
    FROM public.v_fda_ranked_opportunities
   WHERE asset_id = v_inactive_asset_id;

  IF v_inactive_rows <> 0 THEN
    RAISE EXCEPTION 'Expected inactive asset to be absent from ranked view, got % rows', v_inactive_rows;
  END IF;

  SELECT candidate_score
    INTO v_inactive_candidate_score
    FROM public.v_latest_assessments_by_asset
   WHERE asset_id = v_inactive_asset_id;

  IF v_inactive_candidate_score IS NOT NULL THEN
    RAISE EXCEPTION 'Expected inactive asset candidate_score to be suppressed, got %', v_inactive_candidate_score;
  END IF;

  RAISE NOTICE 'v3_fda_ranked_opportunities smoke test passed';
END $$;

ROLLBACK;
