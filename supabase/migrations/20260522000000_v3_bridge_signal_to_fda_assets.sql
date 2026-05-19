-- v3 Bridge: emitted FDA-family signals → fda_assets → asset_documents primary link.
--
-- Context (memory ref: binary_catalyst_scoring_gaps.md, 2026-05-14):
-- Reactor commit 17b5ecf (2026-05-07) routes any signal with
-- scoring_profile IN ('binary_catalyst','fda_event') into the v3 orchestrator
-- queue instead of the legacy convergence pipeline. The v3 reactor branch
-- fires on asset_documents INSERTs (link_type='primary' AND is_material=true),
-- which enqueues an orchestrator_runs row.
--
-- Problem: nothing actually creates that asset_documents row from a signal.
-- Last 30d: 196 binary_catalyst signals emitted, ~25% match an fda_assets row
-- by (entity_id) or (ticker, lower(drug_name)), but ZERO of the 8 signal_types
-- listed below produced an asset_documents primary+material link. The
-- short-circuit "succeeds" (signals carry scoring_profile='binary_catalyst')
-- but the v3 pipeline starves.
--
-- Fix: this migration installs an AFTER INSERT trigger on `signals` that, for
-- the listed signal_types, (1) resolves a matching fda_assets row (by
-- entity_id, then ticker+drug_name case-insensitive, then ticker+nct_id),
-- (2) auto-seeds an fda_assets stub when no match exists AND the signal is
-- high-confidence (has ticker + drug_name + sponsor + a believable drug_name
-- shape), (3) writes an operator_flag when no match exists and the signal is
-- low-confidence, and (4) synthesizes a `documents` row + `asset_documents`
-- primary+material link, which causes call_reactor_assetdoc() to enqueue an
-- orchestrator_runs row through the existing v3 supply line.
--
-- It also exposes a SECURITY DEFINER backfill function
-- bridge_signal_to_v3_backfill(p_since, p_limit) for replaying the 130
-- orphaned signals from the last 30d. The trigger and backfill share the
-- same core function bridge_signal_to_v3_row(signal_row), so a row
-- exercised either way goes through identical resolution.
--
-- This migration also supersedes the narrower
-- auto_seed_fda_asset_from_signal trigger (introduced
-- 20260519000000_auto_seed_fda_asset_from_pre_phase3.sql), which only
-- handled signal_type='pre_phase3_readout' AND only seeded fda_assets but
-- did NOT create the asset_documents link. We drop it at the bottom.
--
-- Out of scope: the reactor short-circuit at supabase/functions/reactor/index.ts:300-310
-- is intentional per memory `strategic_pivot_fda_depth.md`. This migration
-- repairs the supply line INTO the v3 orchestrator, not the routing decision.

-- ============================================================================
-- 0. Extend operator_flags.source CHECK to allow 'bridge_signal_to_v3'
-- ============================================================================
-- operator_flags has a whitelist CHECK on `source` so unrelated callers can't
-- pollute the open-flag dashboard. The bridge needs its own source so a
-- "no fda_asset match" flag is routed to the right operator workflow.

DO $$
DECLARE
  v_def text;
BEGIN
  SELECT pg_get_constraintdef(oid) INTO v_def
  FROM pg_constraint
  WHERE conrelid = 'public.operator_flags'::regclass
    AND conname = 'operator_flags_source_check';

  IF v_def IS NULL THEN
    RAISE NOTICE 'operator_flags_source_check not found — skipping';
  ELSIF v_def LIKE '%bridge_signal_to_v3%' THEN
    RAISE NOTICE 'bridge_signal_to_v3 already in operator_flags_source_check';
  ELSE
    ALTER TABLE public.operator_flags DROP CONSTRAINT operator_flags_source_check;
    ALTER TABLE public.operator_flags ADD CONSTRAINT operator_flags_source_check
      CHECK (source = ANY (ARRAY[
        'translation_health','scanner_probe','scanner_liveness','convergence_qa',
        'candidate_aging','thesis_writer','reactor','reporting_weekly',
        'litigation_baselines','edgar_runtime_health','scanner_failure_streak',
        'rollback_monitor','orchestrator_cost','thesis_jobs','manual',
        'v3_pipeline_watchdog','aging_review','challenger_retro',
        'constitutional_check','memory_writeback','tier2_quality',
        'orphan_sweeper','backfill_v3_assessment',
        'bridge_signal_to_v3'
      ]));
  END IF;
END $$;

-- ============================================================================
-- 1. Extend documents.source CHECK to allow 'conan_signal'
-- ============================================================================
-- The synthetic document we write for each bridged signal needs an honest
-- provenance label. 'conan_signal' marks the document as a Conan-internal
-- summary of a binary_catalyst signal (as opposed to a fetched FDA/EDGAR/news
-- document). Orchestrator Stage 1 should filter on this source if it wants to
-- distinguish raw-doc evidence from signal evidence.

DO $$
DECLARE
  v_constraint_def text;
BEGIN
  SELECT pg_get_constraintdef(oid) INTO v_constraint_def
  FROM pg_constraint
  WHERE conrelid = 'public.documents'::regclass
    AND conname = 'documents_source_check';

  IF v_constraint_def IS NULL THEN
    RAISE NOTICE 'documents_source_check not found — skipping (constraint may have a different name)';
  ELSIF v_constraint_def LIKE '%conan_signal%' THEN
    RAISE NOTICE 'conan_signal already present in documents_source_check';
  ELSE
    ALTER TABLE public.documents DROP CONSTRAINT documents_source_check;
    ALTER TABLE public.documents ADD CONSTRAINT documents_source_check
      CHECK (source = ANY (ARRAY[
        'edgar','federal_register','openfda','clinicaltrials','dailymed','faers',
        'fda_advisory','fda_warning_letter','fda_483','pubmed','biorxiv','medrxiv',
        'polygon_news','press_release','conan_signal'
      ]));
  END IF;
END $$;

-- ============================================================================
-- 2. Core bridge function — accepts a signals row, performs resolution.
-- ============================================================================
-- Returns the asset_id we attached to (or NULL when the signal was flagged
-- for operator triage). All work is idempotent: re-running the bridge on the
-- same signal_id is a no-op.

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
  -- Gate 1: only handle FDA-family scoring profiles.
  IF p_sig.scoring_profile IS NULL
     OR p_sig.scoring_profile NOT IN ('binary_catalyst','fda_event') THEN
    RETURN NULL;
  END IF;
  -- Gate 2: only the 8 signal_types listed in the v3 supply spec.
  IF NOT (p_sig.signal_type = ANY (v_supported_types)) THEN
    RETURN NULL;
  END IF;

  v_seed_hint := p_sig.raw_payload -> 'auto_seed_fda_asset';

  -- Extract canonical fields, preferring auto_seed_fda_asset hint when present
  -- (set by pre_phase3_readout_scanner._build_signal when SEC issuer resolved).
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

  -- Detect EDGAR exhibit-number false positives ("EX-99", "EX-99.1", etc.)
  -- These come from naive 8-K text parsing and are not real drug names.
  v_drug_name_is_garbage := (
    v_drug_name IS NOT NULL
    AND v_drug_name ~* '^ex[-_]?\d'
  );
  IF v_drug_name_is_garbage THEN
    v_drug_name := NULL;
  END IF;

  -- ------------------------------------------------------------------
  -- Resolve fda_assets row.
  -- Match priority:
  --   (a) entity_id + lower(drug_name) match — strongest
  --   (b) entity_id only — when the asset's drug_name is missing/mismatched but
  --       a single asset belongs to the entity. Defensive (skipped if multiple).
  --   (c) ticker + lower(drug_name) — entity_id may be NULL on either side.
  --   (d) ticker + nct_id (via extensions.nct_id) — pre_phase3 carries NCT.
  -- ------------------------------------------------------------------
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

  -- ------------------------------------------------------------------
  -- No match: try auto-seed for high-confidence signals, else operator_flag.
  -- High confidence = has ticker + drug_name + sponsor.
  -- (drug_name garbage was already nulled out above.)
  -- ------------------------------------------------------------------
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
    ON CONFLICT (ticker, drug_name, application_number) DO UPDATE
      SET updated_at = now()  -- no-op write to surface the existing id
    RETURNING id INTO v_asset_id;
  END IF;

  IF v_asset_id IS NULL THEN
    -- Low-confidence (or garbage drug_name): flag for operator triage.
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

  -- ------------------------------------------------------------------
  -- Synthesize the `documents` row representing this signal.
  -- Deterministic source_content_hash on signal_id so re-runs and the
  -- backfill function don't create duplicate documents.
  -- ------------------------------------------------------------------
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
    -- raw_text: a compact, human-readable digest of the signal for Stage 1
    -- extractor input. Full raw_payload is also in extensions.signal.raw_payload.
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

  -- Per-signal-type confidence. fda_decision/pdufa_imminent are the strongest
  -- (a confirmed FDA event); pre_phase3_readout/eop2_meeting are weakest
  -- (we're inferring from trial enrollment + completion date heuristics).
  v_confidence := CASE
    WHEN p_sig.signal_type IN ('fda_decision','pdufa_imminent',
                                'pdufa_date_advanced','pdufa_date_delayed') THEN 0.95
    WHEN p_sig.signal_type IN ('pdufa_watchlist','pdufa_approaching') THEN 0.90
    WHEN p_sig.signal_type IN ('pre_phase3_readout','eop2_meeting') THEN 0.85
    ELSE 0.80
  END;

  -- The primary+material link is what call_reactor_assetdoc() observes; this
  -- INSERT is the event that wakes the v3 orchestrator supply line up.
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

COMMENT ON FUNCTION public.bridge_signal_to_v3_row(public.signals) IS
  'v3 supply line: resolve an FDA-family signal row to an fda_assets row '
  '(match → auto-seed → operator_flag) and create the asset_documents '
  'primary+material link that wakes the v3 orchestrator. Idempotent. '
  'See migration 20260522000000.';

-- ============================================================================
-- 3. INSERT trigger that calls the core function.
-- ============================================================================
-- After-insert ensures NEW is fully visible to the function (it queries
-- signals by signal_id via the public.signals row argument). Errors inside
-- the function would roll back the signals INSERT, which is fine: the bridge
-- is part of the signal's commit boundary.

CREATE OR REPLACE FUNCTION public.bridge_signal_to_v3_trigger()
RETURNS trigger
LANGUAGE plpgsql
AS $func$
BEGIN
  PERFORM public.bridge_signal_to_v3_row(NEW);
  RETURN NEW;
END;
$func$;

DROP TRIGGER IF EXISTS bridge_signal_to_v3_tg ON public.signals;
CREATE TRIGGER bridge_signal_to_v3_tg
  AFTER INSERT ON public.signals
  FOR EACH ROW
  EXECUTE FUNCTION public.bridge_signal_to_v3_trigger();

-- ============================================================================
-- 4. Backfill function — replay the bridge over historical signals.
-- ============================================================================
-- Returns a count of (rows_processed, rows_linked, rows_flagged). Safe to
-- run repeatedly: bridge_signal_to_v3_row is idempotent on document hashes,
-- asset_documents unique key, and operator_flags open-flag uniqueness.

CREATE OR REPLACE FUNCTION public.bridge_signal_to_v3_backfill(
  p_since timestamptz DEFAULT now() - interval '30 days',
  p_limit int DEFAULT 1000
)
RETURNS TABLE (
  rows_seen int,
  rows_linked int,
  rows_flagged int
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $func$
DECLARE
  v_sig public.signals;
  v_seen int := 0;
  v_linked int := 0;
  v_flagged int := 0;
  v_asset_id uuid;
  v_pre_flag_count bigint;
  v_post_flag_count bigint;
BEGIN
  FOR v_sig IN
    SELECT *
    FROM public.signals
    WHERE scan_date >= p_since
      AND scoring_profile IN ('binary_catalyst','fda_event')
      AND signal_type IN (
        'pre_phase3_readout','pdufa_watchlist','eop2_meeting','fda_decision',
        'pdufa_imminent','pdufa_approaching','pdufa_date_advanced','pdufa_date_delayed'
      )
    ORDER BY scan_date ASC
    LIMIT p_limit
  LOOP
    v_seen := v_seen + 1;

    -- Cheap accounting: did the function open a new operator_flag for this signal?
    SELECT count(*) INTO v_pre_flag_count
    FROM public.operator_flags
    WHERE signal_id = v_sig.signal_id
      AND source = 'bridge_signal_to_v3'
      AND resolved_at IS NULL;

    v_asset_id := public.bridge_signal_to_v3_row(v_sig);

    IF v_asset_id IS NOT NULL THEN
      v_linked := v_linked + 1;
    ELSE
      SELECT count(*) INTO v_post_flag_count
      FROM public.operator_flags
      WHERE signal_id = v_sig.signal_id
        AND source = 'bridge_signal_to_v3'
        AND resolved_at IS NULL;
      IF v_post_flag_count > v_pre_flag_count THEN
        v_flagged := v_flagged + 1;
      END IF;
    END IF;
  END LOOP;

  RETURN QUERY SELECT v_seen, v_linked, v_flagged;
END;
$func$;

COMMENT ON FUNCTION public.bridge_signal_to_v3_backfill(timestamptz, int) IS
  'Replay bridge_signal_to_v3_row over historical signals. Idempotent. '
  'Returns (rows_seen, rows_linked, rows_flagged). Default window: 30d.';

-- ============================================================================
-- 5. Supersede the narrower auto_seed_fda_asset_from_signal trigger.
-- ============================================================================
-- The new bridge covers pre_phase3_readout (same scope) AND every other
-- FDA-family signal_type, AND creates the asset_documents link the old
-- trigger never knew about. Leaving the old trigger in place would mean two
-- INSERT-INTO-fda_assets attempts (the second no-ops via ON CONFLICT), no
-- correctness risk but dead-code drift.

DROP TRIGGER IF EXISTS auto_seed_fda_asset_from_signal_tg ON public.signals;
DROP FUNCTION IF EXISTS public.auto_seed_fda_asset_from_signal();
