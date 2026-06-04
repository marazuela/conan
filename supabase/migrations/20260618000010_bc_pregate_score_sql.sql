-- SQL mirror of the reactor pre-gate scorer (supabase/functions/reactor/bc-pregate.ts
-- scoreBcPregate). Used by the catalyst_proximity sweep (which enqueues by asset, not
-- by document, so it can't call the TS reactor) and by offline distribution/measurement.
--
-- PARITY CONTRACT: the additive weights here MUST stay in lockstep with
-- BC_PREGATE_WEIGHTS in bc-pregate.ts (breakthrough 6, first_time_sponsor 4,
-- priority_review 3, class_precedent * 5) and the enrichment_state short-circuits.
-- supabase/tests/bc_pregate_score_sql_smoke.sql pins the numbers.
--
-- Inputs come from the fda_assets columns hydrated by
-- enrich_fda_asset_designations.py; class_precedent from fda_class_precedent_base_rates
-- joined on the normalized (mechanism, indication).

CREATE OR REPLACE FUNCTION public.bc_pregate_score_sql(p_asset_id uuid)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
SET search_path TO 'public', 'pg_temp'
AS $function$
DECLARE
  v_asset            public.fda_assets%ROWTYPE;
  v_found            boolean := false;
  v_threshold_text   text;
  v_threshold        numeric := 4;   -- BC_PREGATE_DEFAULT_THRESHOLD
  v_class            numeric := 0;
  v_score            numeric := 0;
  v_reasons          text[] := ARRAY[]::text[];
  v_moa              text;
  v_ind              text;
BEGIN
  -- Threshold from config, mirroring configThreshold() (fallback on parse failure
  -- or negative value).
  SELECT value INTO v_threshold_text
    FROM public.internal_config WHERE key = 'bc_pregate_threshold';
  BEGIN
    IF v_threshold_text IS NOT NULL AND btrim(v_threshold_text) <> '' THEN
      v_threshold := v_threshold_text::numeric;
      IF v_threshold < 0 THEN v_threshold := 4; END IF;
    END IF;
  EXCEPTION WHEN others THEN
    v_threshold := 4;
  END;

  SELECT * INTO v_asset FROM public.fda_assets WHERE id = p_asset_id;
  v_found := FOUND;

  -- No asset row -> fail-open (we can't judge what we can't find; don't decline
  -- a possibly-real catalyst over a lookup miss).
  IF NOT v_found THEN
    RETURN jsonb_build_object(
      'score', 0, 'passed', true, 'enrichment_state', 'unavailable',
      'threshold', v_threshold, 'reasons', ARRAY['enrichment_unavailable_fail_open']);
  END IF;

  -- Not yet enriched -> fail-open. The gate only declines assets it has data to
  -- judge; un-enriched assets pass and are scored normally once the enricher runs.
  IF v_asset.designations_enriched_at IS NULL THEN
    RETURN jsonb_build_object(
      'score', 0, 'passed', true, 'enrichment_state', 'stub',
      'threshold', v_threshold, 'reasons', ARRAY['enrichment_pending_fail_open']);
  END IF;

  -- class_precedent: normalize (mechanism, indication) the same way as
  -- normalizeClassField() / normalize_class_field() and look up the base rate.
  v_moa := lower(btrim(regexp_replace(coalesce(v_asset.mechanism, ''), '\s+', ' ', 'g')));
  v_ind := lower(btrim(regexp_replace(coalesce(v_asset.indication, ''), '\s+', ' ', 'g')));
  IF v_moa <> '' AND v_ind <> '' THEN
    SELECT approval_rate INTO v_class
      FROM public.fda_class_precedent_base_rates
     WHERE moa_canonical = v_moa AND indication = v_ind;
    v_class := greatest(0, least(1, coalesce(v_class, 0)));
  END IF;

  -- Additive rubric — keep in lockstep with bc-pregate.ts BC_PREGATE_WEIGHTS.
  IF v_asset.breakthrough_designation IS TRUE THEN
    v_score := v_score + 6;
  ELSE
    v_reasons := array_append(v_reasons, 'no_breakthrough_designation');
  END IF;

  IF v_asset.first_time_sponsor IS TRUE THEN
    v_score := v_score + 4;
  ELSE
    v_reasons := array_append(v_reasons, 'sponsor_has_prior_p3');
  END IF;

  IF v_asset.priority_review IS TRUE THEN
    v_score := v_score + 3;
  ELSE
    v_reasons := array_append(v_reasons, 'no_priority_review');
  END IF;

  IF v_class > 0 THEN
    v_score := v_score + v_class * 5;
  ELSE
    v_reasons := array_append(v_reasons, 'class_precedent_unknown');
  END IF;

  RETURN jsonb_build_object(
    'score', v_score,
    'passed', v_score >= v_threshold,
    'enrichment_state', 'ready',
    'threshold', v_threshold,
    'reasons', CASE WHEN v_score >= v_threshold THEN ARRAY[]::text[] ELSE v_reasons END);
END;
$function$;

COMMENT ON FUNCTION public.bc_pregate_score_sql(uuid) IS
  'SQL mirror of reactor bc-pregate.ts scoreBcPregate(). Reads fda_assets designation/sponsor columns + fda_class_precedent_base_rates; returns {score,passed,enrichment_state,threshold,reasons}. Used by the catalyst_proximity sweep and offline measurement. Weights pinned by supabase/tests/bc_pregate_score_sql_smoke.sql.';

GRANT EXECUTE ON FUNCTION public.bc_pregate_score_sql(uuid) TO service_role;
