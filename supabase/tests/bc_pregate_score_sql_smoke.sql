-- Smoke test for public.bc_pregate_score_sql — pins the SQL scorer to the TS
-- rubric (bc-pregate.ts scoreBcPregate: breakthrough 6, first_time_sponsor 4,
-- priority_review 3, class_precedent * 5; default threshold 4).
--
-- Run with:
--   supabase db execute --file supabase/tests/bc_pregate_score_sql_smoke.sql
--
-- All writes happen inside a transaction that ROLLBACKs at the end, so the target
-- database is unaffected (including the in-test bc_pregate_threshold override).

BEGIN;

-- Deterministic threshold regardless of the live config value.
INSERT INTO public.internal_config(key, value, updated_at)
VALUES ('bc_pregate_threshold', '4', now())
ON CONFLICT (key) DO UPDATE SET value = excluded.value;

DO $$
DECLARE
  v_id uuid;
  v_r jsonb;
BEGIN
  INSERT INTO public.fda_assets(ticker, drug_name, mechanism, indication,
      breakthrough_designation, priority_review, first_time_sponsor,
      designations_enriched_at)
  VALUES ('TEST', 'SmokeDrug', 'test moa', 'test indication',
      true, true, true, now())
  RETURNING id INTO v_id;

  -- all-fire (no class-precedent row) -> 6+4+3 = 13, passes at threshold 4
  v_r := public.bc_pregate_score_sql(v_id);
  ASSERT (v_r->>'score')::numeric = 13, format('all-fire score=13, got %s', v_r->>'score');
  ASSERT (v_r->>'passed')::boolean = true, 'all-fire passes';
  ASSERT v_r->>'enrichment_state' = 'ready', 'all-fire ready';

  -- priority only -> 3 < 4 -> declines, surfaces the two missing-signal reasons
  UPDATE public.fda_assets SET breakthrough_designation=false, first_time_sponsor=false
    WHERE id=v_id;
  v_r := public.bc_pregate_score_sql(v_id);
  ASSERT (v_r->>'score')::numeric = 3, format('priority-only score=3, got %s', v_r->>'score');
  ASSERT (v_r->>'passed')::boolean = false, 'priority-only (3) declines at threshold 4';
  ASSERT v_r->'reasons' ? 'no_breakthrough_designation', 'reason: breakthrough';
  ASSERT v_r->'reasons' ? 'sponsor_has_prior_p3', 'reason: sponsor';

  -- first_time_sponsor only -> 4 >= 4 -> passes at the boundary
  UPDATE public.fda_assets SET priority_review=false, first_time_sponsor=true WHERE id=v_id;
  v_r := public.bc_pregate_score_sql(v_id);
  ASSERT (v_r->>'score')::numeric = 4, format('sponsor-only score=4, got %s', v_r->>'score');
  ASSERT (v_r->>'passed')::boolean = true, 'sponsor-only (4) passes at boundary';

  -- zero-signal -> 0 -> declines with all four reasons
  UPDATE public.fda_assets SET breakthrough_designation=false, priority_review=false,
      first_time_sponsor=false WHERE id=v_id;
  v_r := public.bc_pregate_score_sql(v_id);
  ASSERT (v_r->>'score')::numeric = 0, 'zero-signal score=0';
  ASSERT (v_r->>'passed')::boolean = false, 'zero-signal declines';
  ASSERT v_r->'reasons' ? 'no_priority_review', 'reason: priority';
  ASSERT v_r->'reasons' ? 'class_precedent_unknown', 'reason: class';

  -- not yet enriched -> fail-open (stub passes; scored once enriched)
  UPDATE public.fda_assets SET designations_enriched_at=NULL WHERE id=v_id;
  v_r := public.bc_pregate_score_sql(v_id);
  ASSERT v_r->>'enrichment_state' = 'stub', 'null enriched -> stub';
  ASSERT v_r->'reasons' ? 'enrichment_pending_fail_open', 'stub reason: enrichment_pending_fail_open';
  ASSERT (v_r->>'passed')::boolean = true, 'stub fails open (passes)';

  -- unknown asset -> fail-open (unavailable passes)
  v_r := public.bc_pregate_score_sql('00000000-0000-0000-0000-000000000000');
  ASSERT v_r->>'enrichment_state' = 'unavailable', 'missing asset -> unavailable';
  ASSERT v_r->'reasons' ? 'enrichment_unavailable_fail_open', 'unavailable reason';
  ASSERT (v_r->>'passed')::boolean = true, 'unavailable fails open (passes)';

  RAISE NOTICE 'bc_pregate_score_sql_smoke: all assertions passed';
END $$;

ROLLBACK;
