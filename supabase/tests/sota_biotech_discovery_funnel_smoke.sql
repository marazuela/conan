-- Smoke test for the SOTA biotech discovery funnel/evidence-packet layer.
--
-- Run with:
--   supabase db execute --file supabase/tests/sota_biotech_discovery_funnel_smoke.sql

BEGIN;

DO $$
DECLARE
  v_asset_id uuid;
  v_doc_id uuid;
  v_packet jsonb;
  v_alias_count int;
  v_flag_count int;
BEGIN
  INSERT INTO public.fda_assets (
    ticker, drug_name, application_number, sponsor_name, indication,
    program_status, watch_priority
  ) VALUES (
    'BPKT', 'packetmab', 'BPKT-001', 'Packet Bio', 'test indication',
    'phase3', 1
  ) RETURNING id INTO v_asset_id;

  SELECT public.fda_evidence_packet_status(v_asset_id, 2) INTO v_packet;
  IF COALESCE((v_packet ->> 'ok')::boolean, false) THEN
    RAISE EXCEPTION 'Expected Tier-2 packet to fail before document link, got %', v_packet;
  END IF;
  IF NOT (v_packet -> 'errors') ? 'missing_material_primary_document' THEN
    RAISE EXCEPTION 'Expected missing_material_primary_document error, got %', v_packet;
  END IF;

  INSERT INTO public.documents (
    source, source_doc_id, source_content_hash, doc_type,
    raw_text, title, published_at
  ) VALUES (
    'conan_signal', 'sota-smoke-doc', 'sha256:sota-smoke-doc',
    'pdufa_watchlist', 'SOTA smoke doc', 'SOTA smoke doc', now()
  ) RETURNING id INTO v_doc_id;

  INSERT INTO public.asset_documents (
    asset_id, document_id, link_type, extraction_method,
    extraction_confidence, is_material
  ) VALUES (
    v_asset_id, v_doc_id, 'primary', 'manual', 0.95, true
  );

  SELECT public.fda_evidence_packet_status(v_asset_id, 2) INTO v_packet;
  IF NOT COALESCE((v_packet ->> 'ok')::boolean, false) THEN
    RAISE EXCEPTION 'Expected Tier-2 packet to pass after primary doc, got %', v_packet;
  END IF;

  SELECT public.fda_evidence_packet_status(v_asset_id, 1) INTO v_packet;
  IF COALESCE((v_packet ->> 'ok')::boolean, false) THEN
    RAISE EXCEPTION 'Expected Tier-1 packet to fail before facts, got %', v_packet;
  END IF;

  INSERT INTO public.extracted_facts (
    document_id, asset_id, fact_type, fact_text, evidence_quote,
    citation_span, confidence, extraction_model
  ) VALUES (
    v_doc_id, v_asset_id, 'pdufa_date', 'PDUFA target date is 2026-12-31.',
    'PDUFA target date is 2026-12-31.',
    jsonb_build_object('start', 0, 'end', 34), 0.90, 'test'
  );

  SELECT public.fda_evidence_packet_status(v_asset_id, 1) INTO v_packet;
  IF NOT COALESCE((v_packet ->> 'ok')::boolean, false) THEN
    RAISE EXCEPTION 'Expected Tier-1 packet to pass after fact extraction, got %', v_packet;
  END IF;

  INSERT INTO public.fda_asset_resolution_aliases (
    alias_type, alias_value, canonical_value, asset_id, ticker, drug_name,
    source, notes
  ) VALUES (
    'drug_name', 'Packet-MAb', 'packetmab', v_asset_id, 'BPKT', 'packetmab',
    'operator', 'smoke alias'
  );

  SELECT count(*) INTO v_alias_count
  FROM public.fda_asset_resolution_aliases
  WHERE alias_type='drug_name' AND lower(alias_value)='packet-mab';
  IF v_alias_count <> 1 THEN
    RAISE EXCEPTION 'Expected alias row, got %', v_alias_count;
  END IF;

  INSERT INTO public.operator_flags (
    severity, source, kind, title, body, evidence
  ) VALUES (
    'info', 'biotech_discovery', 'sota_smoke',
    'SOTA smoke flag', 'Testing biotech_discovery source',
    jsonb_build_object('asset_id', v_asset_id)
  );

  SELECT count(*) INTO v_flag_count
  FROM public.operator_flags
  WHERE source='biotech_discovery' AND kind='sota_smoke' AND resolved_at IS NULL;
  IF v_flag_count <> 1 THEN
    RAISE EXCEPTION 'Expected biotech_discovery operator flag, got %', v_flag_count;
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM public.fda_discovery_quality_scorecard
    WHERE active_assets >= 1
  ) THEN
    RAISE EXCEPTION 'Expected fda_discovery_quality_scorecard to return at least one row';
  END IF;

  RAISE NOTICE 'sota_biotech_discovery_funnel smoke test passed';
END $$;

ROLLBACK;
