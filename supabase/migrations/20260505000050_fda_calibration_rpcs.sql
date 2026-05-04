-- Conan v2 — Phase 6 calibration RPCs.
--
-- Three SECURITY DEFINER functions:
--
--   1. fda_calibration_load(p_lookback_days int)
--      SECURITY DEFINER read accessor. Returns the labeled dataset the
--      modal_workers/scripts/fda_calibration.py CLI consumes. Joins
--      catalyst_universe (ground truth) → fda_assets → fda_regulatory_events →
--      fda_event_features (latest snapshot before the event date — no peeking).
--      The script wraps this RPC instead of inline SQL so PostgREST query-string
--      handling stays simple.
--
--   2. fda_calibration_activate(p_version, p_note)
--      Operator-driven activation. Supersedes the current effective row in the
--      same scope, flips effective_at on the named version, marks the matching
--      fda_calibration_runs row activated=true, audits via operator_actions.
--
--   3. fda_calibration_rollback(p_version, p_note)
--      Reverses an activation: supersedes the active row, un-supersedes the
--      most recently superseded prior row in the same scope, sets the
--      calibration_runs row's rolled_back_at, audits.
--
-- All three require auth.uid() (no service-role-only writes here — the
-- calibration script runs under service-role for the load + insert paths,
-- but operators run activate/rollback under their authenticated session).

-- ============================================================
-- 1. fda_calibration_load
-- ============================================================
CREATE OR REPLACE FUNCTION public.fda_calibration_load(
  p_lookback_days int DEFAULT 365
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_actor uuid := auth.uid();
  v_rows jsonb;
BEGIN
  IF v_actor IS NULL THEN
    RAISE EXCEPTION 'fda_calibration_load: authentication required';
  END IF;
  IF p_lookback_days IS NULL OR p_lookback_days < 1 OR p_lookback_days > 3650 THEN
    RAISE EXCEPTION 'fda_calibration_load: lookback_days must be in [1, 3650]';
  END IF;

  SELECT COALESCE(jsonb_agg(row_to_json(r)), '[]'::jsonb) INTO v_rows
  FROM (
    SELECT
      cu.catalyst_type,
      cu.catalyst_date::text AS catalyst_date,
      cu.material_outcome,
      cu.realized_price_move,
      fef.fair_probability,
      fef.market_implied_probability,
      fef.shadow_score,
      fef.shadow_band,
      fef.event_id,
      re.event_type,
      a.ticker, a.drug_name, a.indication
    FROM public.catalyst_universe cu
    JOIN public.fda_assets a
      ON a.ticker = cu.ticker
     AND (cu.mic IS NULL OR a.mic IS NOT DISTINCT FROM cu.mic)
    JOIN public.fda_regulatory_events re
      ON re.asset_id = a.id
     AND re.event_date BETWEEN cu.catalyst_date - INTERVAL '60 days'
                           AND cu.catalyst_date + INTERVAL '60 days'
    JOIN public.fda_event_features fef
      ON fef.event_id = re.id
    WHERE cu.catalyst_type IN ('fda_approval','fda_crl','phase3_readout')
      AND cu.catalyst_date >= now() - (p_lookback_days || ' days')::interval
      AND cu.material_outcome IN ('yes','no')
      AND fef.snapshot_at < (cu.catalyst_date::timestamptz)
  ) r;

  RETURN v_rows;
END;
$$;

-- ============================================================
-- 2. fda_calibration_activate
-- ============================================================
CREATE OR REPLACE FUNCTION public.fda_calibration_activate(
  p_version text,
  p_note text DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_actor uuid := auth.uid();
  v_target public.fda_model_versions%ROWTYPE;
  v_prior_id uuid;
  v_calibration_id uuid;
BEGIN
  IF v_actor IS NULL THEN
    RAISE EXCEPTION 'fda_calibration_activate: authentication required';
  END IF;
  IF p_version IS NULL OR length(trim(p_version)) = 0 THEN
    RAISE EXCEPTION 'fda_calibration_activate: version is required';
  END IF;

  SELECT * INTO v_target FROM public.fda_model_versions
  WHERE version = p_version
  FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'fda_calibration_activate: version % not found', p_version;
  END IF;
  IF v_target.effective_at IS NOT NULL AND v_target.superseded_at IS NULL THEN
    RAISE EXCEPTION 'fda_calibration_activate: version % is already active', p_version;
  END IF;
  IF v_target.superseded_at IS NOT NULL THEN
    RAISE EXCEPTION 'fda_calibration_activate: version % is already superseded; use rollback to restore a prior version', p_version;
  END IF;

  -- Supersede any currently-active row in the same scope.
  UPDATE public.fda_model_versions
  SET superseded_at = now()
  WHERE scope = v_target.scope
    AND superseded_at IS NULL
    AND effective_at IS NOT NULL
    AND id <> v_target.id
  RETURNING id INTO v_prior_id;

  -- Flip the target into the effective slot.
  UPDATE public.fda_model_versions
  SET effective_at = now()
  WHERE id = v_target.id;

  -- Mark the corresponding calibration run row activated.
  UPDATE public.fda_calibration_runs
  SET activated = true
  WHERE model_version_id = v_target.id
  RETURNING id INTO v_calibration_id;

  -- Audit row.
  INSERT INTO public.operator_actions (
    actor_id, action_type, target_type, target_id, note, payload
  )
  VALUES (
    v_actor,
    'fda_calibration_activate',
    'fda_model_version',
    v_target.id::text,
    p_note,
    jsonb_build_object(
      'version', p_version,
      'scope', v_target.scope,
      'prior_active_id', v_prior_id,
      'calibration_run_id', v_calibration_id
    )
  );

  RETURN jsonb_build_object(
    'applied', true,
    'version', p_version,
    'model_version_id', v_target.id,
    'scope', v_target.scope,
    'prior_active_id', v_prior_id
  );
END;
$$;

-- ============================================================
-- 3. fda_calibration_rollback
-- ============================================================
CREATE OR REPLACE FUNCTION public.fda_calibration_rollback(
  p_version text,
  p_note text DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_actor uuid := auth.uid();
  v_target public.fda_model_versions%ROWTYPE;
  v_prior_id uuid;
  v_prior_version text;
BEGIN
  IF v_actor IS NULL THEN
    RAISE EXCEPTION 'fda_calibration_rollback: authentication required';
  END IF;

  SELECT * INTO v_target FROM public.fda_model_versions
  WHERE version = p_version
  FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'fda_calibration_rollback: version % not found', p_version;
  END IF;
  IF v_target.effective_at IS NULL THEN
    RAISE EXCEPTION 'fda_calibration_rollback: version % was never activated', p_version;
  END IF;
  IF v_target.superseded_at IS NOT NULL THEN
    RAISE EXCEPTION 'fda_calibration_rollback: version % is already superseded', p_version;
  END IF;

  -- Find the most recently superseded prior version in the same scope to restore.
  SELECT id, version INTO v_prior_id, v_prior_version
  FROM public.fda_model_versions
  WHERE scope = v_target.scope
    AND id <> v_target.id
    AND superseded_at IS NOT NULL
  ORDER BY superseded_at DESC
  LIMIT 1
  FOR UPDATE;

  -- Supersede the active version.
  UPDATE public.fda_model_versions
  SET superseded_at = now()
  WHERE id = v_target.id;

  -- Restore the prior (if found). If no prior exists (rolling back the very
  -- first version), the scope is left without an active row — operators must
  -- run a fresh calibration to re-establish a baseline.
  IF v_prior_id IS NOT NULL THEN
    UPDATE public.fda_model_versions
    SET superseded_at = NULL,
        effective_at = now()
    WHERE id = v_prior_id;
  END IF;

  -- Mark the calibration_runs row as rolled back.
  UPDATE public.fda_calibration_runs
  SET rolled_back_at = now()
  WHERE model_version_id = v_target.id;

  INSERT INTO public.operator_actions (
    actor_id, action_type, target_type, target_id, note, payload
  )
  VALUES (
    v_actor,
    'fda_calibration_rollback',
    'fda_model_version',
    v_target.id::text,
    p_note,
    jsonb_build_object(
      'version', p_version,
      'scope', v_target.scope,
      'restored_version', v_prior_version,
      'restored_id', v_prior_id
    )
  );

  RETURN jsonb_build_object(
    'applied', true,
    'rolled_back_version', p_version,
    'restored_version', v_prior_version,
    'restored_id', v_prior_id
  );
END;
$$;

-- Lock down PUBLIC, grant to authenticated.
REVOKE ALL ON FUNCTION public.fda_calibration_load(int) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.fda_calibration_activate(text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.fda_calibration_rollback(text, text) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION public.fda_calibration_load(int) TO authenticated;
GRANT EXECUTE ON FUNCTION public.fda_calibration_activate(text, text) TO authenticated;
GRANT EXECUTE ON FUNCTION public.fda_calibration_rollback(text, text) TO authenticated;

-- service_role retains EXECUTE by default (it bypasses GRANT/REVOKE).
