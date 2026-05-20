-- Asset-linker edge prefilter — recall fix.
--
-- Follow-on to 20260601000000_skill_asset_linker_cutover.sql and
-- 20260601000010_skill_cutover_operational_guardrails.sql, which already
-- shipped with a tighter design. Those files are pinned to their applied
-- timestamps; redefining the affected objects here ensures the recall fix
-- actually deploys.
--
-- Why: post-cutover, asset_linker burn (#101) was traced to a recall gap.
-- The deterministic prefilter joined only Layer 2 (fda_asset_aliases), so
-- it could not match documents that referenced an asset by its Layer 1
-- drug_name/generic_name/sponsor_name — and the default queue excluded
-- far-future clinical-trial docs entirely. Both fixed here:
--
--   1. v_asset_alias_lookup — single read surface unifying Layer 1
--      (fda_assets.drug_name / generic_name / sponsor_name, filtered to
--      drop placeholder values) with Layer 2 (active fda_asset_aliases).
--      Tickers stay out — they need case-sensitive regex.
--   2. asset_linker_alias_set_hash() — now folds full Layer 1 asset fields
--      into the hash so a drug_name update invalidates the prefilter (was
--      ticker-only before, missing drug-rename invalidation).
--   3. fn_generate_doc_asset_candidates() — tsvector path joins
--      v_asset_alias_lookup instead of fda_asset_aliases directly, picking
--      up Layer 1 names. Sweeper now drains current docs before far-future
--      backlog rows.
--   4. v_asset_linker_skill_queue — replaces the previous future-date WHERE
--      filter with an ORDER BY that demotes (rather than excludes)
--      far-future and sponsor-only edges. Backlog stays visible.
--   5. _v3_pipeline_watchdog() — gains two probes: fda_asset_aliases_empty
--      and asset_linker_skill_queue_ticker_only — both catch Layer-1
--      recall regressions before they burn skill budget.


-- ============================================================
-- 1. v_asset_alias_lookup — Layer 1 + Layer 2 alias read surface
-- ============================================================

CREATE OR REPLACE VIEW public.v_asset_alias_lookup AS
SELECT
  fa.id AS asset_id,
  fa.drug_name AS alias,
  lower(trim(fa.drug_name)) AS alias_normalized,
  'drug_name'::text AS alias_kind,
  phraseto_tsquery('simple', lower(trim(fa.drug_name))) AS alias_tsquery
FROM public.v_asset_linker_skill_assets fa
WHERE NULLIF(trim(coalesce(fa.drug_name, '')), '') IS NOT NULL
  AND lower(trim(fa.drug_name)) NOT IN (
    'peptide', 'concept', 'default', 'ex-99', '(auto-discovered)',
    'nucleotide', 'drug', 'tablet', 'capsule', 'injection'
  )

UNION
SELECT
  fa.id AS asset_id,
  fa.generic_name AS alias,
  lower(trim(fa.generic_name)) AS alias_normalized,
  'generic'::text AS alias_kind,
  phraseto_tsquery('simple', lower(trim(fa.generic_name))) AS alias_tsquery
FROM public.v_asset_linker_skill_assets fa
WHERE NULLIF(trim(coalesce(fa.generic_name, '')), '') IS NOT NULL
  AND lower(trim(fa.generic_name)) NOT IN (
    'peptide', 'concept', 'default', 'ex-99', '(auto-discovered)',
    'nucleotide', 'drug', 'tablet', 'capsule', 'injection'
  )

UNION
SELECT
  fa.id AS asset_id,
  fa.sponsor_name AS alias,
  lower(trim(fa.sponsor_name)) AS alias_normalized,
  'sponsor_alias'::text AS alias_kind,
  phraseto_tsquery('simple', lower(trim(fa.sponsor_name))) AS alias_tsquery
FROM public.v_asset_linker_skill_assets fa
WHERE NULLIF(trim(coalesce(fa.sponsor_name, '')), '') IS NOT NULL
  AND lower(trim(fa.sponsor_name)) NOT IN (
    'peptide', 'concept', 'default', 'ex-99', '(auto-discovered)',
    'nucleotide', 'drug', 'tablet', 'capsule', 'injection'
  )

UNION
SELECT
  a.asset_id,
  a.alias,
  a.alias_normalized,
  a.alias_kind,
  a.alias_tsquery
FROM public.fda_asset_aliases a
WHERE a.active = true;

COMMENT ON VIEW public.v_asset_alias_lookup IS
  'Layer 1 fda_assets aliases (drug/generic/sponsor) plus active supplemental '
  'fda_asset_aliases. Excludes ticker aliases, which are matched case-sensitively '
  'from fda_assets.ticker in fn_generate_doc_asset_candidates.';


-- ============================================================
-- 2. asset_linker_alias_set_hash() — fold Layer 1 asset fields
-- ============================================================

CREATE OR REPLACE FUNCTION public.asset_linker_alias_set_hash()
RETURNS text
LANGUAGE sql
STABLE
SET search_path = public
AS $$
  SELECT md5(
    COALESCE(
      (SELECT string_agg(
        a.asset_id::text || '|' || a.alias_normalized || '|' || a.alias_kind,
        ',' ORDER BY a.asset_id, a.alias_normalized, a.alias_kind
      ) FROM public.fda_asset_aliases a WHERE a.active = true),
      ''
    ) || '#' ||
    COALESCE(
      (SELECT string_agg(
        fa.id::text || '|' ||
        coalesce(fa.ticker, '') || '|' ||
        coalesce(fa.drug_name, '') || '|' ||
        coalesce(fa.generic_name, '') || '|' ||
        coalesce(fa.sponsor_name, ''),
        ',' ORDER BY fa.id
      )
       FROM public.v_asset_linker_skill_assets fa),
      ''
    )
  );
$$;


-- ============================================================
-- 3. fn_generate_doc_asset_candidates() — tsvector via v_asset_alias_lookup
-- ============================================================

CREATE OR REPLACE FUNCTION public.fn_generate_doc_asset_candidates(
  p_limit int DEFAULT 1000
)
RETURNS jsonb
LANGUAGE plpgsql
SET search_path = public, extensions
AS $$
DECLARE
  v_hash text;
  v_docs_scanned int := 0;
  v_edges_emitted int := 0;
BEGIN
  v_hash := public.asset_linker_alias_set_hash();

  WITH target_docs AS (
    SELECT d.id, d.raw_text, d.title, d.raw_text_tsv
    FROM public.documents d
    WHERE NOT EXISTS (
      SELECT 1 FROM public.doc_asset_prefilter_runs r
      WHERE r.document_id = d.id
        AND r.alias_set_hash = v_hash
    )
    ORDER BY
      CASE WHEN d.published_at > now() + interval '30 days' THEN 1 ELSE 0 END,
      d.published_at DESC NULLS LAST,
      d.id
    LIMIT p_limit
  ),
  -- tsvector path: Layer 1 drug/generic/sponsor names plus Layer 2 brand,
  -- generic, drug_name, sponsor_alias, sponsor_stem, abbreviation.
  -- GIN-indexed; bulk of the matching work runs here.
  tsv_hits AS (
    SELECT
      td.id AS document_id,
      a.asset_id,
      jsonb_build_object(
        'alias',            a.alias,
        'alias_normalized', a.alias_normalized,
        'kind',             a.alias_kind,
        'path',             'tsvector'
      ) AS hit
    FROM target_docs td
    JOIN public.v_asset_alias_lookup a
      ON a.alias_kind NOT IN ('nct_id', 'code')
     AND td.raw_text_tsv @@ a.alias_tsquery
  ),
  -- exact + word-boundary path: NCT IDs and drug codes — fuzz unacceptable.
  exact_hits AS (
    SELECT
      td.id AS document_id,
      a.asset_id,
      jsonb_build_object(
        'alias',            a.alias,
        'alias_normalized', a.alias_normalized,
        'kind',             a.alias_kind,
        'path',             'exact_word_boundary'
      ) AS hit
    FROM target_docs td
    JOIN public.fda_asset_aliases a
      ON a.active = true
     AND a.alias_kind IN ('nct_id', 'code')
     AND (coalesce(td.raw_text, '') || ' ' || coalesce(td.title, ''))
         ~* ('\m' || a.alias_normalized || '\M')
  ),
  -- ticker path: case-sensitive word-boundary against fda_assets.ticker.
  -- Tickers are NOT in fda_asset_aliases (would case-fold via tsvector and
  -- collide with English words like "ions"). Pulled directly here.
  ticker_hits AS (
    SELECT
      td.id AS document_id,
      fa.id AS asset_id,
      jsonb_build_object(
        'alias', fa.ticker,
        'kind',  'ticker',
        'path',  'case_sensitive_word_boundary'
      ) AS hit
    FROM target_docs td
    JOIN public.v_asset_linker_skill_assets fa
      ON (coalesce(td.raw_text, '') || ' ' || coalesce(td.title, ''))
         ~ ('\m' || fa.ticker || '\M')
  ),
  all_hits AS (
    SELECT * FROM tsv_hits
    UNION ALL SELECT * FROM exact_hits
    UNION ALL SELECT * FROM ticker_hits
  ),
  aggregated AS (
    SELECT
      document_id,
      asset_id,
      jsonb_agg(hit ORDER BY hit->>'kind', hit->>'alias_normalized')
        AS matched_aliases,
      LEAST(count(DISTINCT (hit->>'kind')), 32767)::smallint AS match_strength
    FROM all_hits
    GROUP BY document_id, asset_id
  ),
  inserted_candidates AS (
    INSERT INTO public.doc_asset_candidates (
      document_id, asset_id, matched_aliases, match_strength, alias_set_hash
    )
    SELECT document_id, asset_id, matched_aliases, match_strength, v_hash
    FROM aggregated
    ON CONFLICT (document_id, asset_id, alias_set_hash) DO UPDATE
      SET matched_aliases = EXCLUDED.matched_aliases,
          match_strength  = EXCLUDED.match_strength,
          matched_at      = now()
    RETURNING document_id, asset_id
  ),
  scanned_marker AS (
    INSERT INTO public.doc_asset_prefilter_runs (
      document_id, alias_set_hash, candidate_count
    )
    SELECT
      td.id,
      v_hash,
      coalesce(
        (SELECT count(*) FROM aggregated WHERE document_id = td.id),
        0
      )
    FROM target_docs td
    ON CONFLICT (document_id, alias_set_hash) DO UPDATE
      SET candidate_count = EXCLUDED.candidate_count,
          scanned_at      = now()
    RETURNING document_id
  )
  SELECT
    (SELECT count(*) FROM scanned_marker),
    (SELECT count(*) FROM inserted_candidates)
  INTO v_docs_scanned, v_edges_emitted;

  RETURN jsonb_build_object(
    'docs_scanned',   v_docs_scanned,
    'edges_emitted',  v_edges_emitted,
    'alias_set_hash', v_hash
  );
END;
$$;

REVOKE ALL ON FUNCTION public.fn_generate_doc_asset_candidates(int) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.fn_generate_doc_asset_candidates(int) FROM anon;
REVOKE ALL ON FUNCTION public.fn_generate_doc_asset_candidates(int) FROM authenticated;


-- ============================================================
-- 4. v_asset_linker_skill_queue — demote far-future / sponsor-only,
--    don't exclude
-- ============================================================

CREATE OR REPLACE VIEW public.v_asset_linker_skill_queue AS
WITH current_hash AS (
  SELECT public.asset_linker_alias_set_hash() AS alias_set_hash
)
SELECT
  e.id AS candidate_id,
  e.document_id,
  e.asset_id,
  e.matched_aliases,
  e.match_strength,
  e.alias_set_hash,
  d.source,
  d.doc_type,
  d.title,
  d.url,
  d.published_at,
  d.raw_text_tokens,
  d.raw_text,
  d.storage_path,
  d.extensions,
  a.ticker,
  a.drug_name,
  a.generic_name,
  a.sponsor_name,
  a.indication
FROM public.doc_asset_candidates e
JOIN public.documents d ON d.id = e.document_id
JOIN public.v_asset_linker_skill_assets a ON a.id = e.asset_id
CROSS JOIN current_hash h
WHERE e.analyzed_at IS NULL
  AND e.alias_set_hash = h.alias_set_hash
  AND NOT EXISTS (
    SELECT 1
      FROM public.document_asset_linker_attempts att
     WHERE att.document_id = e.document_id
       AND att.asset_id = e.asset_id
       AND att.alias_set_hash = e.alias_set_hash
       AND att.status = 'error'
       AND att.created_at > now() - interval '24 hours'
  )
ORDER BY
  e.match_strength DESC,
  CASE WHEN EXISTS (
    SELECT 1
    FROM jsonb_array_elements(e.matched_aliases) AS hit
    WHERE hit->>'kind' IN (
      'drug_name', 'generic', 'brand', 'nct_id', 'code', 'ticker', 'abbreviation'
    )
  ) THEN 0 ELSE 1 END,
  CASE WHEN d.published_at > now() + interval '30 days' THEN 1 ELSE 0 END,
  d.published_at DESC NULLS LAST,
  e.matched_at;

COMMENT ON VIEW public.v_asset_linker_skill_queue IS
  'Edge-shaped queue: (document, asset) candidate pairs the local Cursor '
  'asset-linker skill should analyze. Pre-filtered by the deterministic '
  'fn_generate_doc_asset_candidates sweeper; the skill spends LLM tokens '
  'only on these pairs. Filtered to current alias_set_hash; rows with a '
  'recent error attempt are held off for 24h. Prioritizes high-signal aliases '
  'and current documents ahead of sponsor-only or far-future backlog edges.';


-- ============================================================
-- 5. _v3_pipeline_watchdog() — add Layer-1 recall probes
-- ============================================================

CREATE OR REPLACE FUNCTION public._v3_pipeline_watchdog()
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
DECLARE
  v_results jsonb := '{}'::jsonb;
  v_n integer;
  v_sample jsonb;
  v_cost numeric;
BEGIN
  SELECT count(*),
         COALESCE(jsonb_agg(jsonb_build_object(
           'run_id', id,
           'asset_id', asset_id,
           'scheduled_at', scheduled_at,
           'age_seconds', extract(epoch from (now() - scheduled_at))::int
         )) FILTER (WHERE rn <= 5), '[]'::jsonb)
    INTO v_n, v_sample
    FROM (
      SELECT id, asset_id, scheduled_at,
             row_number() OVER (ORDER BY scheduled_at) AS rn
        FROM public.orchestrator_runs
       WHERE status = 'pending'
         AND tier = 1
         AND scheduled_at < now() - interval '15 minutes'
    ) s;

  IF v_n > 0 THEN
    INSERT INTO public.operator_flags (severity, source, kind, title, body, evidence)
    VALUES (
      'critical',
      'v3_pipeline_watchdog',
      'drainer_tier1_pending_too_long',
      'Tier-1 orchestrator drainer not consuming queue',
      v_n || ' tier=1 row(s) pending > 15min. Check cron.job_run_details + net._http_response.',
      jsonb_build_object('count', v_n, 'sample', v_sample, 'threshold_minutes', 15)
    )
    ON CONFLICT DO NOTHING;
  ELSE
    UPDATE public.operator_flags
       SET resolved_at = now(),
           resolved_note = 'auto-resolved by _v3_pipeline_watchdog: no tier=1 pending rows older than 15 minutes',
           updated_at = now()
     WHERE source = 'v3_pipeline_watchdog'
       AND kind = 'drainer_tier1_pending_too_long'
       AND resolved_at IS NULL;
  END IF;
  v_results := v_results || jsonb_build_object('drainer_tier1_pending_too_long', v_n);

  SELECT count(*),
         COALESCE(jsonb_agg(jsonb_build_object(
           'run_id', id,
           'asset_id', asset_id,
           'scheduled_at', scheduled_at
         )) FILTER (WHERE rn <= 5), '[]'::jsonb)
    INTO v_n, v_sample
    FROM (
      SELECT id, asset_id, scheduled_at,
             row_number() OVER (ORDER BY scheduled_at) AS rn
        FROM public.orchestrator_runs
       WHERE status = 'pending'
         AND tier = 2
         AND scheduled_at < now() - interval '6 hours'
    ) s;

  IF v_n > 0 THEN
    INSERT INTO public.operator_flags (severity, source, kind, title, body, evidence)
    VALUES (
      'warn',
      'v3_pipeline_watchdog',
      'drainer_tier2_pending_too_long',
      'Tier-2 Cowork queue not draining',
      v_n || ' tier=2 row(s) pending > 6h.',
      jsonb_build_object('count', v_n, 'sample', v_sample, 'threshold_hours', 6)
    )
    ON CONFLICT DO NOTHING;
  ELSE
    UPDATE public.operator_flags
       SET resolved_at = now(),
           resolved_note = 'auto-resolved by _v3_pipeline_watchdog: no tier=2 pending rows older than 6 hours',
           updated_at = now()
     WHERE source = 'v3_pipeline_watchdog'
       AND kind = 'drainer_tier2_pending_too_long'
       AND resolved_at IS NULL;
  END IF;
  v_results := v_results || jsonb_build_object('drainer_tier2_pending_too_long', v_n);

  SELECT count(*) INTO v_n
    FROM public.v_asset_linker_skill_queue;

  IF v_n > 500 THEN
    INSERT INTO public.operator_flags (severity, source, kind, title, body, evidence)
    VALUES (
      'warn',
      'v3_pipeline_watchdog',
      'asset_linker_skill_queue_backlog',
      'Local asset-linker edge queue is growing',
      v_n || ' prefiltered (document, asset) edges are pending local skill classification.',
      jsonb_build_object('count', v_n, 'threshold', 500, 'mode', 'cursor_skill_edge_queue')
    )
    ON CONFLICT DO NOTHING;
  ELSE
    UPDATE public.operator_flags
       SET resolved_at = now(),
           resolved_note = 'auto-resolved by _v3_pipeline_watchdog: local asset-linker queue below threshold',
           updated_at = now()
     WHERE source = 'v3_pipeline_watchdog'
       AND kind = 'asset_linker_skill_queue_backlog'
       AND resolved_at IS NULL;
  END IF;
  v_results := v_results || jsonb_build_object('asset_linker_skill_queue_backlog', v_n);

  SELECT count(*),
         COALESCE(jsonb_agg(jsonb_build_object(
           'candidate_id', candidate_id,
           'ticker', ticker,
           'drug_name', drug_name,
           'published_at', published_at,
           'match_strength', match_strength
         )) FILTER (WHERE rn <= 10), '[]'::jsonb)
    INTO v_n, v_sample
    FROM (
      SELECT candidate_id, ticker, drug_name, published_at, match_strength,
             row_number() OVER (ORDER BY published_at DESC NULLS LAST) AS rn
        FROM public.v_asset_linker_skill_queue
       WHERE published_at > now() + interval '30 days'
    ) s;

  IF v_n > 50 THEN
    INSERT INTO public.operator_flags (severity, source, kind, title, body, evidence)
    VALUES (
      'warn',
      'v3_pipeline_watchdog',
      'asset_linker_skill_queue_future_dated',
      'Local asset-linker queue contains many future-dated edges',
      v_n || ' pending edges have published_at more than 30 days in the future. Prioritize current FDA signals before these.',
      jsonb_build_object('count', v_n, 'threshold', 50, 'sample', v_sample)
    )
    ON CONFLICT DO NOTHING;
  ELSE
    UPDATE public.operator_flags
       SET resolved_at = now(),
           resolved_note = 'auto-resolved by _v3_pipeline_watchdog: future-dated edge count below threshold',
           updated_at = now()
     WHERE source = 'v3_pipeline_watchdog'
       AND kind = 'asset_linker_skill_queue_future_dated'
       AND resolved_at IS NULL;
  END IF;
  v_results := v_results || jsonb_build_object('asset_linker_skill_queue_future_dated', v_n);

  SELECT count(*) INTO v_n
    FROM public.fda_asset_aliases
   WHERE active = true;

  IF v_n = 0 THEN
    INSERT INTO public.operator_flags (severity, source, kind, title, body, evidence)
    VALUES (
      'warn',
      'v3_pipeline_watchdog',
      'fda_asset_aliases_empty',
      'Supplemental FDA asset alias table is empty',
      'fda_asset_aliases has zero active rows. Layer-1 asset fields still keep the deterministic prefilter functional, but brand/NCT/code recall is missing until seed_fda_asset_aliases runs.',
      jsonb_build_object(
        'active_aliases', 0,
        'next_step', 'run modal_workers.scripts.seed_fda_asset_aliases initial seed'
      )
    )
    ON CONFLICT DO NOTHING;
  ELSE
    UPDATE public.operator_flags
       SET resolved_at = now(),
           resolved_note = 'auto-resolved by _v3_pipeline_watchdog: active supplemental aliases exist',
           updated_at = now()
     WHERE source = 'v3_pipeline_watchdog'
       AND kind = 'fda_asset_aliases_empty'
       AND resolved_at IS NULL;
  END IF;
  v_results := v_results || jsonb_build_object('fda_asset_aliases_active', v_n);

  SELECT count(*)
    INTO v_n
  FROM public.v_asset_linker_skill_queue q
  WHERE NOT EXISTS (
    SELECT 1
    FROM jsonb_array_elements(q.matched_aliases) AS hit
    WHERE hit->>'kind' <> 'ticker'
  );

  IF v_n > 0
     AND v_n = (SELECT count(*) FROM public.v_asset_linker_skill_queue) THEN
    INSERT INTO public.operator_flags (severity, source, kind, title, body, evidence)
    VALUES (
      'warn',
      'v3_pipeline_watchdog',
      'asset_linker_skill_queue_ticker_only',
      'Local asset-linker queue is ticker-only',
      'Every pending asset-linker edge is ticker-only. This usually means Layer-1 alias lookup or supplemental alias seeding is not active, so the queue is missing drug/name/code/NCT recall.',
      jsonb_build_object('ticker_only_edges', v_n)
    )
    ON CONFLICT DO NOTHING;
  ELSE
    UPDATE public.operator_flags
       SET resolved_at = now(),
           resolved_note = 'auto-resolved by _v3_pipeline_watchdog: queue contains non-ticker alias kinds or is empty',
           updated_at = now()
     WHERE source = 'v3_pipeline_watchdog'
       AND kind = 'asset_linker_skill_queue_ticker_only'
       AND resolved_at IS NULL;
  END IF;
  v_results := v_results || jsonb_build_object('asset_linker_skill_queue_ticker_only', v_n);

  SELECT count(*),
         COALESCE(jsonb_agg(jsonb_build_object(
           'asset_id', id,
           'ticker', ticker,
           'drug_name', drug_name,
           'sponsor_name', sponsor_name
         )) FILTER (WHERE rn <= 10), '[]'::jsonb)
    INTO v_n, v_sample
    FROM (
      SELECT id, ticker, drug_name, sponsor_name,
             row_number() OVER (ORDER BY ticker, created_at DESC) AS rn
        FROM public.fda_assets
       WHERE is_active = true
         AND lower(trim(coalesce(drug_name, ''))) IN (
           '(auto-discovered)', 'ex-99', 'peptide', 'concept', 'nucleotide', 'default'
         )
    ) s;

  IF v_n > 0 THEN
    INSERT INTO public.operator_flags (severity, source, kind, title, body, evidence)
    VALUES (
      'warn',
      'v3_pipeline_watchdog',
      'fda_assets_garbage_program_names',
      'Active fda_assets contain placeholder program names',
      v_n || ' active FDA asset rows use placeholder/garbage drug_name values that can block exact signal-to-asset matching.',
      jsonb_build_object('count', v_n, 'sample', v_sample)
    )
    ON CONFLICT DO NOTHING;
  ELSE
    UPDATE public.operator_flags
       SET resolved_at = now(),
           resolved_note = 'auto-resolved by _v3_pipeline_watchdog: no active placeholder program names found',
           updated_at = now()
     WHERE source = 'v3_pipeline_watchdog'
       AND kind = 'fda_assets_garbage_program_names'
       AND resolved_at IS NULL;
  END IF;
  v_results := v_results || jsonb_build_object('fda_assets_garbage_program_names', v_n);

  SELECT count(*),
         COALESCE(jsonb_agg(jsonb_build_object(
           'asset_id', id,
           'ticker', ticker,
           'drug_name', drug_name,
           'watch_priority', watch_priority,
           'age_days', extract(day from (now() - created_at))::int
         )) FILTER (WHERE rn <= 10), '[]'::jsonb)
    INTO v_n, v_sample
    FROM (
      SELECT a.id, a.ticker, a.drug_name, a.watch_priority, a.created_at,
             row_number() OVER (
               ORDER BY a.watch_priority ASC NULLS LAST, a.created_at ASC
             ) AS rn
        FROM public.fda_assets a
       WHERE a.is_active = true
         AND lower(trim(coalesce(a.drug_name, ''))) NOT IN (
           '(auto-discovered)', 'ex-99', 'peptide', 'concept', 'nucleotide', 'default'
         )
         AND a.created_at < now() - interval '3 days'
         AND NOT EXISTS (
           SELECT 1 FROM public.asset_documents ad WHERE ad.asset_id = a.id
         )
    ) s;

  IF v_n > 5 THEN
    INSERT INTO public.operator_flags (severity, source, kind, title, body, evidence)
    VALUES (
      'warn',
      'v3_pipeline_watchdog',
      'fda_assets_no_docs',
      'Active fda_assets have no linked documents',
      v_n || ' non-placeholder active assets >3 days old have zero asset_documents.',
      jsonb_build_object('count', v_n, 'sample', v_sample, 'threshold', 5)
    )
    ON CONFLICT DO NOTHING;
  ELSE
    UPDATE public.operator_flags
       SET resolved_at = now(),
           resolved_note = 'auto-resolved by _v3_pipeline_watchdog: active non-placeholder assets without docs below threshold',
           updated_at = now()
     WHERE source = 'v3_pipeline_watchdog'
       AND kind = 'fda_assets_no_docs'
       AND resolved_at IS NULL;
  END IF;
  v_results := v_results || jsonb_build_object('fda_assets_no_docs', v_n);

  SELECT count(*),
         COALESCE(jsonb_agg(DISTINCT substring(content FROM 1 FOR 300)), '[]'::jsonb)
    INTO v_n, v_sample
    FROM net._http_response
   WHERE created > now() - interval '30 minutes'
     AND (
       status_code >= 400
       OR timed_out
       OR error_msg IS NOT NULL
     );

  IF v_n > 0 THEN
    INSERT INTO public.operator_flags (severity, source, kind, title, body, evidence)
    VALUES (
      'critical',
      'v3_pipeline_watchdog',
      'compute_v3_400_recent',
      'compute_v3 endpoint rejecting requests',
      v_n || ' failed compute_v3 HTTP response(s) in last 30 minutes.',
      jsonb_build_object('count', v_n, 'sample_responses', v_sample, 'window_minutes', 30)
    )
    ON CONFLICT DO NOTHING;
  ELSE
    UPDATE public.operator_flags
       SET resolved_at = now(),
           resolved_note = 'auto-resolved by _v3_pipeline_watchdog: no compute_v3 HTTP rejects in last 30 minutes',
           updated_at = now()
     WHERE source = 'v3_pipeline_watchdog'
       AND kind = 'compute_v3_400_recent'
       AND resolved_at IS NULL;
  END IF;
  v_results := v_results || jsonb_build_object('compute_v3_400_recent', v_n);

  UPDATE public.operator_flags
     SET resolved_at = now(),
         resolved_note = 'auto-resolved by _v3_pipeline_watchdog: retired by skill asset-linker/fact-extractor cutover',
         updated_at = now()
   WHERE source IN ('v3_pipeline_watchdog', 'orchestrator_cost')
     AND kind IN (
       'asset_linker_pass1_backlog',
       'asset_linker_pass2_backlog',
       'fact_extractor_stalled',
       'asset_linker_burn_no_output',
       'asset_linker_burn_rate_high',
       'asset_linker_24h_hard_halt'
     )
     AND resolved_at IS NULL;

  SELECT COALESCE(sum(cost_usd), 0) INTO v_cost
    FROM public.asset_linker_runs
   WHERE status IN ('completed', 'budget_exceeded')
     AND completed_at > now() - interval '1 hour';
  v_results := v_results || jsonb_build_object(
    'retired_asset_linker_burn_rate_1h_usd',
    round(v_cost, 4)
  );

  RETURN v_results;
END;
$function$;

COMMENT ON FUNCTION public._v3_pipeline_watchdog() IS
  'Cutover-aware v3 pipeline health check. Monitors orchestrator queues, local asset-linker edge queue quality, active fda_assets data quality, and recent compute_v3 rejects; retired Modal asset-linker/fact-extractor checks are auto-resolved.';
