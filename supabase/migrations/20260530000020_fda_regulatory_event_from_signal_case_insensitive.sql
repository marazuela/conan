-- Patch fda_regulatory_event_from_signal() for the case-insensitive fda_assets
-- unique index introduced by 20260530000010.
--
-- Context: 20260530000010 replaced the case-sensitive unique constraint
-- (ticker, drug_name, application_number) on fda_assets with a case-insensitive
-- unique INDEX (ticker, lower(drug_name), application_number). The
-- fda_regulatory_event_from_signal() trigger function — installed earlier as
-- part of the v3 transition — still referenced the old constraint via
-- `ON CONFLICT (ticker, drug_name, application_number)`. Because that
-- constraint name no longer exists, every binary_catalyst signal INSERT now
-- raises:
--   ERROR 42P10: there is no unique or exclusion constraint matching the
--   ON CONFLICT specification
--
-- Fix: rewrite the function to (a) infer the new index by its expression list,
-- and (b) also use lower(drug_name) in the pre-INSERT SELECT lookup so a
-- dedup'd asset stored as "Olezarsen" still matches a signal that emits
-- "olezarsen". The body is otherwise unchanged from its prior shape.

CREATE OR REPLACE FUNCTION public.fda_regulatory_event_from_signal()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $function$
DECLARE
  v_signal_type        text;
  v_ticker             text;
  v_drug_name          text;
  v_app_num            text;
  v_indication         text;
  v_sponsor            text;
  v_pdufa_date_str     text;
  v_previous_pdufa     text;
  v_pcd                text;
  v_file_date          text;
  v_adsh               text;
  v_status             text;
  v_crl_date           text;
  v_score              numeric;
  v_event_type         text;
  v_event_status       text;
  v_event_date         date;
  v_hash               text;
  v_asset_id           uuid;
  v_program_status     text;
  v_hint               jsonb;
BEGIN
  IF NEW.scoring_profile IS DISTINCT FROM 'binary_catalyst' THEN RETURN NEW; END IF;
  IF NEW.entity_id IS NULL THEN RETURN NEW; END IF;
  v_score := COALESCE(NEW.score_with_bonus, NEW.score, 0);
  IF v_score < 25 THEN RETURN NEW; END IF;
  v_signal_type := NEW.signal_type;
  IF v_signal_type NOT IN (
    'pdufa_imminent','pdufa_approaching','pdufa_watchlist',
    'pdufa_date_advanced','pdufa_date_delayed',
    'fda_decision','pre_phase3_readout','eop2_meeting'
  ) THEN RETURN NEW; END IF;

  IF v_signal_type IN ('pdufa_imminent','pdufa_approaching','pdufa_watchlist') THEN
    v_ticker := NULLIF(NEW.raw_payload->>'ticker','');
    v_drug_name := NULLIF(NEW.raw_payload->>'drug_name','');
    v_app_num := COALESCE(NULLIF(NEW.raw_payload->>'application_number',''), '');
    v_indication := NULLIF(NEW.raw_payload->>'indication','');
    v_sponsor := NULLIF(NEW.raw_payload->>'company_name','');
    v_pdufa_date_str := NULLIF(NEW.raw_payload->>'pdufa_date','');
    v_event_type := 'pdufa';
    v_event_status := 'pending';
    BEGIN v_event_date := v_pdufa_date_str::date; EXCEPTION WHEN OTHERS THEN v_event_date := NULL; END;
    v_hash := 'sha256:' || encode(extensions.digest(
      'pdufa|' || COALESCE(v_ticker,'') || '|' || COALESCE(v_drug_name,'') || '|' || COALESCE(v_pdufa_date_str,''),
      'sha256'), 'hex');
    v_program_status := 'filed';
  ELSIF v_signal_type IN ('pdufa_date_advanced','pdufa_date_delayed') THEN
    v_ticker := NULLIF(NEW.raw_payload->>'ticker','');
    v_drug_name := NULLIF(NEW.raw_payload->>'drug_name','');
    v_app_num := COALESCE(NULLIF(NEW.raw_payload->>'application_number',''), '');
    v_indication := NULLIF(NEW.raw_payload->>'indication','');
    v_sponsor := NULLIF(NEW.raw_payload->>'company_name','');
    v_pdufa_date_str := NULLIF(NEW.raw_payload->>'pdufa_date','');
    v_previous_pdufa := NULLIF(NEW.raw_payload->>'previous_pdufa_date','');
    v_event_type := 'date_change';
    v_event_status := 'pending';
    BEGIN v_event_date := v_pdufa_date_str::date; EXCEPTION WHEN OTHERS THEN v_event_date := NULL; END;
    v_hash := 'sha256:' || encode(extensions.digest(
      'date_change|' || COALESCE(v_ticker,'') || '|' || COALESCE(v_drug_name,'') || '|' ||
      COALESCE(v_pdufa_date_str,'') || '|' || COALESCE(v_previous_pdufa,''),
      'sha256'), 'hex');
    v_program_status := 'filed';
  ELSIF v_signal_type = 'fda_decision' THEN
    v_ticker := NULLIF(NEW.raw_payload->>'ticker','');
    v_drug_name := NULLIF(NEW.raw_payload->>'drug_name','');
    v_app_num := COALESCE(NULLIF(NEW.raw_payload->>'application_number',''), '');
    v_indication := NULLIF(NEW.raw_payload->>'indication','');
    v_sponsor := NULLIF(NEW.raw_payload->>'company_name','');
    v_pdufa_date_str := NULLIF(NEW.raw_payload->>'pdufa_date','');
    v_crl_date := NULLIF(NEW.raw_payload->>'crl_date','');
    v_status := COALESCE(NULLIF(NEW.raw_payload->>'status',''), '');
    v_event_type := CASE
      WHEN v_status IN ('approved','approval') THEN 'approval'
      WHEN v_status = 'crl' THEN 'crl'
      WHEN v_status IN ('presumed_crl','resolved_crl') THEN 'presumed_crl'
      WHEN v_status = 'withdrawal' THEN 'withdrawal'
      ELSE NULL END;
    IF v_event_type IS NULL THEN RETURN NEW; END IF;
    v_event_status := 'resolved';
    BEGIN v_event_date := COALESCE(v_crl_date, v_pdufa_date_str)::date;
    EXCEPTION WHEN OTHERS THEN v_event_date := NULL; END;
    v_hash := 'sha256:' || encode(extensions.digest(
      'decision|' || COALESCE(v_ticker,'') || '|' || COALESCE(v_drug_name,'') || '|' ||
      v_event_type || '|' || COALESCE(v_pdufa_date_str,''),
      'sha256'), 'hex');
    v_program_status := CASE WHEN v_event_type = 'approval' THEN 'approved' ELSE 'filed' END;
  ELSIF v_signal_type = 'eop2_meeting' THEN
    v_ticker := NULLIF(NEW.raw_payload->>'ticker','');
    v_drug_name := NULLIF(NEW.raw_payload->>'drug_name','');
    v_app_num := '';
    v_sponsor := NULLIF(NEW.raw_payload->>'company_name','');
    v_file_date := NULLIF(NEW.raw_payload->>'file_date','');
    v_adsh := NULLIF(NEW.raw_payload->>'adsh','');
    IF v_adsh IS NULL THEN RETURN NEW; END IF;
    v_event_type := 'eop2';
    v_event_status := 'pending';
    BEGIN v_event_date := v_file_date::date; EXCEPTION WHEN OTHERS THEN v_event_date := NULL; END;
    v_hash := 'sha256:' || encode(extensions.digest('eop2|' || v_adsh, 'sha256'), 'hex');
    v_program_status := 'phase2';
  ELSIF v_signal_type = 'pre_phase3_readout' THEN
    v_hint := NEW.raw_payload->'auto_seed_fda_asset';
    IF v_hint IS NULL THEN RETURN NEW; END IF;
    v_ticker := NULLIF(v_hint->>'ticker','');
    v_drug_name := NULLIF(v_hint->>'drug_name','');
    v_app_num := '';
    v_indication := NULLIF(v_hint->>'indication','');
    v_sponsor := NULLIF(v_hint->>'sponsor_name','');
    v_pcd := NULLIF(v_hint->>'primary_completion_date','');
    v_event_type := 'phase3_readout';
    v_event_status := 'pending';
    BEGIN v_event_date := v_pcd::date; EXCEPTION WHEN OTHERS THEN v_event_date := NULL; END;
    v_hash := 'sha256:' || encode(extensions.digest(
      'phase3_readout|' || COALESCE(v_ticker,'') || '|' || COALESCE(v_drug_name,'') || '|' || COALESCE(v_pcd,''),
      'sha256'), 'hex');
    v_program_status := 'phase3';
  ELSE
    RETURN NEW;
  END IF;

  IF v_ticker IS NULL OR v_drug_name IS NULL THEN RETURN NEW; END IF;

  -- Case-insensitive lookup so a deduped asset stored with the canonical-case
  -- drug_name still matches a signal that emits a different casing.
  SELECT id INTO v_asset_id
    FROM public.fda_assets
   WHERE ticker = v_ticker
     AND lower(drug_name) = lower(v_drug_name)
     AND application_number = v_app_num
   ORDER BY created_at ASC
   LIMIT 1;

  IF v_asset_id IS NULL THEN
    INSERT INTO public.fda_assets (
      ticker, drug_name, application_number,
      entity_id, sponsor_name, indication, program_status,
      is_active, watch_priority, extensions
    )
    VALUES (
      v_ticker, v_drug_name, v_app_num,
      NEW.entity_id, v_sponsor, v_indication, v_program_status,
      true, 3,
      jsonb_build_object(
        'auto_seeded_from', 'fda_regulatory_event_from_signal',
        'seeding_signal_id', NEW.signal_id,
        'seeding_signal_type', v_signal_type,
        'seeded_at', now()
      )
    )
    -- Target the new case-insensitive unique INDEX
    -- (fda_assets_ticker_lowerdrug_appnum_uniq) by inferring its expression list.
    ON CONFLICT (ticker, (lower(drug_name)), application_number) DO NOTHING
    RETURNING id INTO v_asset_id;
    IF v_asset_id IS NULL THEN
      SELECT id INTO v_asset_id FROM public.fda_assets
       WHERE ticker = v_ticker
         AND lower(drug_name) = lower(v_drug_name)
         AND application_number = v_app_num
       ORDER BY created_at ASC
       LIMIT 1;
    END IF;
  END IF;

  IF v_asset_id IS NULL THEN RETURN NEW; END IF;

  INSERT INTO public.fda_regulatory_events (
    asset_id, event_type, event_date, event_status,
    source_content_hash, notes, extensions
  )
  VALUES (
    v_asset_id, v_event_type, v_event_date, v_event_status,
    v_hash,
    'auto-derived from signal: ' || v_signal_type || ' (signal_id=' || NEW.signal_id || ')',
    jsonb_build_object(
      'source_signal_id', NEW.signal_id,
      'source_signal_type', v_signal_type,
      'source_scoring_profile', NEW.scoring_profile,
      'source_score', v_score,
      'created_by', 'fda_regulatory_event_from_signal'
    )
  )
  ON CONFLICT (asset_id, event_type, event_date, source_content_hash) DO NOTHING;

  RETURN NEW;
END;
$function$;
