-- v3 G3B (2026-05-11): operator-driven IC-memo promotion path.
--
-- After the orchestrator's Stage-10 IC memo lands in fda_agent_reviews
-- (agent_kind='ic_memo'), the operator reviews it on the FDA detail page and
-- decides whether to promote the underlying event into the thesis pipeline.
-- Mirrors fda_event_approve_for_thesis (2026-05-05) but keys off the v3
-- convergence_assessments row instead of fda_event_features, and links the
-- chosen ic_memo review row in the resulting signals row's extensions for
-- audit.
--
-- Flow:
--   1. Auth check (auth.uid() must be set; SECURITY DEFINER context).
--   2. Validate the chosen review row: exists, agent_kind='ic_memo',
--      status='completed', and event_id matches the supplied event.
--   3. Validate the event: exists, status='pending', not a resolution event.
--   4. Pull the latest non-superseded convergence_assessments for the asset.
--      If none exists, the v3 path is not applicable — operator must use the
--      v2 fda_event_approve_for_thesis instead.
--   5. Insert the signals row using v3 conviction_pct + thesis_direction
--      from the assessment. signal_id is namespaced with 'v3:' prefix so
--      bridge-mode collisions cannot occur (the v2 RPC uses 'fdae:').
--   6. Append an operator_actions audit row.
--
-- RLS: function is SECURITY DEFINER + GRANT EXECUTE TO authenticated. The
-- explicit auth.uid() guard is still required because policy bypass alone
-- would allow service-role contexts to call without an actor.

CREATE OR REPLACE FUNCTION public.fda_signal_promote_to_thesis(
  p_event_id uuid,
  p_ic_memo_review_id uuid,
  p_note text DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_actor uuid := auth.uid();
  v_event public.fda_regulatory_events%ROWTYPE;
  v_asset public.fda_assets%ROWTYPE;
  v_review public.fda_agent_reviews%ROWTYPE;
  v_assessment public.convergence_assessments%ROWTYPE;
  v_scanner_id uuid;
  v_rubric_id uuid;
  v_signal_id text;
  v_band signal_band;
  v_score numeric(5,2);
BEGIN
  IF v_actor IS NULL THEN
    RAISE EXCEPTION 'fda_signal_promote_to_thesis: authentication required';
  END IF;

  -- 1. Validate review row.
  SELECT * INTO v_review FROM public.fda_agent_reviews
    WHERE id = p_ic_memo_review_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'fda_signal_promote_to_thesis: ic_memo review % not found',
      p_ic_memo_review_id;
  END IF;
  IF v_review.agent_kind <> 'ic_memo' THEN
    RAISE EXCEPTION 'fda_signal_promote_to_thesis: review % has agent_kind=%, expected ic_memo',
      p_ic_memo_review_id, v_review.agent_kind;
  END IF;
  IF v_review.event_id <> p_event_id THEN
    RAISE EXCEPTION 'fda_signal_promote_to_thesis: review % is for event %, not %',
      p_ic_memo_review_id, v_review.event_id, p_event_id;
  END IF;
  IF v_review.status <> 'completed' THEN
    RAISE EXCEPTION 'fda_signal_promote_to_thesis: review % has status %, expected completed',
      p_ic_memo_review_id, v_review.status;
  END IF;

  -- 2. Validate event row + lock for update.
  SELECT * INTO v_event FROM public.fda_regulatory_events
    WHERE id = p_event_id FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'fda_signal_promote_to_thesis: event % not found', p_event_id;
  END IF;
  IF v_event.event_status <> 'pending' THEN
    RAISE EXCEPTION 'fda_signal_promote_to_thesis: event % has status %, expected pending',
      p_event_id, v_event.event_status;
  END IF;
  IF v_event.event_type IN ('approval','crl','presumed_crl','withdrawal') THEN
    RAISE EXCEPTION 'fda_signal_promote_to_thesis: resolution events cannot be promoted as new opportunities';
  END IF;

  -- 3. Asset lookup (entity_id needed for signals.entity_id NOT NULL).
  SELECT * INTO v_asset FROM public.fda_assets WHERE id = v_event.asset_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'fda_signal_promote_to_thesis: asset % not found', v_event.asset_id;
  END IF;

  -- 4. Pull latest non-superseded assessment for this asset. If absent, the
  -- v3 path is not applicable — the operator should use the v2 RPC instead.
  SELECT * INTO v_assessment FROM public.convergence_assessments
    WHERE asset_id = v_event.asset_id
      AND superseded_at IS NULL
    ORDER BY created_at DESC
    LIMIT 1;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'fda_signal_promote_to_thesis: no active convergence_assessments row for asset % — run orchestrator first',
      v_event.asset_id;
  END IF;
  IF v_assessment.conviction_pct IS NULL THEN
    RAISE EXCEPTION 'fda_signal_promote_to_thesis: assessment % has null conviction_pct — re-run orchestrator',
      v_assessment.id;
  END IF;

  -- 5. Resolve scanner + rubric for the synthetic signal.
  SELECT id INTO v_scanner_id FROM public.scanners WHERE name = 'fda_signal_bridge';
  IF v_scanner_id IS NULL THEN
    RAISE EXCEPTION 'fda_signal_promote_to_thesis: scanner fda_signal_bridge not registered';
  END IF;
  SELECT id INTO v_rubric_id FROM public.rubrics
    WHERE profile = 'fda_event' AND superseded_at IS NULL
    ORDER BY rubric_version DESC LIMIT 1;
  IF v_rubric_id IS NULL THEN
    RAISE EXCEPTION 'fda_signal_promote_to_thesis: no active fda_event rubric';
  END IF;

  v_signal_id := 'v3:' || replace(p_event_id::text, '-', '');
  v_band := COALESCE(v_assessment.band::signal_band, 'discard'::signal_band);
  v_score := v_assessment.conviction_pct;  -- already 0..100

  INSERT INTO public.signals (
    signal_id, entity_id, scanner_id, scoring_profile, rubric_version_id,
    source_content_hash, source_date, scan_date, signal_type,
    thesis_direction, dimensions,
    score, band, score_with_bonus, band_with_bonus,
    raw_payload, extensions
  )
  VALUES (
    v_signal_id,
    v_asset.entity_id,
    v_scanner_id,
    'fda_event',
    v_rubric_id,
    v_event.source_content_hash,
    COALESCE(v_event.event_date::timestamptz, now()),
    now(),
    v_event.event_type,
    v_assessment.thesis_direction,
    jsonb_build_object(
      'conviction_pct', v_assessment.conviction_pct,
      'conviction_pct_calibrated', v_assessment.conviction_pct_calibrated,
      'ensemble_dispersion', v_assessment.ensemble_dispersion,
      'evidence_quality', v_assessment.evidence_quality,
      '_provenance', 'dashboard_v3_promote'
    ),
    v_score,
    v_band,
    v_score,
    v_band,
    jsonb_build_object(
      'ticker', v_asset.ticker,
      'drug_name', v_asset.drug_name,
      'application_number', v_asset.application_number,
      'indication', v_asset.indication,
      'event_id', p_event_id,
      'event_type', v_event.event_type,
      'event_date', v_event.event_date,
      'assessment_id', v_assessment.id,
      'ic_memo_review_id', p_ic_memo_review_id,
      'thesis_direction', v_assessment.thesis_direction,
      'conviction_pct', v_assessment.conviction_pct,
      'promoted_by_dashboard', true
    ),
    jsonb_build_object(
      'fda_event_id', p_event_id,
      'ic_memo_review_id', p_ic_memo_review_id,
      'assessment_id', v_assessment.id,
      'orchestrator_version', v_assessment.orchestrator_version,
      'calibration_curve_version', v_assessment.calibration_curve_version,
      'promoted_at', now(),
      'promoted_by', v_actor
    )
  )
  ON CONFLICT (signal_id) DO NOTHING;

  -- 6. Audit log.
  INSERT INTO public.operator_actions (
    actor_id, action_type, target_type, target_id, signal_id, note, payload
  )
  VALUES (
    v_actor,
    'fda_signal_promote_to_thesis',
    'fda_event',
    p_event_id::text,
    v_signal_id,
    p_note,
    jsonb_build_object(
      'event_type', v_event.event_type,
      'ticker', v_asset.ticker,
      'drug_name', v_asset.drug_name,
      'assessment_id', v_assessment.id,
      'ic_memo_review_id', p_ic_memo_review_id,
      'conviction_pct', v_assessment.conviction_pct,
      'thesis_direction', v_assessment.thesis_direction,
      'band', v_band
    )
  );

  RETURN jsonb_build_object(
    'applied', true,
    'event_id', p_event_id,
    'signal_id', v_signal_id,
    'assessment_id', v_assessment.id,
    'ic_memo_review_id', p_ic_memo_review_id,
    'conviction_pct', v_assessment.conviction_pct,
    'thesis_direction', v_assessment.thesis_direction,
    'band', v_band
  );
END;
$$;

GRANT EXECUTE ON FUNCTION public.fda_signal_promote_to_thesis(uuid, uuid, text)
  TO authenticated;

COMMENT ON FUNCTION public.fda_signal_promote_to_thesis(uuid, uuid, text) IS
  'v3 G3B (2026-05-11): operator-driven IC-memo promotion path. '
  'Inserts a signals row keyed off latest non-superseded convergence_assessments '
  'for the event''s asset. signal_id namespaced "v3:" to avoid collision with '
  'the v2 fda_event_approve_for_thesis "fdae:" path. Reasons it raises (and '
  'what the operator should do): no active assessment → run orchestrator first; '
  'assessment.conviction_pct null → re-run orchestrator (Stage 8 calibration '
  'failed); ic_memo review not completed → wait for runner to finish; event '
  'not pending → already actioned.';
