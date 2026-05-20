-- Skill cutover operational guardrails.
--
-- Follow-on to 20260601000000_skill_asset_linker_cutover.sql. Keeps live
-- production behavior aligned with the local-skill cutover by:
--   1. gating the orchestrator drain cron so it only spawns Modal when tier-1
--      work exists;
--   2. replacing legacy asset-linker/fact-extractor watchdog checks with
--      local edge-queue and fda_assets data-quality checks;
--   3. excluding far-future edges from the default local asset-linker queue;
--   4. allowing skill_watchdog to write operator_flags under its canonical
--      source; and
--   5. demoting placeholder fda_assets rows when exactly one clean same-ticker
--      replacement exists.

DO $$
DECLARE
  v_jobid bigint;
BEGIN
  SELECT jobid INTO v_jobid
    FROM cron.job
   WHERE jobname = 'v3-orchestrator-drain';

  IF v_jobid IS NOT NULL THEN
    PERFORM cron.unschedule(v_jobid);
  END IF;

  PERFORM cron.schedule(
    'v3-orchestrator-drain',
    '*/5 * * * *',
    $cron$
      SELECT public._conan_modal_post_enqueue(
        'compute_v3',
        jsonb_build_object(
          'action', 'orchestrator_drain_queue',
          'args',   '{}'::jsonb
        )
      )
      WHERE EXISTS (
        SELECT 1
          FROM public.orchestrator_runs
         WHERE status = 'pending'
           AND tier = 1
         LIMIT 1
      );
    $cron$
  );
END $$;


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
  AND (
    d.published_at IS NULL
    OR d.published_at <= now() + interval '30 days'
  )
  AND NOT EXISTS (
    SELECT 1
      FROM public.document_asset_linker_attempts att
     WHERE att.document_id = e.document_id
       AND att.asset_id = e.asset_id
       AND att.alias_set_hash = e.alias_set_hash
       AND att.status = 'error'
       AND att.created_at > now() - interval '24 hours'
  )
ORDER BY e.match_strength DESC, d.published_at DESC NULLS LAST, e.matched_at;


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


ALTER TABLE public.operator_flags
  DROP CONSTRAINT IF EXISTS operator_flags_source_check;

ALTER TABLE public.operator_flags
  ADD CONSTRAINT operator_flags_source_check CHECK (
    source = ANY (ARRAY[
      'translation_health'::text,
      'scanner_probe'::text,
      'scanner_liveness'::text,
      'convergence_qa'::text,
      'candidate_aging'::text,
      'thesis_writer'::text,
      'reactor'::text,
      'reporting_weekly'::text,
      'litigation_baselines'::text,
      'edgar_runtime_health'::text,
      'scanner_failure_streak'::text,
      'rollback_monitor'::text,
      'orchestrator_cost'::text,
      'thesis_jobs'::text,
      'manual'::text,
      'v3_pipeline_watchdog'::text,
      'aging_review'::text,
      'challenger_retro'::text,
      'constitutional_check'::text,
      'memory_writeback'::text,
      'tier2_quality'::text,
      'orphan_sweeper'::text,
      'backfill_v3_assessment'::text,
      'bridge_signal_to_v3'::text,
      'signal_entity_resolver_hard_halt'::text,
      'signal_entity_resolver_run'::text,
      'asset_linker_hard_halt'::text,
      'skill_watchdog'::text
    ])
  );

UPDATE public.operator_flags
   SET resolved_at = now(),
       resolved_note = 'Resolved: operator_flags.source CHECK now allows skill_watchdog.',
       updated_at = now()
 WHERE resolved_at IS NULL
   AND kind IN (
     'skill_watchdog_source_missing',
     'skill_watchdog_blocked',
     'skill_watchdog:write_surface_blocked'
   );

UPDATE public.operator_flags
   SET resolved_at = now(),
       resolved_note = 'Resolved: skill_watchdog can now write canonical source=skill_watchdog; stale workaround flag cleared pending next canonical run.',
       updated_at = now()
 WHERE resolved_at IS NULL
   AND source = 'v3_pipeline_watchdog'
   AND kind LIKE 'skill_dark:%';


WITH garbage AS (
  SELECT
    g.id AS garbage_id,
    array_agg(clean.id ORDER BY clean.watch_priority ASC NULLS LAST, clean.created_at ASC)
      FILTER (WHERE clean.id IS NOT NULL) AS clean_ids,
    count(clean.id) AS clean_count
  FROM public.fda_assets g
  LEFT JOIN public.fda_assets clean
    ON clean.is_active = true
   AND clean.ticker = g.ticker
   AND clean.id <> g.id
   AND lower(trim(coalesce(clean.drug_name, ''))) NOT IN (
     '(auto-discovered)', 'ex-99', 'peptide', 'concept', 'nucleotide', 'default'
   )
  WHERE g.is_active = true
    AND lower(trim(coalesce(g.drug_name, ''))) IN (
      '(auto-discovered)', 'ex-99', 'peptide', 'concept', 'nucleotide', 'default'
    )
  GROUP BY g.id
),
safe_map AS (
  SELECT garbage_id, clean_ids[1] AS clean_id
    FROM garbage
   WHERE clean_count = 1
),
deleted_duplicates AS (
  DELETE FROM public.asset_documents ad
  USING safe_map sm
  WHERE ad.asset_id = sm.garbage_id
    AND EXISTS (
      SELECT 1
        FROM public.asset_documents existing
       WHERE existing.asset_id = sm.clean_id
         AND existing.document_id = ad.document_id
         AND existing.link_type = ad.link_type
    )
  RETURNING ad.id
),
moved_links AS (
  UPDATE public.asset_documents ad
     SET asset_id = sm.clean_id
    FROM safe_map sm
   WHERE ad.asset_id = sm.garbage_id
  RETURNING ad.id
)
UPDATE public.fda_assets fa
   SET is_active = false,
       aging_state = 'demoted',
       aging_extensions = coalesce(fa.aging_extensions, '{}'::jsonb)
         || jsonb_build_object(
              'demoted_reason', 'placeholder_program_duplicate_clean_same_ticker',
              'demoted_at', now(),
              'replacement_asset_id', sm.clean_id
            ),
       updated_at = now()
  FROM safe_map sm
 WHERE fa.id = sm.garbage_id;


-- Shared guard for all signal-to-asset producers. Keep this aligned with the
-- skill queue and alias blocklists: these strings are parser artifacts or broad
-- biochemical classes, not investable FDA programs.
CREATE OR REPLACE FUNCTION public.fda_asset_program_name_is_placeholder(p_name text)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
AS $$
  SELECT p_name IS NOT NULL
     AND (
       lower(trim(p_name)) IN (
         '(auto-discovered)', 'ex-99', 'peptide', 'concept', 'nucleotide', 'default'
       )
       OR lower(trim(p_name)) ~ '^ex[-_]?[0-9]'
     );
$$;


-- Quarantine residual placeholder assets even when no clean same-ticker
-- replacement exists. They remain in the database for operator enrichment but
-- are removed from active candidate surfaces, and their pending regulatory
-- events are superseded so immediate-scored parser artifacts do not page the
-- pipeline.
WITH placeholder_assets AS (
  SELECT id
  FROM public.fda_assets
  WHERE public.fda_asset_program_name_is_placeholder(drug_name)
),
superseded_events AS (
  UPDATE public.fda_regulatory_events e
     SET event_status = 'superseded',
         extensions = coalesce(e.extensions, '{}'::jsonb)
           || jsonb_build_object(
                'superseded_reason', 'placeholder_program_name_quarantined',
                'superseded_at', now()
              )
    FROM placeholder_assets pa
   WHERE e.asset_id = pa.id
     AND e.event_status = 'pending'
  RETURNING e.id
),
demoted_assets AS (
  UPDATE public.fda_assets fa
     SET is_active = false,
         aging_state = 'demoted',
         aging_extensions = coalesce(fa.aging_extensions, '{}'::jsonb)
           || jsonb_build_object(
                'demoted_reason', 'placeholder_program_name_needs_enrichment',
                'demoted_at', now(),
                'preserved_for_operator_enrichment', true
              ),
         updated_at = now()
    FROM placeholder_assets pa
   WHERE fa.id = pa.id
     AND fa.is_active = true
  RETURNING fa.id
)
UPDATE public.operator_flags
   SET resolved_at = now(),
       resolved_note = 'Resolved by placeholder-program quarantine: active placeholder fda_assets demoted and pending placeholder events superseded.',
       updated_at = now()
 WHERE resolved_at IS NULL
   AND (
     (source = 'v3_pipeline_watchdog' AND kind = 'fda_assets_garbage_program_names')
     OR (
       source = 'bridge_signal_to_v3'
       AND kind = 'v3_bridge_no_asset_match'
       AND (evidence->>'drug_name_was_garbage')::boolean IS TRUE
     )
   );

UPDATE public.operator_flags
   SET resolved_at = now(),
       resolved_note = 'Resolved by skill asset-linker cutover: automated fact_extractor_opus production path is intentionally dark; local/skill workflows own this work surface.',
       updated_at = now()
 WHERE resolved_at IS NULL
   AND source = 'manual'
   AND kind = 'skill_watchdog:skill_dark:fact_extractor_opus';


-- Patch fda_regulatory_event_from_signal() so placeholder program names do not
-- seed fda_assets/fda_regulatory_events. The function body is large and has
-- drifted across migrations, so patch the stable guard point and fail loudly if
-- the expected shape is gone.
DO $$
DECLARE
  v_sql text;
  v_next text;
BEGIN
  SELECT pg_get_functiondef('public.fda_regulatory_event_from_signal()'::regprocedure)
    INTO v_sql;

  IF v_sql NOT LIKE '%fda_asset_program_name_is_placeholder%' THEN
    v_next := replace(
      v_sql,
      '  IF v_ticker IS NULL OR v_drug_name IS NULL THEN RETURN NEW; END IF;',
      '  IF public.fda_asset_program_name_is_placeholder(v_drug_name) THEN RETURN NEW; END IF;' || E'\n' ||
      '  IF v_ticker IS NULL OR v_drug_name IS NULL THEN RETURN NEW; END IF;'
    );

    IF v_next = v_sql THEN
      RAISE EXCEPTION 'Could not patch fda_regulatory_event_from_signal placeholder guard';
    END IF;

    EXECUTE v_next;
  END IF;
END $$;


-- Federal Register AdCom notices are sparse. Treat a fresh successful scanner
-- run as healthy even when no new catalyst_universe row was inserted, otherwise
-- the liveness watchdog repeatedly flags normal "no new notice" periods.
DO $$
DECLARE
  v_sql text;
  v_next text;
BEGIN
  SELECT pg_get_functiondef('public._scanner_liveness_watchdog()'::regprocedure)
    INTO v_sql;

  IF v_sql NOT LIKE '%scanner_fresh_no_new_rows%' THEN
    v_next := replace(
      v_sql,
      '    IF v_max_fetched IS NULL OR v_feed_age_hours > 36 THEN',
      '    IF v_feed_row.source_feed = ''federal_register_adcom'' AND EXISTS (' || E'\n' ||
      '      SELECT 1 FROM public.scanners' || E'\n' ||
      '       WHERE id = v_scanner_id' || E'\n' ||
      '         AND last_run_utc > now() - interval ''36 hours''' || E'\n' ||
      '    ) THEN' || E'\n' ||
      '      UPDATE public.operator_flags' || E'\n' ||
      '         SET resolved_at = now(),' || E'\n' ||
      '             resolved_note = ''auto-resolved by _scanner_liveness_watchdog: sparse feed scanner ran recently; no new source rows is not an outage'',' || E'\n' ||
      '             updated_at = now()' || E'\n' ||
      '       WHERE source=''scanner_liveness''' || E'\n' ||
      '         AND kind=''fetcher_overdue''' || E'\n' ||
      '         AND resolved_at IS NULL' || E'\n' ||
      '         AND scanner_id = v_scanner_id;' || E'\n' ||
      '      GET DIAGNOSTICS v_resolved_n = ROW_COUNT;' || E'\n' ||
      '      v_results := v_results || jsonb_build_object(' || E'\n' ||
      '        v_feed_row.source_feed,' || E'\n' ||
      '        jsonb_build_object(''skipped'', ''scanner_fresh_no_new_rows'',' || E'\n' ||
      '                           ''resolved'', v_resolved_n,' || E'\n' ||
      '                           ''age_hours'', v_feed_age_hours));' || E'\n' ||
      '      CONTINUE;' || E'\n' ||
      '    END IF;' || E'\n\n' ||
      '    IF v_max_fetched IS NULL OR v_feed_age_hours > 36 THEN'
    );

    IF v_next = v_sql THEN
      RAISE EXCEPTION 'Could not patch _scanner_liveness_watchdog sparse-feed guard';
    END IF;

    EXECUTE v_next;
  END IF;
END $$;


-- Patch bridge_signal_to_v3_row() for the broader placeholder blocklist and make
-- operator_flags robust if historical signals carry a stale entity_id.
DO $$
DECLARE
  v_sql text;
  v_next text;
BEGIN
  SELECT pg_get_functiondef('public.bridge_signal_to_v3_row(public.signals)'::regprocedure)
    INTO v_sql;
  v_next := v_sql;

  IF v_next NOT LIKE '%v_flag_entity_id%' THEN
    v_next := replace(
      v_next,
      '  v_high_confidence boolean;',
      '  v_high_confidence boolean;' || E'\n' ||
      '  v_flag_entity_id uuid;'
    );
  END IF;

  IF v_next NOT LIKE '%fda_asset_program_name_is_placeholder%' THEN
    v_next := replace(
      v_next,
      '  v_drug_name_is_garbage := (' || E'\n' ||
      '    v_drug_name IS NOT NULL' || E'\n' ||
      '    AND v_drug_name ~* ''^ex[-_]?\d''' || E'\n' ||
      '  );',
      '  v_drug_name_is_garbage := public.fda_asset_program_name_is_placeholder(v_drug_name);'
    );

    -- Some deployed versions have the same assignment compressed onto one line.
    v_next := replace(
      v_next,
      '  v_drug_name_is_garbage := (v_drug_name IS NOT NULL AND v_drug_name ~* ''^ex[-_]?\d'');',
      '  v_drug_name_is_garbage := public.fda_asset_program_name_is_placeholder(v_drug_name);'
    );
  END IF;

  IF v_next NOT LIKE '%raw_entity_id%' THEN
    v_next := replace(
      v_next,
      '  IF v_asset_id IS NULL THEN' || E'\n' ||
      '    INSERT INTO public.operator_flags',
      '  IF v_asset_id IS NULL THEN' || E'\n' ||
      '    SELECT p_sig.entity_id INTO v_flag_entity_id' || E'\n' ||
      '    WHERE p_sig.entity_id IS NOT NULL' || E'\n' ||
      '      AND EXISTS (SELECT 1 FROM public.entities e WHERE e.id = p_sig.entity_id);' || E'\n\n' ||
      '    INSERT INTO public.operator_flags'
    );

    v_next := replace(
      v_next,
      '      p_sig.signal_id,' || E'\n' ||
      '      p_sig.entity_id,' || E'\n' ||
      '      format(',
      '      p_sig.signal_id,' || E'\n' ||
      '      v_flag_entity_id,' || E'\n' ||
      '      format('
    );

    v_next := replace(
      v_next,
      '        ''issuer_figi'', p_sig.issuer_figi' || E'\n' ||
      '      )',
      '        ''issuer_figi'', p_sig.issuer_figi,' || E'\n' ||
      '        ''raw_entity_id'', p_sig.entity_id' || E'\n' ||
      '      )'
    );
  END IF;

  IF v_next = v_sql THEN
    RAISE NOTICE 'bridge_signal_to_v3_row already has placeholder/entity guard patches';
  ELSE
    IF v_next NOT LIKE '%fda_asset_program_name_is_placeholder%'
       OR v_next NOT LIKE '%v_flag_entity_id%'
       OR v_next NOT LIKE '%raw_entity_id%' THEN
      RAISE EXCEPTION 'Could not patch bridge_signal_to_v3_row guard shape';
    END IF;
    EXECUTE v_next;
  END IF;
END $$;
