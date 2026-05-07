-- Conan v2 — FDA Cockpit dashboard RPCs.
--
-- Six SECURITY DEFINER functions that the /fda route's server actions call.
-- Each requires auth.uid() and writes a row to operator_actions for audit.
-- Pattern mirrors 20260503090000_dashboard_operator_workflow.sql.
--
-- 1. fda_event_approve_for_thesis(event_id, note)
--    Manual promotion of a shadow event to the live pipeline. Inserts a
--    signals row with scoring_profile='fda_event' so the reactor + thesis
--    writer pick it up under the standard flow. Handy when an operator wants
--    to short-circuit the shadow window for a high-conviction setup.
-- 2. fda_event_suppress(event_id, reason)
--    Marks event_status='superseded' and records the suppression reason in
--    the extensions JSONB. The bridge will skip it on the next run.
-- 3. fda_event_request_specialist_refresh(event_id, agent_kind)
--    Enqueues an fda_agent_reviews row with status='queued'. The Phase 5
--    Cowork specialist agents drain this queue.
-- 4. fda_event_override_feature(event_id, field, value, audit_note)
--    Appends a manual evidence row carrying the operator's override. The
--    feature builder picks up the override on the next snapshot.
-- 5. fda_event_mark_evidence_bad(evidence_id, reason)
--    Soft-deletes evidence by setting evidence_status='rejected'. The active
--    partial index excludes rejected rows, so the next snapshot is computed
--    without them.
-- 6. fda_event_pin_to_watch(event_id)
--    Creates a watch-state candidate row for the event's ticker so it
--    appears in /candidates without operating on the signals table.

-- ============================================================
-- 1. approve_for_thesis
-- ============================================================
CREATE OR REPLACE FUNCTION public.fda_event_approve_for_thesis(
  p_event_id uuid,
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
  v_feature public.fda_event_features%ROWTYPE;
  v_scanner_id uuid;
  v_rubric_id uuid;
  v_signal_id text;
  v_thesis_dir text;
BEGIN
  IF v_actor IS NULL THEN
    RAISE EXCEPTION 'fda_event_approve_for_thesis: authentication required';
  END IF;

  SELECT * INTO v_event FROM public.fda_regulatory_events WHERE id = p_event_id FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'fda_event_approve_for_thesis: event % not found', p_event_id;
  END IF;
  IF v_event.event_status <> 'pending' THEN
    RAISE EXCEPTION 'fda_event_approve_for_thesis: event % has status %, expected pending', p_event_id, v_event.event_status;
  END IF;
  IF v_event.event_type IN ('approval','crl','presumed_crl','withdrawal') THEN
    RAISE EXCEPTION 'fda_event_approve_for_thesis: resolution events cannot be promoted as new opportunities';
  END IF;

  SELECT * INTO v_asset FROM public.fda_assets WHERE id = v_event.asset_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'fda_event_approve_for_thesis: asset % not found', v_event.asset_id;
  END IF;

  SELECT * INTO v_feature FROM public.fda_event_features WHERE event_id = p_event_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'fda_event_approve_for_thesis: feature snapshot for event % not found; bridge has not scored it yet', p_event_id;
  END IF;

  SELECT id INTO v_scanner_id FROM public.scanners WHERE name = 'fda_signal_bridge';
  IF v_scanner_id IS NULL THEN
    RAISE EXCEPTION 'fda_event_approve_for_thesis: scanner fda_signal_bridge not registered';
  END IF;
  SELECT id INTO v_rubric_id FROM public.rubrics
    WHERE profile = 'fda_event' AND superseded_at IS NULL
    ORDER BY rubric_version DESC LIMIT 1;
  IF v_rubric_id IS NULL THEN
    RAISE EXCEPTION 'fda_event_approve_for_thesis: no active fda_event rubric';
  END IF;

  v_signal_id := 'fdae:' || replace(p_event_id::text, '-', '');
  v_thesis_dir := CASE
    WHEN COALESCE(v_feature.fair_probability, 0) > COALESCE(v_feature.market_implied_probability, v_feature.fair_probability) THEN 'long'
    WHEN COALESCE(v_feature.fair_probability, 0) < COALESCE(v_feature.market_implied_probability, v_feature.fair_probability) THEN 'short'
    ELSE 'neutral'
  END;

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
    v_thesis_dir,
    jsonb_build_object(
      'fair_probability', v_feature.fair_probability,
      'pricing_edge', v_feature.pricing_edge,
      'expected_value', v_feature.expected_value_pct,
      '_provenance', 'dashboard_approve'
    ),
    COALESCE(v_feature.score, v_feature.shadow_score, 0)::numeric(5,2),
    COALESCE(v_feature.band, v_feature.shadow_band, 'discard'::signal_band),
    COALESCE(v_feature.score, v_feature.shadow_score)::numeric(5,2),
    COALESCE(v_feature.band, v_feature.shadow_band),
    jsonb_build_object(
      'ticker', v_asset.ticker,
      'drug_name', v_asset.drug_name,
      'application_number', v_asset.application_number,
      'indication', v_asset.indication,
      'event_id', p_event_id,
      'event_type', v_event.event_type,
      'event_date', v_event.event_date,
      'fair_probability', v_feature.fair_probability,
      'market_implied_probability', v_feature.market_implied_probability,
      'expected_value_pct', v_feature.expected_value_pct,
      'upside_pct', v_feature.upside_pct,
      'downside_pct', v_feature.downside_pct,
      'approved_by_dashboard', true
    ),
    jsonb_build_object(
      'fda_event_id', p_event_id,
      'approved_at', now(),
      'approved_by', v_actor
    )
  )
  ON CONFLICT (signal_id) DO NOTHING;

  INSERT INTO public.operator_actions (
    actor_id, action_type, target_type, target_id, signal_id, note, payload
  )
  VALUES (
    v_actor,
    'fda_event_approve_for_thesis',
    'fda_event',
    p_event_id::text,
    v_signal_id,
    p_note,
    jsonb_build_object(
      'event_type', v_event.event_type,
      'ticker', v_asset.ticker,
      'drug_name', v_asset.drug_name,
      'feature_score', v_feature.score,
      'feature_band', v_feature.band
    )
  );

  RETURN jsonb_build_object(
    'applied', true,
    'event_id', p_event_id,
    'signal_id', v_signal_id,
    'scoring_profile', 'fda_event'
  );
END;
$$;

-- ============================================================
-- 2. suppress
-- ============================================================
CREATE OR REPLACE FUNCTION public.fda_event_suppress(
  p_event_id uuid,
  p_reason text
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_actor uuid := auth.uid();
  v_old public.fda_regulatory_events%ROWTYPE;
BEGIN
  IF v_actor IS NULL THEN
    RAISE EXCEPTION 'fda_event_suppress: authentication required';
  END IF;
  IF p_reason IS NULL OR length(trim(p_reason)) = 0 THEN
    RAISE EXCEPTION 'fda_event_suppress: reason is required';
  END IF;

  SELECT * INTO v_old FROM public.fda_regulatory_events WHERE id = p_event_id FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'fda_event_suppress: event % not found', p_event_id;
  END IF;

  UPDATE public.fda_regulatory_events
  SET event_status = 'superseded',
      extensions = COALESCE(extensions, '{}'::jsonb) || jsonb_build_object(
        'suppressed_reason', p_reason,
        'suppressed_by', v_actor,
        'suppressed_at', now()
      ),
      updated_at = now()
  WHERE id = p_event_id;

  INSERT INTO public.operator_actions (
    actor_id, action_type, target_type, target_id, note, payload
  )
  VALUES (
    v_actor,
    'fda_event_suppress',
    'fda_event',
    p_event_id::text,
    p_reason,
    jsonb_build_object(
      'previous_status', v_old.event_status,
      'event_type', v_old.event_type
    )
  );

  RETURN jsonb_build_object(
    'applied', true,
    'event_id', p_event_id,
    'previous_status', v_old.event_status,
    'new_status', 'superseded'
  );
END;
$$;

-- ============================================================
-- 3. request_specialist_refresh
-- ============================================================
CREATE OR REPLACE FUNCTION public.fda_event_request_specialist_refresh(
  p_event_id uuid,
  p_agent_kind text
) RETURNS jsonb
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
  IF p_agent_kind NOT IN ('medical', 'regulatory', 'microstructure') THEN
    RAISE EXCEPTION 'fda_event_request_specialist_refresh: agent_kind %, expected medical|regulatory|microstructure', p_agent_kind;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM public.fda_regulatory_events WHERE id = p_event_id) THEN
    RAISE EXCEPTION 'fda_event_request_specialist_refresh: event % not found', p_event_id;
  END IF;

  -- Each manual refresh request gets a unique snapshot_hash so the unique
  -- constraint (event_id, agent_kind, snapshot_hash) doesn't collide with a
  -- prior auto-snapshot.
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

-- ============================================================
-- 4. override_feature
-- ============================================================
CREATE OR REPLACE FUNCTION public.fda_event_override_feature(
  p_event_id uuid,
  p_field text,
  p_value jsonb,
  p_audit_note text
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_actor uuid := auth.uid();
  v_evidence_id uuid;
  v_hash text;
BEGIN
  IF v_actor IS NULL THEN
    RAISE EXCEPTION 'fda_event_override_feature: authentication required';
  END IF;
  IF p_field IS NULL OR length(trim(p_field)) = 0 THEN
    RAISE EXCEPTION 'fda_event_override_feature: field is required';
  END IF;
  IF p_audit_note IS NULL OR length(trim(p_audit_note)) = 0 THEN
    RAISE EXCEPTION 'fda_event_override_feature: audit_note is required';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM public.fda_regulatory_events WHERE id = p_event_id) THEN
    RAISE EXCEPTION 'fda_event_override_feature: event % not found', p_event_id;
  END IF;

  v_hash := encode(digest(p_event_id::text || ':' || p_field || ':' || coalesce(p_value::text,'') || ':' || extract(epoch from now())::text, 'sha256'), 'hex');

  INSERT INTO public.fda_event_evidence (
    event_id, source, evidence_type, payload, hash
  )
  VALUES (
    p_event_id,
    'manual',
    'feature_override',
    jsonb_build_object(
      'field', p_field,
      'value', p_value,
      'audit_note', p_audit_note,
      'override_by', v_actor,
      'override_at', now()
    ),
    v_hash
  )
  RETURNING id INTO v_evidence_id;

  INSERT INTO public.operator_actions (
    actor_id, action_type, target_type, target_id, note, payload
  )
  VALUES (
    v_actor,
    'fda_event_override_feature',
    'fda_event',
    p_event_id::text,
    p_audit_note,
    jsonb_build_object('field', p_field, 'value', p_value, 'evidence_id', v_evidence_id)
  );

  RETURN jsonb_build_object(
    'applied', true,
    'event_id', p_event_id,
    'evidence_id', v_evidence_id,
    'field', p_field
  );
END;
$$;

-- ============================================================
-- 5. mark_evidence_bad
-- ============================================================
CREATE OR REPLACE FUNCTION public.fda_event_mark_evidence_bad(
  p_evidence_id uuid,
  p_reason text
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_actor uuid := auth.uid();
  v_evidence public.fda_event_evidence%ROWTYPE;
BEGIN
  IF v_actor IS NULL THEN
    RAISE EXCEPTION 'fda_event_mark_evidence_bad: authentication required';
  END IF;
  IF p_reason IS NULL OR length(trim(p_reason)) = 0 THEN
    RAISE EXCEPTION 'fda_event_mark_evidence_bad: reason is required';
  END IF;

  SELECT * INTO v_evidence FROM public.fda_event_evidence WHERE id = p_evidence_id FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'fda_event_mark_evidence_bad: evidence % not found', p_evidence_id;
  END IF;
  IF v_evidence.evidence_status = 'rejected' THEN
    RETURN jsonb_build_object(
      'applied', false,
      'evidence_id', p_evidence_id,
      'reason', 'already_rejected'
    );
  END IF;

  UPDATE public.fda_event_evidence
  SET evidence_status = 'rejected',
      rejected_reason = p_reason,
      rejected_at = now(),
      rejected_by = v_actor
  WHERE id = p_evidence_id;

  INSERT INTO public.operator_actions (
    actor_id, action_type, target_type, target_id, note, payload
  )
  VALUES (
    v_actor,
    'fda_event_mark_evidence_bad',
    'fda_event_evidence',
    p_evidence_id::text,
    p_reason,
    jsonb_build_object(
      'event_id', v_evidence.event_id,
      'source', v_evidence.source,
      'evidence_type', v_evidence.evidence_type
    )
  );

  RETURN jsonb_build_object(
    'applied', true,
    'evidence_id', p_evidence_id,
    'event_id', v_evidence.event_id
  );
END;
$$;

-- ============================================================
-- 6. pin_to_watch
-- ============================================================
CREATE OR REPLACE FUNCTION public.fda_event_pin_to_watch(
  p_event_id uuid,
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
  v_candidate_id uuid;
BEGIN
  IF v_actor IS NULL THEN
    RAISE EXCEPTION 'fda_event_pin_to_watch: authentication required';
  END IF;

  SELECT * INTO v_event FROM public.fda_regulatory_events WHERE id = p_event_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'fda_event_pin_to_watch: event % not found', p_event_id;
  END IF;
  SELECT * INTO v_asset FROM public.fda_assets WHERE id = v_event.asset_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'fda_event_pin_to_watch: asset % not found', v_event.asset_id;
  END IF;

  -- Reuse an existing watch candidate for this ticker if one exists; otherwise insert.
  SELECT id INTO v_candidate_id
  FROM public.candidates
  WHERE ticker = v_asset.ticker AND mic IS NOT DISTINCT FROM v_asset.mic
  FOR UPDATE;

  IF v_candidate_id IS NULL THEN
    INSERT INTO public.candidates (
      ticker, mic, entity_id, state, scoring_profile,
      kill_conditions, next_catalyst_date, extensions
    )
    VALUES (
      v_asset.ticker,
      v_asset.mic,
      v_asset.entity_id,
      'watch'::candidate_state,
      'fda_event',
      '[]'::jsonb,
      v_event.event_date,
      jsonb_build_object(
        'fda_event_id', p_event_id,
        'pinned_by_dashboard', true,
        'pinned_at', now(),
        'pinned_by', v_actor,
        'note', p_note
      )
    )
    RETURNING id INTO v_candidate_id;
  ELSE
    UPDATE public.candidates
    SET extensions = COALESCE(extensions, '{}'::jsonb) || jsonb_build_object(
          'fda_event_id', p_event_id,
          'pinned_by_dashboard', true,
          'pinned_at', now(),
          'pinned_by', v_actor,
          'note', p_note
        ),
        next_catalyst_date = COALESCE(next_catalyst_date, v_event.event_date),
        updated_at = now()
    WHERE id = v_candidate_id;
  END IF;

  INSERT INTO public.operator_actions (
    actor_id, action_type, target_type, target_id, candidate_id, note, payload
  )
  VALUES (
    v_actor,
    'fda_event_pin_to_watch',
    'fda_event',
    p_event_id::text,
    v_candidate_id,
    p_note,
    jsonb_build_object(
      'ticker', v_asset.ticker,
      'event_type', v_event.event_type,
      'event_date', v_event.event_date
    )
  );

  RETURN jsonb_build_object(
    'applied', true,
    'event_id', p_event_id,
    'candidate_id', v_candidate_id
  );
END;
$$;

-- Lock down PUBLIC, grant to authenticated.
REVOKE ALL ON FUNCTION public.fda_event_approve_for_thesis(uuid, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.fda_event_suppress(uuid, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.fda_event_request_specialist_refresh(uuid, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.fda_event_override_feature(uuid, text, jsonb, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.fda_event_mark_evidence_bad(uuid, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.fda_event_pin_to_watch(uuid, text) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION public.fda_event_approve_for_thesis(uuid, text) TO authenticated;
GRANT EXECUTE ON FUNCTION public.fda_event_suppress(uuid, text) TO authenticated;
GRANT EXECUTE ON FUNCTION public.fda_event_request_specialist_refresh(uuid, text) TO authenticated;
GRANT EXECUTE ON FUNCTION public.fda_event_override_feature(uuid, text, jsonb, text) TO authenticated;
GRANT EXECUTE ON FUNCTION public.fda_event_mark_evidence_bad(uuid, text) TO authenticated;
GRANT EXECUTE ON FUNCTION public.fda_event_pin_to_watch(uuid, text) TO authenticated;
