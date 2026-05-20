-- Skill-based asset linker cutover.
--
-- Asset linking is no longer allowed to run from Modal with ANTHROPIC_API_KEY.
-- Production only exposes a queue for the local Cursor asset-linker skill,
-- and every skill classification writes an attempt row so no-match documents
-- do not get processed repeatedly.

DO $$
DECLARE
  v_jobid bigint;
BEGIN
  FOR v_jobid IN
    SELECT jobid
      FROM cron.job
     WHERE jobname IN (
       'v3-asset-linker-pass1',
       'v3-asset-linker-pass2',
       'v3-fact-extractor'
     )
  LOOP
    PERFORM cron.unschedule(v_jobid);
  END LOOP;
END $$;


-- The ingestion scheduler watchdog used to recreate/re-enable asset linker
-- and fact-extractor cron jobs. After the skill cutover it only protects
-- deterministic, zero-LLM-cost ingestion support jobs.
CREATE OR REPLACE FUNCTION public.v3_ingestion_scheduler_watchdog()
RETURNS jsonb
LANGUAGE plpgsql
SET search_path = public, extensions
AS $$
DECLARE
  v_expected text[] := ARRAY[
    'v3-doc-asset-prefilter',
    'v3-asset-alias-weekly-refresh'
  ];
  v_disabled text[];
  v_missing text[];
  v_existing_flag uuid;
  v_jobid bigint;
BEGIN
  SELECT COALESCE(array_agg(jobname ORDER BY jobname), ARRAY[]::text[])
    INTO v_disabled
  FROM cron.job
  WHERE jobname = ANY (v_expected)
    AND COALESCE(active, false) = false;

  SELECT COALESCE(array_agg(expected.jobname ORDER BY expected.jobname), ARRAY[]::text[])
    INTO v_missing
  FROM unnest(v_expected) AS expected(jobname)
  LEFT JOIN cron.job AS j ON j.jobname = expected.jobname
  WHERE j.jobid IS NULL;

  FOR v_jobid IN
    SELECT jobid
    FROM cron.job
    WHERE jobname = ANY (v_expected)
      AND COALESCE(active, false) = false
  LOOP
    PERFORM cron.alter_job(v_jobid, active := true);
  END LOOP;

  IF 'v3-doc-asset-prefilter' = ANY (v_missing) THEN
    PERFORM cron.schedule(
      'v3-doc-asset-prefilter',
      '*/2 * * * *',
      $cron$ SELECT public.fn_generate_doc_asset_candidates(2000); $cron$
    );
  END IF;

  IF 'v3-asset-alias-weekly-refresh' = ANY (v_missing) THEN
    PERFORM cron.schedule(
      'v3-asset-alias-weekly-refresh',
      '0 3 * * 1',
      $cron$
        SELECT public._conan_modal_post_enqueue(
          'compute_v3',
          jsonb_build_object(
            'action', 'seed_fda_asset_aliases_refresh',
            'args',   '{}'::jsonb
          )
        );
      $cron$
    );
  END IF;

  SELECT id INTO v_existing_flag
  FROM public.operator_flags
  WHERE source = 'v3_pipeline_watchdog'
    AND kind = 'v3_ingestion_cron_repaired'
    AND resolved_at IS NULL
  ORDER BY created_at DESC
  LIMIT 1;

  IF array_length(v_disabled, 1) IS NOT NULL OR array_length(v_missing, 1) IS NOT NULL THEN
    IF v_existing_flag IS NULL THEN
      INSERT INTO public.operator_flags (
        severity, source, kind, title, body, evidence
      ) VALUES (
        'info',
        'v3_pipeline_watchdog',
        'v3_ingestion_cron_repaired',
        'v3 ingestion cron repaired',
        'Scheduler watchdog repaired one or more v3 ingestion crons (doc/asset prefilter, alias weekly refresh). LLM-based asset linking and fact extraction are intentionally disabled; use local skills against the pre-matched edge queue.',
        jsonb_build_object(
          'disabled_jobs', v_disabled,
          'missing_jobs', v_missing,
          'protected_jobs', v_expected,
          'asset_linker_mode', 'cursor_skill_edge_queue'
        )
      );
    END IF;
  ELSIF v_existing_flag IS NOT NULL THEN
    UPDATE public.operator_flags
       SET resolved_at = now(),
           resolved_note = 'v3 ingestion crons (doc/asset prefilter, alias weekly refresh) are present and active; LLM asset-linker and fact-extractor crons intentionally disabled for skill workflows.'
     WHERE id = v_existing_flag;
  END IF;

  RETURN jsonb_build_object(
    'disabled_jobs', v_disabled,
    'missing_jobs', v_missing,
    'protected_jobs', v_expected,
    'asset_linker_mode', 'cursor_skill_edge_queue'
  );
END;
$$;

REVOKE ALL ON FUNCTION public.v3_ingestion_scheduler_watchdog() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.v3_ingestion_scheduler_watchdog() FROM anon;
REVOKE ALL ON FUNCTION public.v3_ingestion_scheduler_watchdog() FROM authenticated;


CREATE TABLE IF NOT EXISTS public.document_asset_linker_attempts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  document_id uuid NOT NULL REFERENCES public.documents(id) ON DELETE CASCADE,
  asset_id uuid NOT NULL REFERENCES public.fda_assets(id) ON DELETE CASCADE,
  alias_set_hash text NOT NULL,
  status text NOT NULL CHECK (
    status IN ('linked', 'no_match', 'error', 'skipped_prefilter')
  ),
  link_inserted boolean NOT NULL DEFAULT false,
  reasoning_summary text,
  classified_by text NOT NULL DEFAULT 'cursor-agent-skill',
  error_message text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS document_asset_linker_attempts_terminal_once
  ON public.document_asset_linker_attempts (document_id, asset_id, alias_set_hash)
  WHERE status IN ('linked', 'no_match', 'skipped_prefilter');

CREATE INDEX IF NOT EXISTS document_asset_linker_attempts_document_created_idx
  ON public.document_asset_linker_attempts (document_id, created_at DESC);

CREATE INDEX IF NOT EXISTS document_asset_linker_attempts_asset_created_idx
  ON public.document_asset_linker_attempts (asset_id, created_at DESC);

CREATE INDEX IF NOT EXISTS document_asset_linker_attempts_status_created_idx
  ON public.document_asset_linker_attempts (status, created_at DESC);

COMMENT ON TABLE public.document_asset_linker_attempts IS
  'One row per local asset-linker skill classification attempt against one '
  '(document, asset) edge. Terminal rows (linked/no_match/skipped_prefilter) '
  'prevent repeat processing for the same (document, asset, alias_set_hash) '
  'tuple. Paired 1:1 with the doc_asset_candidates row the skill consumed '
  '(linked via doc_asset_candidates.analysis_run_id pointing here through the '
  'asset_linker_runs join).';


CREATE OR REPLACE VIEW public.v_asset_linker_skill_assets AS
SELECT
  id,
  ticker,
  drug_name,
  generic_name,
  sponsor_name,
  indication,
  indication_normalized,
  watch_priority
FROM public.fda_assets
WHERE is_active = true
  AND NULLIF(trim(coalesce(drug_name, '')), '') IS NOT NULL
  AND lower(trim(coalesce(drug_name, ''))) NOT IN (
    '(auto-discovered)', 'ex-99', 'peptide', 'concept', 'nucleotide', 'default'
  );

-- ============================================================
-- Deterministic edge prefilter — tables
-- ============================================================
--
-- See tasks/skill_asset_linker_edge_prefilter_plan.md for the full design.
-- Summary: replace the LLM-driven document prefilter with a SQL keyword/alias
-- matcher that emits persisted (document, asset) candidate edges. The local
-- Cursor asset-linker skill then consumes those edges (not raw documents),
-- spending LLM tokens only on pre-matched pairs.


-- Materialized full-text vector on documents (simple config — no English
-- stemming, no stop-word stripping; multi-word sponsor names need every token
-- preserved). One-time rewrite on existing rows is acceptable at ~3k row scale.
ALTER TABLE public.documents
  ADD COLUMN IF NOT EXISTS raw_text_tsv tsvector
  GENERATED ALWAYS AS (
    to_tsvector('simple',
      coalesce(raw_text, '') || ' ' || coalesce(title, '')
    )
  ) STORED;

CREATE INDEX IF NOT EXISTS documents_raw_text_tsv_gin_idx
  ON public.documents USING GIN (raw_text_tsv);


-- fda_asset_aliases — Layer 2 alias supplement. Tickers intentionally NOT
-- ingested here; they live on fda_assets.ticker and require case-sensitive
-- matching distinct from the tsvector-based name-matching path.
CREATE TABLE IF NOT EXISTS public.fda_asset_aliases (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  asset_id         uuid NOT NULL REFERENCES public.fda_assets(id) ON DELETE CASCADE,
  alias            text NOT NULL CHECK (length(trim(alias)) >= 3),
  alias_normalized text NOT NULL CHECK (
    length(alias_normalized) >= 3
    AND alias_normalized = lower(trim(alias_normalized))
    AND alias_normalized NOT IN (
      'peptide', 'concept', 'default', 'ex-99', '(auto-discovered)',
      'nucleotide', 'drug', 'tablet', 'capsule', 'injection'
    )
  ),
  alias_kind       text NOT NULL CHECK (alias_kind IN (
    'brand', 'generic', 'code', 'nct_id', 'abbreviation',
    'sponsor_alias', 'sponsor_stem', 'drug_name'
  )),
  -- Per-kind shape checks: NCT IDs must match the registry format; codes are
  -- alphanumeric with optional internal hyphen/space.
  CONSTRAINT fda_asset_aliases_nct_shape CHECK (
    alias_kind <> 'nct_id'
    OR alias_normalized ~ '^nct[0-9]{8}$'
  ),
  source           text NOT NULL CHECK (source IN (
    'curated_map', 'openfda_label', 'clinicaltrials_v2',
    'extensions_mining', 'operator', 'synthetic'
  )),
  source_ref       text,
  active           boolean NOT NULL DEFAULT true,
  inactive_reason  text,
  alias_tsquery    tsquery GENERATED ALWAYS AS (
    phraseto_tsquery('simple', alias_normalized)
  ) STORED,
  created_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (asset_id, alias_normalized, alias_kind)
);

CREATE INDEX IF NOT EXISTS fda_asset_aliases_lookup_idx
  ON public.fda_asset_aliases (alias_normalized) WHERE active = true;

CREATE INDEX IF NOT EXISTS fda_asset_aliases_asset_idx
  ON public.fda_asset_aliases (asset_id);

CREATE INDEX IF NOT EXISTS fda_asset_aliases_kind_idx
  ON public.fda_asset_aliases (alias_kind) WHERE active = true;

COMMENT ON TABLE public.fda_asset_aliases IS
  'Asset alias supplement (Layer 2) feeding the deterministic doc/asset '
  'prefilter. Populated by modal_workers/scripts/seed_fda_asset_aliases.py '
  'from CURATED_MAP, openFDA labels, ClinicalTrials.gov, and existing '
  'documents.extensions mining. Tickers intentionally excluded — they require '
  'case-sensitive matching and are handled directly from fda_assets.ticker.';


-- doc_asset_candidates — persisted (doc, asset) candidate edges emitted by
-- the deterministic prefilter sweeper. One row per (document, asset,
-- alias_set_hash) tuple. The Cursor asset-linker skill consumes unanalyzed
-- rows in match_strength priority order.
CREATE TABLE IF NOT EXISTS public.doc_asset_candidates (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  document_id     uuid NOT NULL REFERENCES public.documents(id) ON DELETE CASCADE,
  asset_id        uuid NOT NULL REFERENCES public.fda_assets(id) ON DELETE CASCADE,
  matched_aliases jsonb NOT NULL,
  match_strength  smallint NOT NULL CHECK (match_strength >= 1),
  alias_set_hash  text NOT NULL,
  matched_at      timestamptz NOT NULL DEFAULT now(),
  analyzed_at     timestamptz,
  analysis_run_id uuid,
  UNIQUE (document_id, asset_id, alias_set_hash)
);

CREATE INDEX IF NOT EXISTS doc_asset_candidates_unprocessed_idx
  ON public.doc_asset_candidates (match_strength DESC, matched_at)
  WHERE analyzed_at IS NULL;

CREATE INDEX IF NOT EXISTS doc_asset_candidates_document_idx
  ON public.doc_asset_candidates (document_id);

CREATE INDEX IF NOT EXISTS doc_asset_candidates_asset_idx
  ON public.doc_asset_candidates (asset_id);

CREATE INDEX IF NOT EXISTS doc_asset_candidates_hash_idx
  ON public.doc_asset_candidates (alias_set_hash);

COMMENT ON TABLE public.doc_asset_candidates IS
  '(document, asset) candidate edges from the deterministic prefilter. '
  'analyzed_at NULL = pending skill review. alias_set_hash snapshot lets the '
  'sweeper invalidate stale candidates when the alias inventory changes.';


-- doc_asset_prefilter_runs — sentinel table marking which (document,
-- alias_set_hash) pairs the sweeper has already scanned. Required so docs
-- that produced zero candidates do not get rescanned under the same hash.
CREATE TABLE IF NOT EXISTS public.doc_asset_prefilter_runs (
  document_id     uuid NOT NULL REFERENCES public.documents(id) ON DELETE CASCADE,
  alias_set_hash  text NOT NULL,
  candidate_count integer NOT NULL DEFAULT 0 CHECK (candidate_count >= 0),
  scanned_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (document_id, alias_set_hash)
);

CREATE INDEX IF NOT EXISTS doc_asset_prefilter_runs_hash_idx
  ON public.doc_asset_prefilter_runs (alias_set_hash, scanned_at DESC);

COMMENT ON TABLE public.doc_asset_prefilter_runs IS
  'Sentinel marking documents already scanned by fn_generate_doc_asset_candidates '
  'under a given alias_set_hash. Sweeper anti-joins against this table so '
  'zero-candidate docs are not rescanned needlessly.';


-- v_asset_alias_lookup — one read surface for Layer 1 asset fields plus Layer 2
-- supplemental aliases. Tickers stay out because they require case-sensitive
-- regex matching in the dedicated ticker path below.
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
-- Deterministic edge prefilter — hash function
-- ============================================================

-- alias_set_hash includes the full active alias inventory plus Layer 1 asset
-- fields. Any alias add/remove/deactivate OR asset field update invalidates the
-- hash, which in turn invalidates
-- doc_asset_prefilter_runs entries and triggers a sweeper rescan.
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

-- v_asset_linker_skill_queue — edge-shaped queue: one row per pre-matched
-- (document, asset) candidate edge that the local Cursor asset-linker skill
-- has not yet analyzed under the current alias_set_hash. Replaces the
-- doc-shaped LLM-prefilter queue.
CREATE OR REPLACE VIEW public.v_asset_linker_skill_queue AS
WITH current_hash AS (
  SELECT public.asset_linker_alias_set_hash() AS alias_set_hash
)
SELECT
  e.id              AS candidate_id,
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
  -- Drug/generic/brand/code/NCT/ticker edges are higher-signal than sponsor-only
  -- mentions. Keep sponsor-only in the queue, but drain it later.
  CASE WHEN EXISTS (
    SELECT 1
    FROM jsonb_array_elements(e.matched_aliases) AS hit
    WHERE hit->>'kind' IN (
      'drug_name', 'generic', 'brand', 'nct_id', 'code', 'ticker', 'abbreviation'
    )
  ) THEN 0 ELSE 1 END,
  -- Future-dated clinical-trial rows are useful backlog, not the first rows a
  -- skill run should spend time on when current FDA docs exist.
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


ALTER TABLE public.asset_linker_runs
  DROP CONSTRAINT IF EXISTS asset_linker_runs_pass_check;

ALTER TABLE public.asset_linker_runs
  ADD CONSTRAINT asset_linker_runs_pass_check
  CHECK (pass IN ('pass1','pass2','cowork_backfill','skill','seed'));


-- ============================================================
-- Deterministic edge prefilter — sweeper function
-- ============================================================

-- fn_generate_doc_asset_candidates(p_limit) — scans up to p_limit documents
-- that have not been processed under the current alias_set_hash, runs the
-- three match paths (tsvector, exact word-boundary for nct_id/code,
-- case-sensitive word-boundary for tickers), and emits one row per matching
-- (document, asset) pair into doc_asset_candidates. Always marks scanned docs
-- in doc_asset_prefilter_runs (including zero-match), preventing rescan.
--
-- Designed for ~3000-doc backlog at <60s wall time. Set-based; no per-row
-- PL/pgSQL loop. The Cursor asset-linker skill consumes the resulting edges
-- via v_asset_linker_skill_queue.
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

COMMENT ON FUNCTION public.fn_generate_doc_asset_candidates(int) IS
  'Deterministic prefilter sweeper. Scans up to p_limit docs not yet scanned '
  'under the current alias_set_hash, emits (doc, asset) candidate edges via '
  'three match paths (tsvector for names, exact word-boundary for NCT IDs / '
  'codes, case-sensitive word-boundary for tickers). Returns docs_scanned, '
  'edges_emitted, alias_set_hash. Cron-driven via v3-doc-asset-prefilter.';


-- ============================================================
-- Deterministic edge prefilter — operator review view
-- ============================================================

CREATE OR REPLACE VIEW public.v_recent_auto_aliases AS
SELECT
  a.id,
  a.asset_id,
  a.alias,
  a.alias_normalized,
  a.alias_kind,
  a.source,
  a.source_ref,
  a.created_at,
  fa.ticker,
  fa.drug_name,
  fa.sponsor_name,
  fa.indication
FROM public.fda_asset_aliases a
JOIN public.fda_assets fa ON fa.id = a.asset_id
WHERE a.source IN ('openfda_label', 'clinicaltrials_v2', 'extensions_mining')
  AND a.created_at > now() - interval '14 days'
  AND a.active = true
ORDER BY a.created_at DESC, a.asset_id;

COMMENT ON VIEW public.v_recent_auto_aliases IS
  'Aliases auto-populated by seed_fda_asset_aliases.py in the last 14 days. '
  'Operator review surface for false-positive triage; deactivate via '
  'UPDATE fda_asset_aliases SET active = false, inactive_reason = ...';


-- ============================================================
-- Deterministic edge prefilter — cron schedules
-- ============================================================
--
-- These are also recreated by v3_ingestion_scheduler_watchdog() if deleted,
-- but scheduling them here gets the prefilter running immediately on apply
-- instead of waiting for the next watchdog invocation.

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM cron.job WHERE jobname = 'v3-doc-asset-prefilter'
  ) THEN
    PERFORM cron.schedule(
      'v3-doc-asset-prefilter',
      '*/2 * * * *',
      $cron$ SELECT public.fn_generate_doc_asset_candidates(2000); $cron$
    );
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM cron.job WHERE jobname = 'v3-asset-alias-weekly-refresh'
  ) THEN
    PERFORM cron.schedule(
      'v3-asset-alias-weekly-refresh',
      '0 3 * * 1',
      $cron$
        SELECT public._conan_modal_post_enqueue(
          'compute_v3',
          jsonb_build_object(
            'action', 'seed_fda_asset_aliases_refresh',
            'args',   '{}'::jsonb
          )
        );
      $cron$
    );
  END IF;
END $$;
