-- Conan — defense-in-depth gate on watch→active for routine_declined candidates.
--
-- Background
-- ----------
-- The watch→active promotion rule (watch_active_promotion.md, 2026-04-27)
-- requires `extensions->>'routine_declined' IS DISTINCT FROM 'true'` as a
-- promotion gate. The spec is enforced at TWO call sites today:
--   1. thesis_writer.md step 7 — initial state on UPSERT.
--   2. candidate_aging.md §3 Stage A — daily sweep.
--
-- Both are skill-level (Cowork operator) and easy to forget. On
-- 2026-05-13 the daily candidate_aging Stage A run promoted FOUR
-- routine_declined candidates (LNTH, AG1, CMRC, HON) to state='active'
-- via candidate_transition_apply because the WHERE clause omitted the
-- routine_declined predicate. Five sets of pre-edge promotion emails
-- subsequently leaked (separate fanout fix in this same change set).
--
-- Adding "more guidance" to the skill spec won't fix evaluator drift —
-- the same observation that drove the 2026-05-08 AXSM-class fix. So we
-- enforce the gate in the database: any caller of
-- `candidate_transition_apply` requesting a watch→active transition is
-- refused when `extensions->>'routine_declined' = 'true'`, regardless of
-- source. To override, the operator must clear the flag first (which is
-- the spec's stated workflow anyway).
--
-- Scope of the gate
-- -----------------
-- ONLY watch→active. Other transitions (active→killed, active→delivered,
-- watch→killed for aged-out, active→watch for stale/elapsed catalyst,
-- etc.) are unaffected.
--
-- Caller-facing contract
-- ----------------------
-- A refused call returns the same `applied=false` envelope shape the RPC
-- already uses for the no-op-on-same-state branch:
--   {
--     "applied": false,
--     "candidate_id": <uuid>,
--     "from_state": "watch",
--     "to_state": "active",
--     "reason": "routine_declined_gate"
--   }
-- No `candidate_events` row is written; no exception is raised. Callers
-- check `applied` (already true of the existing no-state-change branch).

CREATE OR REPLACE FUNCTION public.candidate_transition_apply(
  p_candidate_id uuid,
  p_new_state candidate_state,
  p_reason text,
  p_source text,
  p_outcome_type text DEFAULT NULL,
  p_outcome_notes text DEFAULT NULL,
  p_triggered_kill_id text DEFAULT NULL,
  p_payload jsonb DEFAULT '{}'::jsonb
) RETURNS jsonb
LANGUAGE plpgsql
AS $$
DECLARE
  v_prev_state candidate_state;
  v_routine_declined text;
  v_event_id uuid;
  v_outcome_id uuid;
BEGIN
  SELECT state, extensions->>'routine_declined'
  INTO v_prev_state, v_routine_declined
  FROM candidates
  WHERE id = p_candidate_id
  FOR UPDATE;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'candidate_transition_apply: candidate % not found', p_candidate_id;
  END IF;

  IF v_prev_state = p_new_state THEN
    RETURN jsonb_build_object(
      'applied', false,
      'candidate_id', p_candidate_id,
      'from_state', v_prev_state,
      'to_state', p_new_state,
      'reason', 'no_state_change'
    );
  END IF;

  -- routine_declined gate (2026-05-14): watch -> active only.
  -- See watch_active_promotion.md + the 2026-05-13 promotion incident.
  IF v_prev_state = 'watch'
     AND p_new_state = 'active'
     AND v_routine_declined = 'true'
  THEN
    RETURN jsonb_build_object(
      'applied', false,
      'candidate_id', p_candidate_id,
      'from_state', v_prev_state,
      'to_state', p_new_state,
      'reason', 'routine_declined_gate'
    );
  END IF;

  UPDATE candidates
  SET state = p_new_state,
      updated_at = now()
  WHERE id = p_candidate_id;

  INSERT INTO candidate_events (candidate_id, event_type, payload)
  VALUES (
    p_candidate_id,
    'state_changed',
    jsonb_build_object(
      'from', v_prev_state,
      'to', p_new_state,
      'reason', p_reason,
      'source', p_source
    )
    || COALESCE(p_payload, '{}'::jsonb)
    || CASE
         WHEN p_triggered_kill_id IS NOT NULL
           THEN jsonb_build_object('triggered_kill_id', p_triggered_kill_id)
         ELSE '{}'::jsonb
       END
  )
  RETURNING id INTO v_event_id;

  IF p_outcome_type IS NOT NULL THEN
    INSERT INTO outcomes (candidate_id, outcome_type, notes)
    VALUES (p_candidate_id, p_outcome_type, p_outcome_notes)
    RETURNING id INTO v_outcome_id;
  END IF;

  RETURN jsonb_build_object(
    'applied', true,
    'candidate_id', p_candidate_id,
    'from_state', v_prev_state,
    'to_state', p_new_state,
    'candidate_event_id', v_event_id,
    'outcome_id', v_outcome_id
  );
END;
$$;

COMMENT ON FUNCTION public.candidate_transition_apply(
  uuid, candidate_state, text, text, text, text, text, jsonb
) IS
  'Atomic candidate lifecycle transition helper. Updates candidates.state, appends a '
  'state_changed candidate_event, and optionally appends an outcomes row for terminal '
  'transitions. Intended for deterministic service-role actors such as pre_edge_monitor '
  'and the candidate_aging Stage A sweep. Refuses watch->active transitions when the '
  'candidate row carries extensions.routine_declined=true (returns applied=false, '
  'reason=routine_declined_gate); operator must clear the flag first.';
