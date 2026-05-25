-- Smoke test for public.orchestrator_enqueue_guard.
--
-- Coverage:
--   1. Bypass triggers (manual / operator_refresh / backtest) proceed.
--   2. NULL hash proceeds (no fingerprint to dedup against).
--   3. Same-hash non-superseded assessment <6h → skip with reason
--      'same_hash_within_cooldown'.
--   4. tier2_escalation participates in dedup (locked decision 2026-05-25).
--   5. Assessment >6h old → no skip (cool-down expired).
--   6. Superseded assessment → no skip even if <6h old.
--   7. Pending orchestrator_runs row with same hash → skip with reason
--      'pending_same_hash'.
--
-- Run with:
--   supabase db execute --file supabase/tests/orchestrator_enqueue_guard_smoke.sql
--
-- All writes happen inside a transaction that ROLLBACKs at the end.

BEGIN;

DO $$
DECLARE
  v_asset_id      uuid;
  v_hash          text := md5('enqueue-guard-smoke-fixture');
  v_assessment_id uuid;
  v_result        jsonb;
BEGIN
  INSERT INTO public.fda_assets (id, ticker, drug_name, is_active, aging_state)
  VALUES (gen_random_uuid(), 'TST_GRD', 'TEST_GRD', true, 'watch')
  RETURNING id INTO v_asset_id;

  -- Case 1: bypass triggers.
  v_result := public.orchestrator_enqueue_guard(v_asset_id, 'manual', v_hash);
  IF (v_result->>'skip')::boolean THEN
    RAISE EXCEPTION 'Case 1a (manual): expected skip=false, got %', v_result;
  END IF;
  v_result := public.orchestrator_enqueue_guard(v_asset_id, 'operator_refresh', v_hash);
  IF (v_result->>'skip')::boolean THEN
    RAISE EXCEPTION 'Case 1b (operator_refresh): expected skip=false, got %', v_result;
  END IF;
  v_result := public.orchestrator_enqueue_guard(v_asset_id, 'backtest', v_hash);
  IF (v_result->>'skip')::boolean THEN
    RAISE EXCEPTION 'Case 1c (backtest): expected skip=false, got %', v_result;
  END IF;

  -- Case 2: NULL hash.
  v_result := public.orchestrator_enqueue_guard(v_asset_id, 'scheduled', NULL);
  IF (v_result->>'skip')::boolean THEN
    RAISE EXCEPTION 'Case 2 (null hash): expected skip=false, got %', v_result;
  END IF;
  IF (v_result->>'reason') <> 'null_hash_no_fingerprint' THEN
    RAISE EXCEPTION 'Case 2 reason: expected null_hash_no_fingerprint, got %', v_result;
  END IF;

  -- Seed a recent non-superseded tier=2 assessment with the same hash.
  -- Tier=2 avoids the tier-1 document_window NOT NULL constraint; the guard
  -- does not filter by tier so this is sufficient.
  INSERT INTO public.convergence_assessments (
    asset_id, orchestrator_version, model_id, trigger_type,
    document_ids, fact_ids, evidence_ledger, cited_prose_blocks,
    key_facts, uncertainties, ensemble_n, constitutional_retries,
    total_input_tokens, total_output_tokens, total_thinking_tokens,
    total_cache_read_tokens, total_cache_creation_tokens, cost_usd,
    surviving_hypothesis_ids, tier, document_set_hash, created_at
  )
  VALUES (
    v_asset_id, 'v3', 'claude-sonnet-4-6', 'scheduled',
    ARRAY[]::uuid[], ARRAY[]::uuid[], '{}'::jsonb, '[]'::jsonb,
    '{}'::jsonb, '[]'::jsonb, 1, 0, 0, 0, 0, 0, 0, 0,
    ARRAY[]::uuid[], 2, v_hash, now() - interval '2 hours'
  )
  RETURNING id INTO v_assessment_id;

  -- Case 3: same-hash assessment <6h → skip.
  v_result := public.orchestrator_enqueue_guard(v_asset_id, 'catalyst_proximity', v_hash);
  IF NOT (v_result->>'skip')::boolean THEN
    RAISE EXCEPTION 'Case 3 (same hash 2h old): expected skip=true, got %', v_result;
  END IF;
  IF (v_result->>'reason') <> 'same_hash_within_cooldown' THEN
    RAISE EXCEPTION 'Case 3 reason: expected same_hash_within_cooldown, got %', v_result;
  END IF;

  -- Case 4: tier2_escalation also participates.
  v_result := public.orchestrator_enqueue_guard(v_asset_id, 'tier2_escalation', v_hash);
  IF NOT (v_result->>'skip')::boolean THEN
    RAISE EXCEPTION 'Case 4 (tier2_escalation): expected skip=true, got %', v_result;
  END IF;

  -- Case 5: age out the assessment past the 6h cool-down.
  UPDATE public.convergence_assessments
     SET created_at = now() - interval '8 hours'
   WHERE id = v_assessment_id;
  v_result := public.orchestrator_enqueue_guard(v_asset_id, 'scheduled', v_hash);
  IF (v_result->>'skip')::boolean THEN
    RAISE EXCEPTION 'Case 5 (8h old): expected skip=false, got %', v_result;
  END IF;

  -- Case 6: mark assessment superseded — should no longer gate.
  UPDATE public.convergence_assessments
     SET created_at = now() - interval '2 hours', superseded_at = now()
   WHERE id = v_assessment_id;
  v_result := public.orchestrator_enqueue_guard(v_asset_id, 'scheduled', v_hash);
  IF (v_result->>'skip')::boolean THEN
    RAISE EXCEPTION 'Case 6 (superseded): expected skip=false, got %', v_result;
  END IF;

  -- Case 7: a pending same-hash run gates.
  INSERT INTO public.orchestrator_runs
    (asset_id, trigger_type, tier, status, document_set_hash)
  VALUES (v_asset_id, 'scheduled', 2, 'pending', v_hash);

  v_result := public.orchestrator_enqueue_guard(v_asset_id, 'catalyst_proximity', v_hash);
  IF NOT (v_result->>'skip')::boolean THEN
    RAISE EXCEPTION 'Case 7 (pending same hash): expected skip=true, got %', v_result;
  END IF;
  IF (v_result->>'reason') <> 'pending_same_hash' THEN
    RAISE EXCEPTION 'Case 7 reason: expected pending_same_hash, got %', v_result;
  END IF;

  RAISE NOTICE 'orchestrator_enqueue_guard_smoke: all 7 cases passed';
END $$;

ROLLBACK;
