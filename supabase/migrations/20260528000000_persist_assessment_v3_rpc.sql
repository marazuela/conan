-- 20260528000000_persist_assessment_v3_rpc.sql
-- Wave 4 deep-fix Phase B — atomic Stage 10 persistence.
--
-- Why:
--   Today stage_10_persist (orchestrator_runtime/runtime.py) writes the
--   parent convergence_assessments row + 4 child tables
--   (assessment_stage_metrics, hypothesis_enumeration, premortem_assessments,
--   post_mortem_queue) via separate REST calls. On a mid-write failure the
--   caller DELETEs the parent (relying on CASCADE FKs), but that is two
--   round-trips, not one transaction — a network blip on the DELETE leaves
--   an orphan parent.  Also: a retry of the same orchestrator_run_id produces
--   a duplicate convergence_assessments row, and the existing
--   `superseded_at` column is never written, so Wave 1.2's prior-assessments
--   filter degenerates to "all-N-rows are live".
--
-- This migration adds:
--   (a) convergence_assessments.orchestrator_run_id  uuid UNIQUE
--       — idempotency key. Re-driving the same orchestrator_runs.id finds the
--       existing assessment instead of inserting a duplicate.
--   (b) persist_assessment_v3(payload jsonb) RETURNS uuid
--       — one transaction, one entry point, all of Stage 10's writes.
--       Supersedes any prior live assessment for the same asset.
--
-- Backwards compat: the column is nullable so the OLD orchestrator deploy
-- (which doesn't populate it) keeps working. The new code populates it on
-- every call; once a week of runs has landed we can flip NOT NULL in a
-- follow-up migration.
--
-- Rollback path: DROP FUNCTION persist_assessment_v3(jsonb);
--                ALTER TABLE convergence_assessments DROP COLUMN orchestrator_run_id;

BEGIN;

-- ============================================================================
-- B.2 idempotency key column
-- ============================================================================

ALTER TABLE public.convergence_assessments
  ADD COLUMN IF NOT EXISTS orchestrator_run_id uuid
    REFERENCES public.orchestrator_runs(id) ON DELETE SET NULL;

-- UNIQUE constraint as a separate statement so we can name it cleanly + use
-- DEFERRABLE if the RPC ever needs it (today it doesn't).
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'convergence_assessments_orchestrator_run_id_key'
  ) THEN
    ALTER TABLE public.convergence_assessments
      ADD CONSTRAINT convergence_assessments_orchestrator_run_id_key
      UNIQUE (orchestrator_run_id);
  END IF;
END $$;

COMMENT ON COLUMN public.convergence_assessments.orchestrator_run_id IS
  'Wave 4 deep-fix Phase B.2 — idempotency key. Set by persist_assessment_v3 '
  'on every new assessment. Nullable for rows written before the RPC was '
  'introduced; flip NOT NULL after a week of backfill.';

CREATE INDEX IF NOT EXISTS convergence_assessments_orch_run_idx
  ON public.convergence_assessments(orchestrator_run_id)
  WHERE orchestrator_run_id IS NOT NULL;

-- ============================================================================
-- B.1 atomic RPC
-- ============================================================================
-- Single transaction. Order of operations:
--   1. Idempotency check  — return existing row if orchestrator_run_id matches
--   2. Supersede chain    — stamp prior live assessment for this asset
--   3. INSERT convergence_assessments
--   4. INSERT assessment_stage_metrics rows
--   5. INSERT hypothesis_enumeration rows
--   6. INSERT premortem_assessments rows
--   7. INSERT post_mortem_queue stub
--   8. RETURN new assessment id
--
-- Payload shape:
--   {
--     "orchestrator_run_id": "<uuid>",
--     "assessment": { <all convergence_assessments columns except id+created_at> },
--     "stage_metrics": [ { <one per row> } ],
--     "hypotheses": [ { <one per hypothesis> } ],            -- nullable
--     "premortem_verdicts": [ { <one per verdict> } ],       -- nullable
--     "post_mortem_stub": { <single object> }                -- nullable
--   }
--
-- Caller (runtime.py) constructs the payload from the existing ctx + run state.
-- Postgres rolls back the entire transaction on any constraint or check
-- failure — no partial-write rescue logic needed in the application.

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

  -- (1) idempotency — if the orchestrator_run_id already has an assessment,
  --     return that id. Caller treats the second call as a no-op success.
  IF v_orchestrator_run_id IS NOT NULL THEN
    SELECT id INTO v_existing_id
    FROM public.convergence_assessments
    WHERE orchestrator_run_id = v_orchestrator_run_id;
    IF v_existing_id IS NOT NULL THEN
      RETURN v_existing_id;
    END IF;
  END IF;

  -- (2) supersede prior live assessment for this asset BEFORE inserting the
  --     new row. The placeholder NULL on superseded_by gets stamped after
  --     INSERT (we don't know the new id yet inside the UPDATE WHERE).
  UPDATE public.convergence_assessments
  SET superseded_at = now()
  WHERE asset_id = v_asset_id
    AND superseded_at IS NULL;

  -- (3) INSERT parent. jsonb_populate_record handles the column mapping
  --     defensively — unknown keys in the jsonb are ignored, missing keys
  --     default to the table default or NULL. NEW.id is generated.
  INSERT INTO public.convergence_assessments
  SELECT (jsonb_populate_record(
      NULL::public.convergence_assessments,
      v_assessment
        || jsonb_build_object('orchestrator_run_id', v_orchestrator_run_id)
    )).*
  RETURNING id INTO v_assessment_id;

  -- (3b) backfill superseded_by on the rows we just stamped in step (2) so
  --      the supersedence chain is queryable.
  UPDATE public.convergence_assessments
  SET superseded_by = v_assessment_id
  WHERE asset_id = v_asset_id
    AND superseded_at IS NOT NULL
    AND superseded_by IS NULL
    AND id <> v_assessment_id;

  -- (4) assessment_stage_metrics rows
  IF jsonb_array_length(v_stage_metrics) > 0 THEN
    FOR v_row IN SELECT value FROM jsonb_array_elements(v_stage_metrics)
    LOOP
      INSERT INTO public.assessment_stage_metrics
      SELECT (jsonb_populate_record(
          NULL::public.assessment_stage_metrics,
          v_row || jsonb_build_object('assessment_id', v_assessment_id)
        )).*;
    END LOOP;
  END IF;

  -- (5) hypothesis_enumeration rows (one per hypothesis)
  IF jsonb_array_length(v_hypotheses) > 0 THEN
    FOR v_row IN SELECT value FROM jsonb_array_elements(v_hypotheses)
    LOOP
      INSERT INTO public.hypothesis_enumeration
      SELECT (jsonb_populate_record(
          NULL::public.hypothesis_enumeration,
          v_row || jsonb_build_object('assessment_id', v_assessment_id)
        )).*;
    END LOOP;
  END IF;

  -- (6) premortem_assessments rows (one per verdict). Composite FK to
  --     hypothesis_enumeration(assessment_id, hypothesis_id) is DEFERRABLE
  --     INITIALLY DEFERRED so the order between (5) and (6) is flexible
  --     inside this transaction.
  IF jsonb_array_length(v_premortem) > 0 THEN
    FOR v_row IN SELECT value FROM jsonb_array_elements(v_premortem)
    LOOP
      INSERT INTO public.premortem_assessments
      SELECT (jsonb_populate_record(
          NULL::public.premortem_assessments,
          v_row || jsonb_build_object('assessment_id', v_assessment_id)
        )).*;
    END LOOP;
  END IF;

  -- (7) post_mortem_queue stub (one row). Wave 4.3 catalyst_resolution_marker
  --     lives inside the payload; the column was added in
  --     20260526000010_add_post_mortem_catalyst_marker.sql.
  IF v_post_mortem IS NOT NULL THEN
    INSERT INTO public.post_mortem_queue
    SELECT (jsonb_populate_record(
        NULL::public.post_mortem_queue,
        v_post_mortem || jsonb_build_object('assessment_id', v_assessment_id)
      )).*;
  END IF;

  -- (8) return the new id
  RETURN v_assessment_id;
END;
$$;

COMMENT ON FUNCTION public.persist_assessment_v3(jsonb) IS
  'Wave 4 deep-fix Phase B.1 — atomic Stage 10 writeback. Single transaction '
  'over parent + stage_metrics + hypothesis_enumeration + premortem_assessments '
  '+ post_mortem_queue. Idempotent on orchestrator_run_id (retry returns the '
  'same assessment id). Supersedes prior live assessment for the same asset '
  '(superseded_at + superseded_by stamped). Postgres handles rollback on any '
  'constraint failure — no application-side cleanup required.';

-- ============================================================================
-- Permissions — runtime.py uses the service_role key, which already has full
-- access. Granting EXECUTE to authenticated for completeness so the dashboard's
-- "Refresh" RPC path can call it directly without an intermediate Modal hop.
-- ============================================================================

GRANT EXECUTE ON FUNCTION public.persist_assessment_v3(jsonb)
  TO service_role, authenticated;

COMMIT;
