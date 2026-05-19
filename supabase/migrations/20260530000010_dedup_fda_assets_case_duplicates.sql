-- Dedup fda_assets case-only duplicates and prevent recurrence.
--
-- Context (2026-05-14 audit):
-- Two fda_assets rows existed for IONS olezarsen — one capitalized ("Olezarsen",
-- older, has indication string from the TRYNGOLZA approval label, no entity_id)
-- and one lowercase ("olezarsen", freshly auto-seeded, has entity_id, no
-- indication). The existing UNIQUE constraint (ticker, drug_name, application_number)
-- is case-sensitive, so the auto-seeder happily inserted a near-duplicate.
--
-- Fix:
--   1. Merge case-only duplicates: pick the strongest row (entity_id presence,
--      then indication presence, then program_status presence, then created_at ASC),
--      COALESCE the missing fields onto it, re-point FK references, drop the
--      losers.
--   2. Replace the case-sensitive unique constraint with a case-insensitive
--      unique INDEX on (ticker, lower(drug_name), application_number).
--
-- FK references (all 10 tables) get re-pointed before deletion. Most use
-- ON DELETE CASCADE so even an aggressive delete would be data-safe, but we
-- re-point explicitly to preserve history (e.g., fda_regulatory_events should
-- follow the survivor).

-- ============================================================================
-- 1. Merge case-only duplicates onto the strongest survivor.
-- ============================================================================

DO $$
DECLARE
  v_group record;
  v_survivor uuid;
  v_loser uuid;
  v_loser_ids uuid[];
BEGIN
  FOR v_group IN
    SELECT ticker, application_number, lower(drug_name) AS lc_drug,
           array_agg(id ORDER BY
             (entity_id IS NOT NULL) DESC,
             (indication IS NOT NULL AND length(indication) > 0) DESC,
             (program_status IS NOT NULL) DESC,
             watch_priority DESC,
             created_at ASC
           ) AS ids_ranked
    FROM public.fda_assets
    GROUP BY ticker, application_number, lower(drug_name)
    HAVING count(*) > 1
  LOOP
    v_survivor := v_group.ids_ranked[1];
    v_loser_ids := v_group.ids_ranked[2:array_length(v_group.ids_ranked, 1)];

    RAISE NOTICE 'fda_assets dedup: ticker=% drug=% survivor=% losers=%',
      v_group.ticker, v_group.lc_drug, v_survivor, v_loser_ids;

    -- Merge missing fields onto survivor (COALESCE each, prefer survivor).
    UPDATE public.fda_assets s
    SET
      generic_name  = COALESCE(s.generic_name, (SELECT generic_name FROM public.fda_assets WHERE id = ANY(v_loser_ids) AND generic_name IS NOT NULL LIMIT 1)),
      mic           = COALESCE(s.mic, (SELECT mic FROM public.fda_assets WHERE id = ANY(v_loser_ids) AND mic IS NOT NULL LIMIT 1)),
      entity_id     = COALESCE(s.entity_id, (SELECT entity_id FROM public.fda_assets WHERE id = ANY(v_loser_ids) AND entity_id IS NOT NULL LIMIT 1)),
      sponsor_name  = COALESCE(s.sponsor_name, (SELECT sponsor_name FROM public.fda_assets WHERE id = ANY(v_loser_ids) AND sponsor_name IS NOT NULL LIMIT 1)),
      indication    = COALESCE(s.indication, (SELECT indication FROM public.fda_assets WHERE id = ANY(v_loser_ids) AND indication IS NOT NULL AND length(indication) > 0 LIMIT 1)),
      mechanism     = COALESCE(s.mechanism, (SELECT mechanism FROM public.fda_assets WHERE id = ANY(v_loser_ids) AND mechanism IS NOT NULL LIMIT 1)),
      application_type = COALESCE(s.application_type, (SELECT application_type FROM public.fda_assets WHERE id = ANY(v_loser_ids) AND application_type IS NOT NULL LIMIT 1)),
      program_status   = COALESCE(s.program_status, (SELECT program_status FROM public.fda_assets WHERE id = ANY(v_loser_ids) AND program_status IS NOT NULL LIMIT 1)),
      indication_normalized = COALESCE(s.indication_normalized, (SELECT indication_normalized FROM public.fda_assets WHERE id = ANY(v_loser_ids) AND indication_normalized IS NOT NULL LIMIT 1)),
      reviewer_panel_id = COALESCE(s.reviewer_panel_id, (SELECT reviewer_panel_id FROM public.fda_assets WHERE id = ANY(v_loser_ids) AND reviewer_panel_id IS NOT NULL LIMIT 1)),
      reference_class_signature = COALESCE(s.reference_class_signature, (SELECT reference_class_signature FROM public.fda_assets WHERE id = ANY(v_loser_ids) AND reference_class_signature IS NOT NULL LIMIT 1)),
      memory_path = COALESCE(s.memory_path, (SELECT memory_path FROM public.fda_assets WHERE id = ANY(v_loser_ids) AND memory_path IS NOT NULL LIMIT 1)),
      next_catalyst_date = COALESCE(s.next_catalyst_date, (SELECT next_catalyst_date FROM public.fda_assets WHERE id = ANY(v_loser_ids) AND next_catalyst_date IS NOT NULL LIMIT 1)),
      extensions = (
        -- Merge JSON: survivor first, losers fill blanks. Stamp a merge audit.
        COALESCE((SELECT jsonb_object_agg(k, v)
                   FROM (
                     SELECT key AS k, value AS v
                     FROM jsonb_each(s.extensions)
                     UNION ALL
                     SELECT key, value
                     FROM public.fda_assets l, jsonb_each(l.extensions)
                     WHERE l.id = ANY(v_loser_ids)
                       AND NOT (s.extensions ? key)
                   ) merged), '{}'::jsonb
        )
        || jsonb_build_object('merged_from', v_loser_ids,
                              'merged_at', now())
      ),
      catalyst_window = COALESCE(
        CASE
          WHEN s.catalyst_window = '{}'::jsonb
            THEN (SELECT catalyst_window FROM public.fda_assets
                  WHERE id = ANY(v_loser_ids) AND catalyst_window <> '{}'::jsonb LIMIT 1)
          ELSE s.catalyst_window
        END,
        s.catalyst_window,
        '{}'::jsonb
      ),
      aging_extensions = COALESCE(
        CASE
          WHEN s.aging_extensions = '{}'::jsonb
            THEN (SELECT aging_extensions FROM public.fda_assets
                  WHERE id = ANY(v_loser_ids) AND aging_extensions <> '{}'::jsonb LIMIT 1)
          ELSE s.aging_extensions
        END,
        s.aging_extensions,
        '{}'::jsonb
      ),
      updated_at = now()
    WHERE s.id = v_survivor;

    -- Re-point each FK table.
    UPDATE public.fda_regulatory_events SET asset_id = v_survivor WHERE asset_id = ANY(v_loser_ids);
    UPDATE public.fda_asset_parties     SET asset_id = v_survivor WHERE asset_id = ANY(v_loser_ids);
    UPDATE public.post_mortem_queue     SET asset_id = v_survivor WHERE asset_id = ANY(v_loser_ids);
    UPDATE public.asset_documents       SET asset_id = v_survivor WHERE asset_id = ANY(v_loser_ids);
    UPDATE public.extracted_facts       SET asset_id = v_survivor WHERE asset_id = ANY(v_loser_ids);
    UPDATE public.eval_harness          SET asset_id = v_survivor WHERE asset_id = ANY(v_loser_ids);
    UPDATE public.orchestrator_runs     SET asset_id = v_survivor WHERE asset_id = ANY(v_loser_ids);
    UPDATE public.convergence_assessments SET asset_id = v_survivor WHERE asset_id = ANY(v_loser_ids);
    UPDATE public.fda_aging_verdicts    SET asset_id = v_survivor WHERE asset_id = ANY(v_loser_ids);

    -- rag_eval_gold uses source_asset_id with ON DELETE SET NULL — re-point too.
    UPDATE public.rag_eval_gold SET source_asset_id = v_survivor
      WHERE source_asset_id = ANY(v_loser_ids);

    -- Delete the losers. CASCADE on the rest is now harmless because we
    -- re-pointed all rows above.
    DELETE FROM public.fda_assets WHERE id = ANY(v_loser_ids);
  END LOOP;
END $$;

-- ============================================================================
-- 2. Replace case-sensitive unique constraint with case-insensitive unique index.
-- ============================================================================
-- Dropping the constraint also drops the underlying unique index, so we
-- rebuild a case-insensitive partial unique INDEX. PostgreSQL accepts this
-- as the ON CONFLICT target via the constraint-name-or-index resolution
-- since the index has a stable, queryable name.
--
-- The bridge function uses `ON CONFLICT (ticker, drug_name, application_number)`
-- which requires the unique constraint to match the targeted column list
-- EXACTLY. Switching to lower(drug_name) means we must also update the
-- bridge's ON CONFLICT clause. We do that in this migration too so the two
-- changes commit atomically.

ALTER TABLE public.fda_assets
  DROP CONSTRAINT IF EXISTS fda_assets_ticker_drug_name_application_number_key;

CREATE UNIQUE INDEX IF NOT EXISTS fda_assets_ticker_lowerdrug_appnum_uniq
  ON public.fda_assets (ticker, lower(drug_name), application_number);

-- ============================================================================
-- 3. Update bridge_signal_to_v3_row to ON CONFLICT against the new index.
-- ============================================================================
-- Reinstall the function with the index-targeted ON CONFLICT. Everything else
-- is identical to the version installed in 20260530000000.

CREATE OR REPLACE FUNCTION public.bridge_signal_to_v3_row(p_sig public.signals)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $func$
DECLARE
  v_supported_types text[] := ARRAY[
    'pre_phase3_readout','pdufa_watchlist','eop2_meeting','fda_decision',
    'pdufa_imminent','pdufa_approaching','pdufa_date_advanced','pdufa_date_delayed'
  ];
  v_seed_hint jsonb;
  v_ticker text;
  v_drug_name text;
  v_sponsor text;
  v_indication text;
  v_nct text;
  v_pdufa_date text;
  v_pcd text;
  v_source_url text;
  v_drug_name_is_garbage boolean;
  v_asset_id uuid;
  v_document_id uuid;
  v_source_doc_id text;
  v_source_content_hash text;
  v_published_at timestamptz;
  v_title text;
  v_confidence numeric(3,2);
  v_program_status text;
  v_high_confidence boolean;
BEGIN
  IF p_sig.scoring_profile IS NULL
     OR p_sig.scoring_profile NOT IN ('binary_catalyst','fda_event') THEN
    RETURN NULL;
  END IF;
  IF NOT (p_sig.signal_type = ANY (v_supported_types)) THEN
    RETURN NULL;
  END IF;

  v_seed_hint := p_sig.raw_payload -> 'auto_seed_fda_asset';

  v_ticker := COALESCE(
    NULLIF(v_seed_hint ->> 'ticker', ''),
    NULLIF(p_sig.raw_payload ->> 'ticker', ''),
    NULLIF(p_sig.raw_payload ->> 'universe_ticker', '')
  );
  v_drug_name := COALESCE(
    NULLIF(v_seed_hint ->> 'drug_name', ''),
    NULLIF(p_sig.raw_payload ->> 'drug_name', ''),
    NULLIF(p_sig.raw_payload ->> 'product_name', ''),
    NULLIF(p_sig.raw_payload ->> 'generic_name', ''),
    NULLIF(p_sig.raw_payload ->> 'asset_name', '')
  );
  v_sponsor := COALESCE(
    NULLIF(v_seed_hint ->> 'sponsor_name', ''),
    NULLIF(p_sig.raw_payload ->> 'sponsor_name', ''),
    NULLIF(p_sig.raw_payload ->> 'company_name', ''),
    NULLIF(p_sig.raw_payload ->> 'company_name_en', ''),
    NULLIF(p_sig.raw_payload ->> 'universe_title', '')
  );
  v_indication := COALESCE(
    NULLIF(v_seed_hint ->> 'indication', ''),
    NULLIF(p_sig.raw_payload ->> 'indication', ''),
    NULLIF(p_sig.raw_payload ->> 'base_rate_key', '')
  );
  v_nct := COALESCE(
    NULLIF(v_seed_hint ->> 'nct_id', ''),
    NULLIF(p_sig.raw_payload ->> 'nct_id', ''),
    NULLIF(p_sig.raw_payload ->> 'phase3_nctid', '')
  );
  v_pdufa_date := NULLIF(p_sig.raw_payload ->> 'pdufa_date', '');
  v_pcd := COALESCE(
    NULLIF(v_seed_hint ->> 'primary_completion_date', ''),
    NULLIF(p_sig.raw_payload ->> 'primary_completion_date', '')
  );
  v_source_url := NULLIF(p_sig.raw_payload ->> 'source_url', '');

  v_drug_name_is_garbage := (
    v_drug_name IS NOT NULL
    AND v_drug_name ~* '^ex[-_]?\d'
  );
  IF v_drug_name_is_garbage THEN
    v_drug_name := NULL;
  END IF;

  v_asset_id := NULL;

  IF p_sig.entity_id IS NOT NULL AND v_drug_name IS NOT NULL THEN
    SELECT id INTO v_asset_id
    FROM public.fda_assets
    WHERE entity_id = p_sig.entity_id
      AND lower(drug_name) = lower(v_drug_name)
    ORDER BY created_at ASC
    LIMIT 1;
  END IF;

  IF v_asset_id IS NULL AND p_sig.entity_id IS NOT NULL THEN
    SELECT id INTO v_asset_id
    FROM (
      SELECT id, count(*) OVER () AS n
      FROM public.fda_assets
      WHERE entity_id = p_sig.entity_id
      ORDER BY created_at ASC
    ) candidates
    WHERE n = 1
    LIMIT 1;
  END IF;

  IF v_asset_id IS NULL AND v_ticker IS NOT NULL AND v_drug_name IS NOT NULL THEN
    SELECT id INTO v_asset_id
    FROM public.fda_assets
    WHERE ticker = v_ticker
      AND lower(drug_name) = lower(v_drug_name)
    ORDER BY (entity_id IS NOT NULL) DESC, created_at ASC
    LIMIT 1;
  END IF;

  IF v_asset_id IS NULL AND v_ticker IS NOT NULL AND v_nct IS NOT NULL THEN
    SELECT id INTO v_asset_id
    FROM public.fda_assets
    WHERE ticker = v_ticker
      AND (extensions ->> 'nct_id') = v_nct
    ORDER BY created_at ASC
    LIMIT 1;
  END IF;

  v_high_confidence := (v_ticker IS NOT NULL AND v_drug_name IS NOT NULL AND v_sponsor IS NOT NULL);

  IF v_asset_id IS NULL AND v_high_confidence THEN
    v_program_status := CASE
      WHEN p_sig.signal_type = 'pre_phase3_readout' THEN 'phase3'
      WHEN p_sig.signal_type = 'eop2_meeting' THEN 'phase2'
      WHEN p_sig.signal_type IN ('pdufa_watchlist','pdufa_imminent','pdufa_approaching',
                                  'pdufa_date_advanced','pdufa_date_delayed','fda_decision') THEN 'filed'
      ELSE NULL
    END;

    INSERT INTO public.fda_assets (
      ticker, drug_name, application_number,
      entity_id, sponsor_name, indication,
      program_status, is_active, watch_priority,
      extensions
    )
    VALUES (
      v_ticker, v_drug_name, '',
      p_sig.entity_id, v_sponsor, v_indication,
      v_program_status, true, 3,
      jsonb_build_object(
        'auto_seeded_from', 'bridge_signal_to_v3',
        'seeding_signal_id', p_sig.signal_id,
        'seeding_signal_type', p_sig.signal_type,
        'nct_id', v_nct,
        'pdufa_date', v_pdufa_date,
        'primary_completion_date', v_pcd,
        'seeded_at', now()
      )
    )
    ON CONFLICT (ticker, (lower(drug_name)), application_number) DO UPDATE
      SET updated_at = now()
    RETURNING id INTO v_asset_id;
  END IF;

  IF v_asset_id IS NULL THEN
    INSERT INTO public.operator_flags (
      severity, source, kind, signal_id, entity_id, title, body, evidence
    )
    VALUES (
      'warn',
      'bridge_signal_to_v3',
      'v3_bridge_no_asset_match',
      p_sig.signal_id,
      p_sig.entity_id,
      format('No fda_asset for %s %s/%s — v3 orchestrator cannot engage',
              p_sig.signal_type,
              COALESCE(v_ticker, '?'),
              COALESCE(v_drug_name, '?')),
      'No matching fda_assets row by (entity_id) or (ticker, lower(drug_name)) or (ticker, nct_id), '
      || 'AND the signal lacks one of ticker/drug_name/sponsor needed for high-confidence auto-seed. '
      || 'Either enrich the scanner so it emits a usable triple, or manually seed an fda_assets row '
      || 'matching ticker + drug_name; the bridge will then auto-link on the next signal INSERT.',
      jsonb_build_object(
        'signal_type', p_sig.signal_type,
        'ticker', v_ticker,
        'drug_name', v_drug_name,
        'drug_name_was_garbage', v_drug_name_is_garbage,
        'sponsor', v_sponsor,
        'indication', v_indication,
        'nct_id', v_nct,
        'pdufa_date', v_pdufa_date,
        'scan_date', p_sig.scan_date,
        'issuer_figi', p_sig.issuer_figi
      )
    )
    -- operator_flags_open_uniq is a unique INDEX (not a constraint), so we use
    -- the unspecified DO NOTHING form, which matches any unique conflict.
    ON CONFLICT DO NOTHING;
    RETURN NULL;
  END IF;

  v_source_doc_id := 'conan_signal:' || p_sig.signal_id;
  v_source_content_hash := 'sha256:conan_signal:' || p_sig.signal_id;
  v_published_at := COALESCE(p_sig.source_date, p_sig.scan_date, now());
  v_title := format('%s — %s %s',
                    p_sig.signal_type,
                    COALESCE(v_ticker, '?'),
                    COALESCE(v_drug_name, '?'));

  INSERT INTO public.documents (
    source, source_doc_id, source_content_hash,
    url, doc_type, raw_text, title, published_at, language, extensions
  )
  VALUES (
    'conan_signal',
    v_source_doc_id,
    v_source_content_hash,
    v_source_url,
    p_sig.signal_type,
    'Conan signal ' || p_sig.signal_type || ' for ' || COALESCE(v_ticker, '?')
      || ' ' || COALESCE(v_drug_name, '?')
      || CASE WHEN v_pdufa_date IS NOT NULL THEN ' PDUFA ' || v_pdufa_date ELSE '' END
      || CASE WHEN v_nct IS NOT NULL THEN ' NCT ' || v_nct ELSE '' END
      || CASE WHEN v_indication IS NOT NULL THEN ' indication=' || v_indication ELSE '' END
      || E'\n\nFull payload:\n' || coalesce(p_sig.raw_payload::text, ''),
    v_title,
    v_published_at,
    'en',
    jsonb_build_object(
      'signal_id', p_sig.signal_id,
      'signal_type', p_sig.signal_type,
      'scoring_profile', p_sig.scoring_profile,
      'entity_id', p_sig.entity_id,
      'issuer_figi', p_sig.issuer_figi,
      'bridge_version', 1,
      'raw_payload', p_sig.raw_payload
    )
  )
  ON CONFLICT (source, source_content_hash) DO UPDATE
    SET extensions = EXCLUDED.extensions
  RETURNING id INTO v_document_id;

  v_confidence := CASE
    WHEN p_sig.signal_type IN ('fda_decision','pdufa_imminent',
                                'pdufa_date_advanced','pdufa_date_delayed') THEN 0.95
    WHEN p_sig.signal_type IN ('pdufa_watchlist','pdufa_approaching') THEN 0.90
    WHEN p_sig.signal_type IN ('pre_phase3_readout','eop2_meeting') THEN 0.85
    ELSE 0.80
  END;

  INSERT INTO public.asset_documents (
    asset_id, document_id, link_type, extraction_method, extraction_confidence,
    is_material, verified_by_pass2
  )
  VALUES (
    v_asset_id, v_document_id, 'primary', 'regex', v_confidence,
    true, false
  )
  ON CONFLICT (asset_id, document_id, link_type) DO NOTHING;

  RETURN v_asset_id;
END;
$func$;
