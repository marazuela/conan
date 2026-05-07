-- Conan dashboard — v3 mutation RPCs.
--
-- Adds the asset-state and eval-case mutation RPCs the v3 dashboard server
-- actions in dashboard/lib/api/actions/* call into. All RPCs are SECURITY
-- DEFINER, require auth.uid(), and write to operator_actions for audit.
--
-- See plan: ~/.claude/plans/plan-a-dashboard-upgrade-effervescent-avalanche.md
-- See decision: DECISIONS.md D-035 (v3 dashboard visual & UX language lock).

-- ---------------------------------------------------------------------------
-- fda_asset_set_watch_priority
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.fda_asset_set_watch_priority(
  p_asset_id uuid,
  p_priority int,
  p_note text DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_actor uuid := auth.uid();
  v_prev int;
BEGIN
  IF v_actor IS NULL THEN
    RAISE EXCEPTION 'fda_asset_set_watch_priority: authentication required';
  END IF;
  IF p_priority < 1 OR p_priority > 5 THEN
    RAISE EXCEPTION 'fda_asset_set_watch_priority: priority must be 1..5 (got %)', p_priority;
  END IF;

  SELECT watch_priority INTO v_prev
    FROM public.fda_assets
   WHERE id = p_asset_id
   FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'fda_asset_set_watch_priority: asset % not found', p_asset_id;
  END IF;

  UPDATE public.fda_assets
     SET watch_priority = p_priority,
         updated_at = now()
   WHERE id = p_asset_id;

  INSERT INTO public.operator_actions (
    actor_id, action_type, target_type, target_id, note, payload
  ) VALUES (
    v_actor,
    'fda_asset_set_watch_priority',
    'fda_asset',
    p_asset_id::text,
    p_note,
    jsonb_build_object('from', v_prev, 'to', p_priority)
  );

  RETURN jsonb_build_object('asset_id', p_asset_id, 'from', v_prev, 'to', p_priority);
END$$;

GRANT EXECUTE ON FUNCTION public.fda_asset_set_watch_priority(uuid, int, text) TO authenticated;

-- ---------------------------------------------------------------------------
-- fda_asset_set_active
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.fda_asset_set_active(
  p_asset_id uuid,
  p_is_active bool,
  p_note text DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_actor uuid := auth.uid();
  v_prev bool;
BEGIN
  IF v_actor IS NULL THEN
    RAISE EXCEPTION 'fda_asset_set_active: authentication required';
  END IF;

  SELECT is_active INTO v_prev
    FROM public.fda_assets
   WHERE id = p_asset_id
   FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'fda_asset_set_active: asset % not found', p_asset_id;
  END IF;

  UPDATE public.fda_assets
     SET is_active = p_is_active,
         updated_at = now()
   WHERE id = p_asset_id;

  INSERT INTO public.operator_actions (
    actor_id, action_type, target_type, target_id, note, payload
  ) VALUES (
    v_actor,
    'fda_asset_set_active',
    'fda_asset',
    p_asset_id::text,
    p_note,
    jsonb_build_object('from', v_prev, 'to', p_is_active)
  );

  RETURN jsonb_build_object('asset_id', p_asset_id, 'from', v_prev, 'to', p_is_active);
END$$;

GRANT EXECUTE ON FUNCTION public.fda_asset_set_active(uuid, bool, text) TO authenticated;

-- ---------------------------------------------------------------------------
-- fda_asset_pin_reference_class
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.fda_asset_pin_reference_class(
  p_asset_id uuid,
  p_signature text,
  p_note text DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_actor uuid := auth.uid();
  v_prev text;
  v_known bool;
BEGIN
  IF v_actor IS NULL THEN
    RAISE EXCEPTION 'fda_asset_pin_reference_class: authentication required';
  END IF;
  IF p_signature IS NULL OR length(trim(p_signature)) = 0 THEN
    RAISE EXCEPTION 'fda_asset_pin_reference_class: signature required';
  END IF;

  SELECT EXISTS (
    SELECT 1 FROM public.reference_class_base_rates WHERE reference_class = p_signature
  ) INTO v_known;
  IF NOT v_known THEN
    RAISE EXCEPTION
      'fda_asset_pin_reference_class: reference class % not in base-rate table',
      p_signature;
  END IF;

  SELECT reference_class_signature INTO v_prev
    FROM public.fda_assets
   WHERE id = p_asset_id
   FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'fda_asset_pin_reference_class: asset % not found', p_asset_id;
  END IF;

  UPDATE public.fda_assets
     SET reference_class_signature = p_signature,
         updated_at = now()
   WHERE id = p_asset_id;

  INSERT INTO public.operator_actions (
    actor_id, action_type, target_type, target_id, note, payload
  ) VALUES (
    v_actor,
    'fda_asset_pin_reference_class',
    'fda_asset',
    p_asset_id::text,
    p_note,
    jsonb_build_object('from', v_prev, 'to', p_signature)
  );

  RETURN jsonb_build_object('asset_id', p_asset_id, 'from', v_prev, 'to', p_signature);
END$$;

GRANT EXECUTE ON FUNCTION public.fda_asset_pin_reference_class(uuid, text, text) TO authenticated;

-- ---------------------------------------------------------------------------
-- eval_case_open / eval_case_resolve
--
-- eval_harness has no `status` column today; we add a minimal column set to
-- track open/resolved transitions without disturbing the existing seed data.
-- This is additive and reversible.
-- ---------------------------------------------------------------------------
ALTER TABLE public.eval_harness
  ADD COLUMN IF NOT EXISTS opened_at timestamptz,
  ADD COLUMN IF NOT EXISTS resolved_at timestamptz,
  ADD COLUMN IF NOT EXISTS resolution_outcome jsonb;

CREATE INDEX IF NOT EXISTS eval_harness_unresolved_idx
  ON public.eval_harness(opened_at DESC)
  WHERE resolved_at IS NULL AND opened_at IS NOT NULL;

CREATE OR REPLACE FUNCTION public.eval_case_open(
  p_case_id uuid,
  p_note text DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_actor uuid := auth.uid();
BEGIN
  IF v_actor IS NULL THEN
    RAISE EXCEPTION 'eval_case_open: authentication required';
  END IF;

  UPDATE public.eval_harness
     SET opened_at = COALESCE(opened_at, now()),
         resolved_at = NULL,
         resolution_outcome = NULL
   WHERE id = p_case_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'eval_case_open: case % not found', p_case_id;
  END IF;

  INSERT INTO public.operator_actions (
    actor_id, action_type, target_type, target_id, note
  ) VALUES (
    v_actor, 'eval_case_open', 'eval_case', p_case_id::text, p_note
  );

  RETURN jsonb_build_object('case_id', p_case_id, 'opened_at', now());
END$$;

GRANT EXECUTE ON FUNCTION public.eval_case_open(uuid, text) TO authenticated;

CREATE OR REPLACE FUNCTION public.eval_case_resolve(
  p_case_id uuid,
  p_outcome jsonb,
  p_note text DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_actor uuid := auth.uid();
BEGIN
  IF v_actor IS NULL THEN
    RAISE EXCEPTION 'eval_case_resolve: authentication required';
  END IF;
  IF p_outcome IS NULL THEN
    RAISE EXCEPTION 'eval_case_resolve: outcome required';
  END IF;

  UPDATE public.eval_harness
     SET resolved_at = now(),
         resolution_outcome = p_outcome
   WHERE id = p_case_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'eval_case_resolve: case % not found', p_case_id;
  END IF;

  INSERT INTO public.operator_actions (
    actor_id, action_type, target_type, target_id, note, payload
  ) VALUES (
    v_actor, 'eval_case_resolve', 'eval_case', p_case_id::text, p_note, p_outcome
  );

  RETURN jsonb_build_object('case_id', p_case_id, 'resolved_at', now(), 'outcome', p_outcome);
END$$;

GRANT EXECUTE ON FUNCTION public.eval_case_resolve(uuid, jsonb, text) TO authenticated;

-- ---------------------------------------------------------------------------
-- Notes
--
-- - calibration_curve_pin / calibration_curve_rollback are NOT created here.
--   Use the existing public.fda_calibration_activate(p_version, p_note) and
--   public.fda_calibration_rollback(p_version, p_note) RPCs from
--   20260505000050_fda_calibration_rpcs.sql; the dashboard wraps those.
-- - All RPCs above are idempotent on identical inputs aside from operator_actions
--   side effects (one audit row per call).
-- ---------------------------------------------------------------------------
