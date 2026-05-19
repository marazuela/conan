-- Smoke test for the v3 signal→fda_assets→asset_documents bridge.
--
-- Exercises the end-to-end flow proven by migrations
-- 20260530000000_v3_bridge_signal_to_fda_assets.sql and
-- 20260530000010_dedup_fda_assets_case_duplicates.sql.
--
-- Cases:
--   1. Existing-asset match: signal entity_id + drug_name resolves to the
--      pre-seeded fda_assets row; bridge creates documents + asset_documents.
--   2. Case-insensitive match: signal drug_name "OLEZARSEN" matches the
--      pre-seeded "Olezarsen" row.
--   3. Auto-seed: signal with ticker+drug+sponsor but no matching asset
--      seeds a new fda_assets stub and links to it.
--   4. Operator flag: signal lacks ticker → no asset_doc, one open
--      operator_flags row with source='bridge_signal_to_v3'.
--   5. Garbage drug_name "EX-99": treated as missing drug_name → flagged.
--   6. Idempotency: re-firing on the same signal_id is a no-op.
--   7. Non-FDA scoring_profile: bridge no-ops.
--
-- Run with:
--   supabase db execute --file supabase/tests/bridge_signal_to_v3_smoke.sql
--
-- All writes happen inside a transaction that ROLLBACKs at the end so the
-- target database is unaffected. Failed assertions RAISE EXCEPTION, which the
-- ROLLBACK then propagates to the caller's exit code.

BEGIN;

DO $$
DECLARE
  v_entity_id uuid;
  v_rubric_id uuid;
  v_pre_seeded_asset uuid;
  v_seeded_asset uuid;
  v_signal public.signals;
  v_doc_count int;
  v_link_count int;
  v_flag_count int;
BEGIN
  -- Pick any pre-existing entity + rubric_version_id so we satisfy FK + NOT NULL
  -- constraints without owning a fixture schema. The transaction rolls back so
  -- the choice is inconsequential.
  SELECT id INTO v_entity_id FROM public.entities LIMIT 1;
  IF v_entity_id IS NULL THEN
    RAISE EXCEPTION 'No entities row available — smoke test needs one to satisfy signals.entity_id FK';
  END IF;
  SELECT rubric_version_id INTO v_rubric_id
  FROM public.signals WHERE scoring_profile='binary_catalyst' LIMIT 1;
  IF v_rubric_id IS NULL THEN
    RAISE EXCEPTION 'No prior binary_catalyst rubric_version_id available';
  END IF;

  -- ---- Fixture: pre-seeded fda_asset for cases 1 + 2 ----
  -- BSMK chosen so it doesn't collide with real tickers; the ROLLBACK at end
  -- discards it.
  INSERT INTO public.fda_assets (
    ticker, drug_name, application_number, entity_id, sponsor_name, indication
  ) VALUES (
    'BSMK', 'Olezarsen', 'BSMK-001', v_entity_id, 'Bridge Smoke Pharma', 'test indication'
  ) RETURNING id INTO v_pre_seeded_asset;

  -- ---------- CASE 1: existing-asset match by entity_id + drug_name ----------
  INSERT INTO public.signals (
    signal_id, source_content_hash, source_date, scan_date,
    signal_type, scoring_profile, rubric_version_id, score, dimensions,
    raw_payload, entity_id
  ) VALUES (
    'smoke_case1_existing', 'sha256:smoke_case1', now(), now(),
    'pdufa_watchlist', 'binary_catalyst', v_rubric_id, 60, '{}'::jsonb,
    jsonb_build_object('ticker','BSMK','drug_name','Olezarsen',
                       'company_name','Bridge Smoke Pharma','pdufa_date','2026-12-31'),
    v_entity_id
  );

  SELECT count(*) INTO v_link_count FROM public.asset_documents ad
  JOIN public.documents d ON d.id=ad.document_id
  WHERE d.extensions->>'signal_id'='smoke_case1_existing' AND ad.asset_id=v_pre_seeded_asset
    AND ad.link_type='primary' AND ad.is_material;
  IF v_link_count <> 1 THEN
    RAISE EXCEPTION 'CASE 1 failed: expected 1 primary link to pre_seeded asset, got %', v_link_count;
  END IF;

  -- ---------- CASE 2: case-insensitive match (drug_name 'OLEZARSEN') ----------
  INSERT INTO public.signals (
    signal_id, source_content_hash, source_date, scan_date,
    signal_type, scoring_profile, rubric_version_id, score, dimensions,
    raw_payload, entity_id
  ) VALUES (
    'smoke_case2_case_insens', 'sha256:smoke_case2', now(), now(),
    'pdufa_watchlist', 'binary_catalyst', v_rubric_id, 55, '{}'::jsonb,
    jsonb_build_object('ticker','BSMK','drug_name','OLEZARSEN',
                       'company_name','Bridge Smoke Pharma','pdufa_date','2026-12-31'),
    v_entity_id
  );

  SELECT count(*) INTO v_link_count FROM public.asset_documents ad
  JOIN public.documents d ON d.id=ad.document_id
  WHERE d.extensions->>'signal_id'='smoke_case2_case_insens' AND ad.asset_id=v_pre_seeded_asset;
  IF v_link_count <> 1 THEN
    RAISE EXCEPTION 'CASE 2 failed: expected 1 link to existing asset on case-insensitive match, got %', v_link_count;
  END IF;

  -- ---------- CASE 3: auto-seed when no match + high-confidence ----------
  -- A different ticker + drug, with sponsor → auto-seed path.
  INSERT INTO public.signals (
    signal_id, source_content_hash, source_date, scan_date,
    signal_type, scoring_profile, rubric_version_id, score, dimensions,
    raw_payload
  ) VALUES (
    'smoke_case3_autoseed', 'sha256:smoke_case3', now(), now(),
    'pdufa_watchlist', 'binary_catalyst', v_rubric_id, 60, '{}'::jsonb,
    jsonb_build_object('ticker','BNEW','drug_name','noveldrug',
                       'company_name','New Pharma Inc','pdufa_date','2026-12-31')
  );

  SELECT id INTO v_seeded_asset FROM public.fda_assets
  WHERE ticker='BNEW' AND lower(drug_name)='noveldrug';
  IF v_seeded_asset IS NULL THEN
    RAISE EXCEPTION 'CASE 3 failed: expected auto-seeded fda_asset for BNEW/noveldrug, got NULL';
  END IF;

  SELECT count(*) INTO v_link_count FROM public.asset_documents ad
  JOIN public.documents d ON d.id=ad.document_id
  WHERE d.extensions->>'signal_id'='smoke_case3_autoseed' AND ad.asset_id=v_seeded_asset;
  IF v_link_count <> 1 THEN
    RAISE EXCEPTION 'CASE 3 failed: expected 1 link to auto-seeded asset, got %', v_link_count;
  END IF;

  -- ---------- CASE 4: operator_flag when no ticker (low-confidence) ----------
  INSERT INTO public.signals (
    signal_id, source_content_hash, source_date, scan_date,
    signal_type, scoring_profile, rubric_version_id, score, dimensions,
    raw_payload
  ) VALUES (
    'smoke_case4_no_ticker', 'sha256:smoke_case4', now(), now(),
    'pre_phase3_readout', 'binary_catalyst', v_rubric_id, 50, '{}'::jsonb,
    jsonb_build_object('drug_name','someinvestigationaldrug',
                       'nct_id','NCT99999999',
                       'company_name_en','Foreign Sponsor Without Ticker')
  );

  SELECT count(*) INTO v_flag_count FROM public.operator_flags
  WHERE signal_id='smoke_case4_no_ticker'
    AND source='bridge_signal_to_v3' AND kind='v3_bridge_no_asset_match'
    AND resolved_at IS NULL;
  IF v_flag_count <> 1 THEN
    RAISE EXCEPTION 'CASE 4 failed: expected 1 open operator_flag, got %', v_flag_count;
  END IF;

  SELECT count(*) INTO v_link_count FROM public.asset_documents ad
  JOIN public.documents d ON d.id=ad.document_id
  WHERE d.extensions->>'signal_id'='smoke_case4_no_ticker';
  IF v_link_count <> 0 THEN
    RAISE EXCEPTION 'CASE 4 failed: expected 0 asset_doc links, got %', v_link_count;
  END IF;

  -- ---------- CASE 5: garbage drug_name "EX-99" treated as missing ----------
  -- The signal has a ticker + sponsor but drug_name is the EDGAR exhibit ID.
  -- Bridge nulls it out, falls through to operator_flag (no usable triple).
  INSERT INTO public.signals (
    signal_id, source_content_hash, source_date, scan_date,
    signal_type, scoring_profile, rubric_version_id, score, dimensions,
    raw_payload
  ) VALUES (
    'smoke_case5_garbage_drug', 'sha256:smoke_case5', now(), now(),
    'pdufa_watchlist', 'binary_catalyst', v_rubric_id, 50, '{}'::jsonb,
    jsonb_build_object('ticker','BBAD','drug_name','EX-99.1',
                       'company_name','Garbage Pharma','pdufa_date','2026-12-31')
  );

  SELECT count(*) INTO v_flag_count FROM public.operator_flags
  WHERE signal_id='smoke_case5_garbage_drug'
    AND source='bridge_signal_to_v3' AND resolved_at IS NULL
    AND (evidence->>'drug_name_was_garbage')::boolean = true;
  IF v_flag_count <> 1 THEN
    RAISE EXCEPTION 'CASE 5 failed: expected 1 flag with drug_name_was_garbage=true, got %', v_flag_count;
  END IF;

  -- ---------- CASE 6: idempotency — re-running bridge on existing signal is a no-op ----------
  SELECT * INTO v_signal FROM public.signals WHERE signal_id='smoke_case1_existing';
  PERFORM public.bridge_signal_to_v3_row(v_signal);
  PERFORM public.bridge_signal_to_v3_row(v_signal);

  SELECT count(*) INTO v_link_count FROM public.asset_documents ad
  JOIN public.documents d ON d.id=ad.document_id
  WHERE d.extensions->>'signal_id'='smoke_case1_existing';
  IF v_link_count <> 1 THEN
    RAISE EXCEPTION 'CASE 6 failed (idempotency): expected still 1 link after 2 re-runs, got %', v_link_count;
  END IF;

  -- ---------- CASE 7: non-FDA scoring_profile is a no-op ----------
  INSERT INTO public.signals (
    signal_id, source_content_hash, source_date, scan_date,
    signal_type, scoring_profile, rubric_version_id, score, dimensions,
    raw_payload
  ) VALUES (
    'smoke_case7_nonfda', 'sha256:smoke_case7', now(), now(),
    'activist_13d', 'activist_governance', v_rubric_id, 70, '{}'::jsonb,
    jsonb_build_object('ticker','MRG','drug_name','irrelevant')
  );

  SELECT count(*) INTO v_doc_count FROM public.documents
  WHERE source='conan_signal' AND extensions->>'signal_id'='smoke_case7_nonfda';
  IF v_doc_count <> 0 THEN
    RAISE EXCEPTION 'CASE 7 failed: non-FDA profile should be a no-op, but got % conan_signal docs', v_doc_count;
  END IF;

  RAISE NOTICE 'bridge_signal_to_v3 smoke test: all 7 cases passed';
END $$;

ROLLBACK;
