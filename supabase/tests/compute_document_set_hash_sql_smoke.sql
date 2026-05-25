-- Smoke test for public.compute_document_set_hash_sql.
--
-- Coverage:
--   1. Asset with zero material primary docs → NULL.
--   2. Asset with one material primary doc → md5 of that doc_id::text.
--   3. Asset with multiple material primary docs → order-invariant md5
--      matching the Python/Deno definition (sorted by uuid::text).
--   4. Non-primary or non-material docs are excluded.
--   5. Result matches md5(string_agg(doc_id::text, ',' order by doc_id::text))
--      computed inline (parity self-check).
--
-- Run with:
--   supabase db execute --file supabase/tests/compute_document_set_hash_sql_smoke.sql
--
-- All writes happen inside a transaction that ROLLBACKs at the end.

BEGIN;

DO $$
DECLARE
  v_asset_id      uuid;
  v_doc1          uuid := gen_random_uuid();
  v_doc2          uuid := gen_random_uuid();
  v_doc3          uuid := gen_random_uuid();
  v_doc4          uuid := gen_random_uuid();  -- non-primary, should be excluded
  v_doc5          uuid := gen_random_uuid();  -- non-material primary, excluded
  v_hash_empty    text;
  v_hash_one      text;
  v_hash_three    text;
  v_expected_one  text;
  v_expected_three text;
BEGIN
  -- Set up a synthetic asset. fda_assets columns minimally required.
  INSERT INTO public.fda_assets (
    id, ticker, drug_name, is_active, aging_state
  ) VALUES (
    gen_random_uuid(), 'TST_HASH', 'TEST_DRUG', true, 'watch'
  )
  RETURNING id INTO v_asset_id;

  -- Seed minimal documents rows so the FK on asset_documents.document_id is
  -- satisfied. Columns match the live documents table NOT NULL set:
  -- source, source_doc_id, source_content_hash, doc_type, published_at.
  INSERT INTO public.documents
    (id, source, source_doc_id, source_content_hash, doc_type, published_at)
  VALUES
    (v_doc1, 'press_release', 'hash_test_1', md5('1'), 'press_release', now()),
    (v_doc2, 'press_release', 'hash_test_2', md5('2'), 'press_release', now()),
    (v_doc3, 'press_release', 'hash_test_3', md5('3'), 'press_release', now()),
    (v_doc4, 'press_release', 'hash_test_4', md5('4'), 'press_release', now()),
    (v_doc5, 'press_release', 'hash_test_5', md5('5'), 'press_release', now());

  -- Case 1: asset has zero asset_documents — expect NULL.
  v_hash_empty := public.compute_document_set_hash_sql(v_asset_id);
  IF v_hash_empty IS NOT NULL THEN
    RAISE EXCEPTION 'Case 1 failed: expected NULL for empty asset, got %', v_hash_empty;
  END IF;

  -- Case 2: one material primary doc.
  INSERT INTO public.asset_documents
    (asset_id, document_id, link_type, is_material, extraction_method)
  VALUES (v_asset_id, v_doc1, 'primary', true, 'manual');

  v_hash_one := public.compute_document_set_hash_sql(v_asset_id);
  v_expected_one := md5(v_doc1::text);
  IF v_hash_one IS DISTINCT FROM v_expected_one THEN
    RAISE EXCEPTION 'Case 2 failed: expected % got %', v_expected_one, v_hash_one;
  END IF;

  -- Case 3 + 4: add two more material primary docs, plus excluded variants.
  INSERT INTO public.asset_documents
    (asset_id, document_id, link_type, is_material, extraction_method)
  VALUES
    (v_asset_id, v_doc2, 'primary', true, 'manual'),
    (v_asset_id, v_doc3, 'primary', true, 'manual'),
    (v_asset_id, v_doc4, 'mentions', true, 'manual'),   -- wrong link_type, excluded
    (v_asset_id, v_doc5, 'primary', false, 'manual');   -- not material, excluded

  v_hash_three := public.compute_document_set_hash_sql(v_asset_id);
  v_expected_three := (
    SELECT md5(string_agg(d::text, ',' ORDER BY d::text))
      FROM unnest(ARRAY[v_doc1, v_doc2, v_doc3]) AS d
  );
  IF v_hash_three IS DISTINCT FROM v_expected_three THEN
    RAISE EXCEPTION 'Case 3 failed: expected % got %', v_expected_three, v_hash_three;
  END IF;

  -- Case 5: parity self-check — call inline form against function form once
  -- more to ensure ordering is stable (uuids cast as text, sorted).
  IF v_hash_three IS DISTINCT FROM (
    SELECT md5(string_agg(document_id::text, ',' ORDER BY document_id::text))
      FROM public.asset_documents
     WHERE asset_id = v_asset_id
       AND link_type = 'primary'
       AND is_material IS TRUE
  ) THEN
    RAISE EXCEPTION 'Case 5 failed: function diverged from inline definition';
  END IF;

  RAISE NOTICE 'compute_document_set_hash_sql_smoke: all 5 cases passed';
END $$;

ROLLBACK;
