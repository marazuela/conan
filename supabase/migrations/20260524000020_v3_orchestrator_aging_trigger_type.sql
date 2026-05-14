-- 20260524000020_v3_orchestrator_aging_trigger_type.sql
-- Extends CHECK enums to support the v3 aging path.
-- Plan ref: /Users/Pico/.claude/plans/plan-it-thoroughly-tingly-fountain.md (M3)
--
-- 1. orchestrator_runs.trigger_type adds 'aging_recheck' (single-shot ensemble_n=1
--    runs enqueued by bulk_orchestrator_run when an asset rolls to kill_pending).
-- 2. convergence_assessments.trigger_type mirrors (logging the cause).
-- 3. fda_agent_reviews.agent_kind adds 'aging_review' (Cowork skill writes a row
--    when extracted_facts misses a kill_condition pattern — flags an extractor gap
--    while still allowing Gate 1 to pass via raw-doc regex fallback).
-- 4. premortem_assessments.is_declined boolean carries the v2 `decline` verdict
--    semantics without churning the existing verdict CHECK. Remap inside the
--    orchestrator (Stage 3 prompt): challenger_verdict=decline → is_declined=true,
--    verdict remains the survives/weakened/falsified rollup of the failure_modes.

-- ============================================================================
-- 1. orchestrator_runs.trigger_type extension
-- ============================================================================

ALTER TABLE public.orchestrator_runs
  DROP CONSTRAINT IF EXISTS orchestrator_runs_trigger_type_check;

ALTER TABLE public.orchestrator_runs
  ADD CONSTRAINT orchestrator_runs_trigger_type_check
  CHECK (trigger_type IN
    ('new_doc','cross_source','scheduled','operator_refresh','market_move',
     'tier2_escalation','backtest','manual','aging_recheck'));

-- ============================================================================
-- 2. convergence_assessments.trigger_type extension
-- ============================================================================

ALTER TABLE public.convergence_assessments
  DROP CONSTRAINT IF EXISTS convergence_assessments_trigger_type_check;

ALTER TABLE public.convergence_assessments
  ADD CONSTRAINT convergence_assessments_trigger_type_check
  CHECK (trigger_type IN
    ('new_doc','cross_source','scheduled','operator_refresh','market_move',
     'tier2_escalation','backtest','manual','aging_recheck'));

-- ============================================================================
-- 3. fda_agent_reviews.agent_kind extension
-- ============================================================================

ALTER TABLE public.fda_agent_reviews
  DROP CONSTRAINT IF EXISTS fda_agent_reviews_agent_kind_check;

ALTER TABLE public.fda_agent_reviews
  ADD CONSTRAINT fda_agent_reviews_agent_kind_check
  CHECK (agent_kind IN
    ('medical','regulatory','microstructure',
     'literature','competitive','ic_memo','aging_review'));

-- Also extend the inline RPC guard so dashboard refresh actions for the new kind
-- do not hit "expected medical|regulatory|microstructure|literature|competitive|ic_memo".
-- Preserves the live jsonb return shape from 20260510000010_v3_stream6_safety_and_cleanup.sql.
CREATE OR REPLACE FUNCTION public.fda_event_request_specialist_refresh(
  p_event_id uuid,
  p_agent_kind text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_actor uuid := auth.uid();
  v_review_id uuid;
  v_snapshot_hash text;
BEGIN
  IF v_actor IS NULL THEN
    RAISE EXCEPTION 'fda_event_request_specialist_refresh: authentication required';
  END IF;
  IF p_agent_kind NOT IN (
    'medical','regulatory','microstructure',
    'literature','competitive','ic_memo','aging_review'
  ) THEN
    RAISE EXCEPTION 'fda_event_request_specialist_refresh: agent_kind %, expected medical|regulatory|microstructure|literature|competitive|ic_memo|aging_review', p_agent_kind;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM public.fda_regulatory_events WHERE id = p_event_id) THEN
    RAISE EXCEPTION 'fda_event_request_specialist_refresh: event % not found', p_event_id;
  END IF;

  v_snapshot_hash := 'manual:' || encode(gen_random_bytes(8), 'hex');

  INSERT INTO public.fda_agent_reviews (
    event_id, agent_kind, version, snapshot_hash, status
  )
  VALUES (
    p_event_id, p_agent_kind, 'pending', v_snapshot_hash, 'queued'
  )
  RETURNING id INTO v_review_id;

  INSERT INTO public.operator_actions (
    actor_id, action_type, target_type, target_id, payload
  )
  VALUES (
    v_actor,
    'fda_event_request_specialist_refresh',
    'fda_event',
    p_event_id::text,
    jsonb_build_object(
      'agent_kind', p_agent_kind,
      'review_id', v_review_id,
      'snapshot_hash', v_snapshot_hash
    )
  );

  RETURN jsonb_build_object(
    'applied', true,
    'event_id', p_event_id,
    'agent_kind', p_agent_kind,
    'review_id', v_review_id,
    'status', 'queued'
  );
END;
$$;

-- ============================================================================
-- 4. premortem_assessments.is_declined
-- ============================================================================

ALTER TABLE public.premortem_assessments
  ADD COLUMN IF NOT EXISTS is_declined boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN public.premortem_assessments.is_declined IS
  'v3 challenger_verdict=decline carrier. v2 thesis_challenger had four '
  'verdicts (confirm/challenge/kill/decline). The first three map onto the '
  'existing verdict CHECK (confirm->survives, challenge->weakened, '
  'kill->falsified). `decline` (challenger refused to engage; signal does '
  'not support a thesis) sets this flag with verdict left at the rollup of '
  'failure_modes. Avoids churning verdict CHECK on live rows.';
