-- P0 #6: widen public.dashboard_candidate_set_state to allow operator-driven
-- transitions to 'delivered', and to allow source state 'active' (not just
-- 'watch') so operators can resolve stuck-active candidates.
--
-- Background: the original RPC at 20260503090000_dashboard_operator_workflow.sql
-- restricted p_new_state IN ('active','killed') AND v_current_state='watch',
-- which left an 'active' candidate (e.g. AXSM 2026-04-30 PDUFA, no inbound
-- signal) unreachable from the dashboard. Operator had no way to mark a
-- delivered outcome.
--
-- New surface:
--   - p_new_state IN ('active','watch','killed','delivered')
--   - source state must be 'watch' or 'active' (terminal states are dead-ends)
--   - active <-> watch are reversible operator transitions
--   - new optional p_evidence_url + p_realized_return parameters carry
--     audit context into candidate_events.payload and onto outcomes
--
-- Backward compatibility: callers using the 4-arg form still work because
-- p_evidence_url and p_realized_return default to NULL.
--
-- Future: P2 will add a purpose-built dashboard_candidate_resolve RPC that
-- requires evidence + realized_return for terminal transitions; this is the
-- transitional widening to unstick AXSM-class candidates today.

-- Drop the legacy 4-arg signature so a 4-arg call can't accidentally resolve
-- to the old definition (still restricting to active|killed and watch-only).
-- Postgres allows function overloading by signature, so CREATE OR REPLACE
-- on the new 6-arg form would otherwise leave the old one alive alongside it.
DROP FUNCTION IF EXISTS public.dashboard_candidate_set_state(uuid, public.candidate_state, text, text);

CREATE OR REPLACE FUNCTION public.dashboard_candidate_set_state(
  p_candidate_id uuid,
  p_new_state public.candidate_state,
  p_reason text,
  p_note text DEFAULT NULL,
  p_evidence_url text DEFAULT NULL,
  p_realized_return numeric DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_actor uuid := auth.uid();
  v_current_state public.candidate_state;
  v_result jsonb;
  v_outcome_type text;
  v_payload jsonb;
BEGIN
  IF v_actor IS NULL THEN
    RAISE EXCEPTION 'dashboard_candidate_set_state: authentication required';
  END IF;

  IF p_new_state NOT IN ('active', 'watch', 'killed', 'delivered') THEN
    RAISE EXCEPTION 'dashboard_candidate_set_state: new_state %, expected active|watch|killed|delivered', p_new_state;
  END IF;

  SELECT state
  INTO v_current_state
  FROM public.candidates
  WHERE id = p_candidate_id
  FOR UPDATE;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'dashboard_candidate_set_state: candidate % not found', p_candidate_id;
  END IF;

  IF v_current_state NOT IN ('watch', 'active') THEN
    RAISE EXCEPTION 'dashboard_candidate_set_state: candidate % has terminal state %, no operator transitions allowed', p_candidate_id, v_current_state;
  END IF;

  IF p_new_state IN ('killed', 'delivered') THEN
    v_outcome_type := p_new_state::text;
  END IF;

  v_payload := jsonb_build_object(
    'user_id', v_actor,
    'note', p_note
  );
  IF p_evidence_url IS NOT NULL AND p_evidence_url <> '' THEN
    v_payload := v_payload || jsonb_build_object('evidence_url', p_evidence_url);
  END IF;
  IF p_realized_return IS NOT NULL THEN
    v_payload := v_payload || jsonb_build_object('realized_return', p_realized_return);
  END IF;

  v_result := public.candidate_transition_apply(
    p_candidate_id,
    p_new_state,
    COALESCE(NULLIF(p_reason, ''), 'dashboard_review'),
    'dashboard',
    v_outcome_type,
    p_note,
    NULL,
    v_payload
  );

  -- If this is a terminal transition with realized_return, persist on outcomes.
  -- candidate_transition_apply inserts the outcome row but doesn't accept
  -- realized_return; patch it on the most-recent matching outcome.
  IF p_realized_return IS NOT NULL AND p_new_state IN ('killed', 'delivered') THEN
    UPDATE public.outcomes
    SET realized_return = p_realized_return
    WHERE id = (
      SELECT id FROM public.outcomes
      WHERE candidate_id = p_candidate_id
      ORDER BY created_at DESC
      LIMIT 1
    );
  END IF;

  INSERT INTO public.operator_actions (
    actor_id, action_type, target_type, target_id, candidate_id, note, payload
  )
  VALUES (
    v_actor,
    'candidate_set_state',
    'candidate',
    p_candidate_id::text,
    p_candidate_id,
    p_note,
    jsonb_build_object(
      'new_state', p_new_state,
      'reason', p_reason,
      'evidence_url', p_evidence_url,
      'realized_return', p_realized_return,
      'result', v_result
    )
  );

  RETURN v_result;
END;
$$;
