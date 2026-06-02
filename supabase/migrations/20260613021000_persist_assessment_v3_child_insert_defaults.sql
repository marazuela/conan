-- 20260613021000_persist_assessment_v3_child_insert_defaults.sql
--
-- Reconciles a LIVE hotfix (applied 2026-06-01 ~13:27 UTC via CREATE OR REPLACE)
-- back to disk. Extends the NOT-NULL defensive-defaults treatment from the
-- PARENT insert to ALL FOUR CHILD inserts in persist_assessment_v3.
--
-- Root cause (same trap, one level down):
--   persist_assessment_v3 builds every INSERT as
--     INSERT INTO <tbl> SELECT (jsonb_populate_record(NULL::<tbl>, payload)).*
--   jsonb_populate_record emits an explicit NULL for any column whose key is
--   absent from the jsonb. INSERT ... SELECT (record).* then carries that NULL
--   as an explicit value, which BYPASSES the column DEFAULT (a DEFAULT only
--   fires when the column is omitted from the INSERT, not when NULL is supplied).
--   The prior fix (20260601125914 / PR #174) defended only the PARENT
--   convergence_assessments insert (id/created_at/tier/constitutional_retries).
--   Because the parent insert always failed FIRST, it masked the identical bug
--   on the child inserts. Once the parent fix landed, the first successful
--   end-to-end run failed with:
--     null value in column "id" of relation "assessment_stage_metrics"
--     violates not-null constraint
--   and would have walked the same chain on hypothesis_enumeration /
--   premortem_assessments / post_mortem_queue.
--
-- Fix:
--   Merge each child table's NOT NULL-with-default columns (id, created_at, and
--   the per-table token/status/collection/flag defaults) AHEAD of the caller row
--   payload, so `defaults || v_row || {assessment_id}` guarantees those columns
--   are present. Caller-supplied values win (right side of || overrides), and
--   assessment_id is forced last.
--
-- Timestamp note:
--   Stamped 20260613021000 (after the parent-fix files 20260601125914 AND the
--   still-present future-dated 20260613020000) so this CREATE OR REPLACE applies
--   LAST and cannot be reverted by either parent-only migration on a fresh
--   `supabase db push`. The function body below is the COMPLETE definition
--   (parent + child defenses), so the end state is correct regardless of which
--   parent-fix migration applied before it.
--
-- Validation (live, pre-reconciliation):
--   assessment 5181b25d-166b-4d38-b8f5-1765f4681912 persisted 2026-06-01 13:45 UTC
--   with 5 assessment_stage_metrics child rows — first success since 2026-05-25
--   and first orchestrator_version_v4=true row on live.
--
-- Rollback: re-apply 20260601125914 (parent-only). Doing so re-introduces the
--   child-insert 23502 for any caller whose child rows omit id/created_at.

BEGIN;

CREATE OR REPLACE FUNCTION public.persist_assessment_v3(payload jsonb)
RETURNS uuid
LANGUAGE plpgsql
AS $$
DECLARE
  v_orchestrator_run_id uuid;
  v_asset_id            uuid;
  v_assessment_id       uuid;
  v_existing_id         uuid;
  v_assessment          jsonb;
  v_stage_metrics       jsonb;
  v_hypotheses          jsonb;
  v_premortem           jsonb;
  v_post_mortem         jsonb;
  v_row                 jsonb;
BEGIN
  v_orchestrator_run_id := (payload->>'orchestrator_run_id')::uuid;
  v_assessment          := payload->'assessment';
  v_stage_metrics       := COALESCE(payload->'stage_metrics', '[]'::jsonb);
  v_hypotheses          := COALESCE(payload->'hypotheses', '[]'::jsonb);
  v_premortem           := COALESCE(payload->'premortem_verdicts', '[]'::jsonb);
  v_post_mortem         := payload->'post_mortem_stub';

  IF v_assessment IS NULL THEN
    RAISE EXCEPTION 'persist_assessment_v3: payload.assessment is required';
  END IF;
  v_asset_id := (v_assessment->>'asset_id')::uuid;
  IF v_asset_id IS NULL THEN
    RAISE EXCEPTION 'persist_assessment_v3: assessment.asset_id is required';
  END IF;

  -- (1) idempotency on orchestrator_run_id
  IF v_orchestrator_run_id IS NOT NULL THEN
    SELECT id INTO v_existing_id
    FROM public.convergence_assessments
    WHERE orchestrator_run_id = v_orchestrator_run_id;
    IF v_existing_id IS NOT NULL THEN
      RETURN v_existing_id;
    END IF;
  END IF;

  -- (2) supersede prior live assessment for this asset
  UPDATE public.convergence_assessments
  SET superseded_at = now()
  WHERE asset_id = v_asset_id
    AND superseded_at IS NULL;

  -- (3) parent insert — defensive defaults (id/created_at/tier/constitutional_retries)
  INSERT INTO public.convergence_assessments
  SELECT (jsonb_populate_record(
      NULL::public.convergence_assessments,
      jsonb_build_object(
        'id', gen_random_uuid(),
        'created_at', now(),
        'tier', 1,
        'constitutional_retries', 0
      )
      || v_assessment
      || jsonb_build_object('orchestrator_run_id', v_orchestrator_run_id)
    )).*
  RETURNING id INTO v_assessment_id;

  -- (3b) backfill superseded_by chain
  UPDATE public.convergence_assessments
  SET superseded_by = v_assessment_id
  WHERE asset_id = v_asset_id
    AND superseded_at IS NOT NULL
    AND superseded_by IS NULL
    AND id <> v_assessment_id;

  -- child inserts: defend id/created_at + collection/status/flag defaults.
  -- jsonb_populate_record emits explicit NULL for omitted keys (bypassing column
  -- DEFAULTs); caller v_row overrides where it supplies a value.

  -- (4) assessment_stage_metrics
  IF jsonb_array_length(v_stage_metrics) > 0 THEN
    FOR v_row IN SELECT value FROM jsonb_array_elements(v_stage_metrics)
    LOOP
      INSERT INTO public.assessment_stage_metrics
      SELECT (jsonb_populate_record(
          NULL::public.assessment_stage_metrics,
          jsonb_build_object(
            'id', gen_random_uuid(),
            'created_at', now(),
            'input_tokens', 0, 'output_tokens', 0, 'thinking_tokens', 0,
            'cache_read_tokens', 0, 'cache_creation_tokens', 0,
            'cost_usd', 0, 'latency_ms', 0, 'status', 'completed'
          )
          || v_row
          || jsonb_build_object('assessment_id', v_assessment_id)
        )).*;
    END LOOP;
  END IF;

  -- (5) hypothesis_enumeration
  IF jsonb_array_length(v_hypotheses) > 0 THEN
    FOR v_row IN SELECT value FROM jsonb_array_elements(v_hypotheses)
    LOOP
      INSERT INTO public.hypothesis_enumeration
      SELECT (jsonb_populate_record(
          NULL::public.hypothesis_enumeration,
          jsonb_build_object(
            'id', gen_random_uuid(),
            'created_at', now(),
            'supporting_fact_ids', '[]'::jsonb,
            'contradicting_fact_ids', '[]'::jsonb,
            'deliver_conditions', '[]'::jsonb
          )
          || v_row
          || jsonb_build_object('assessment_id', v_assessment_id)
        )).*;
    END LOOP;
  END IF;

  -- (6) premortem_assessments
  IF jsonb_array_length(v_premortem) > 0 THEN
    FOR v_row IN SELECT value FROM jsonb_array_elements(v_premortem)
    LOOP
      INSERT INTO public.premortem_assessments
      SELECT (jsonb_populate_record(
          NULL::public.premortem_assessments,
          jsonb_build_object(
            'id', gen_random_uuid(),
            'created_at', now(),
            'disconfirming_searches', '[]'::jsonb,
            'update_triggers', '[]'::jsonb,
            'is_declined', false
          )
          || v_row
          || jsonb_build_object('assessment_id', v_assessment_id)
        )).*;
    END LOOP;
  END IF;

  -- (7) post_mortem_queue stub
  IF v_post_mortem IS NOT NULL THEN
    INSERT INTO public.post_mortem_queue
    SELECT (jsonb_populate_record(
        NULL::public.post_mortem_queue,
        jsonb_build_object(
          'id', gen_random_uuid(),
          'created_at', now(),
          'status', 'pending'
        )
        || v_post_mortem
        || jsonb_build_object('assessment_id', v_assessment_id)
      )).*;
  END IF;

  RETURN v_assessment_id;
END;
$$;

COMMENT ON FUNCTION public.persist_assessment_v3(jsonb) IS
  'Wave 4 atomic Stage 10 writeback. Idempotent on orchestrator_run_id; supersedes prior live assessment for the asset. Defensive NOT-NULL defaults on parent (id/created_at/tier/constitutional_retries) AND all 4 child inserts (id/created_at + per-table token/status/collection/flag defaults) — jsonb_populate_record emits explicit NULL for omitted keys, bypassing column DEFAULTs. 20260613021000: extends defense to child inserts after the parent-only fix (20260601125914 / PR #174) exposed child NULL-id 23502.';

GRANT EXECUTE ON FUNCTION public.persist_assessment_v3(jsonb)
  TO service_role, authenticated;

COMMIT;
