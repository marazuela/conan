-- asset_linker: drop sponsor_alias from prefilter, GC stranded candidates,
-- enforce alias usability at write-time.
--
-- Three coordinated changes:
--   1. v_asset_alias_lookup loses the sponsor_name UNION branch (Pfizer/AZ/BMS
--      sponsor-only edges were dominating the queue at low precision).
--   2. fn_generate_doc_asset_candidates() prepends a GC of doc_asset_candidates
--      and doc_asset_prefilter_runs rows under dead alias_set_hash values.
--   3. fda_asset_aliases CHECK constraints tighten: alias_kind whitelist no
--      longer accepts 'sponsor_alias' / 'sponsor_stem'; new CHECK enforces
--      asset_linker_alias_is_usable() on active rows.
--
-- Prod state at write time: fda_asset_aliases has zero rows of kind
-- sponsor_alias / sponsor_stem (sponsor matches all came from Layer 1, not
-- Layer 2), so no backfill / inactivation step is required for sponsor rows.

BEGIN;

CREATE OR REPLACE FUNCTION public.asset_linker_alias_is_usable(
  p_alias_kind text,
  p_alias_normalized text
)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
AS $$
  SELECT
    NULLIF(trim(coalesce(p_alias_normalized, '')), '') IS NOT NULL
    AND lower(trim(p_alias_normalized)) NOT IN (
      'peptide', 'concept', 'default', 'ex-99', '(auto-discovered)',
      'nucleotide', 'drug', 'tablet', 'capsule', 'injection'
    )
    AND (
      p_alias_kind <> 'code'
      OR (
        lower(p_alias_normalized) !~ '(placebo|vehicle)'
        AND lower(p_alias_normalized) ~ '[0-9.-]'
      )
    );
$$;

-- 1. View: drop the sponsor_name UNION branch.
CREATE OR REPLACE VIEW public.v_asset_alias_lookup AS
SELECT
  fa.id AS asset_id,
  fa.drug_name AS alias,
  lower(trim(fa.drug_name)) AS alias_normalized,
  'drug_name'::text AS alias_kind,
  phraseto_tsquery('simple', lower(trim(fa.drug_name))) AS alias_tsquery
FROM public.v_asset_linker_skill_assets fa
WHERE public.asset_linker_alias_is_usable('drug_name', lower(trim(fa.drug_name)))
UNION
SELECT
  fa.id AS asset_id,
  fa.generic_name AS alias,
  lower(trim(fa.generic_name)) AS alias_normalized,
  'generic'::text AS alias_kind,
  phraseto_tsquery('simple', lower(trim(fa.generic_name))) AS alias_tsquery
FROM public.v_asset_linker_skill_assets fa
WHERE public.asset_linker_alias_is_usable('generic', lower(trim(fa.generic_name)))
UNION
SELECT
  a.asset_id,
  a.alias,
  a.alias_normalized,
  a.alias_kind,
  a.alias_tsquery
FROM public.fda_asset_aliases a
WHERE a.active = true
  AND public.asset_linker_alias_is_usable(a.alias_kind, a.alias_normalized);

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
      )
       FROM public.fda_asset_aliases a
       WHERE a.active = true
         AND public.asset_linker_alias_is_usable(a.alias_kind, a.alias_normalized)),
      ''
    ) || '#layer1-nosponsor#' ||
    COALESCE(
      (SELECT string_agg(
        fa.id::text || '|' ||
        coalesce(fa.ticker, '') || '|' ||
        coalesce(fa.drug_name, '') || '|' ||
        coalesce(fa.generic_name, ''),
        ',' ORDER BY fa.id
      )
       FROM public.v_asset_linker_skill_assets fa),
      ''
    )
  );
$$;

-- 2. Tighten alias_kind whitelist. Precheck above confirmed no active rows
-- use sponsor_alias or sponsor_stem, so we can drop them from the CHECK
-- without first inactivating anything.
ALTER TABLE public.fda_asset_aliases
  DROP CONSTRAINT fda_asset_aliases_alias_kind_check;

ALTER TABLE public.fda_asset_aliases
  ADD CONSTRAINT fda_asset_aliases_alias_kind_check
  CHECK (alias_kind = ANY (ARRAY[
    'brand'::text,
    'generic'::text,
    'code'::text,
    'nct_id'::text,
    'abbreviation'::text,
    'drug_name'::text
  ]));

-- 3. Write-time usability gate. Predicate gated on `active` so legacy
-- inactivated junk rows don't fail the constraint. Added NOT VALID first,
-- then backfilled any usability violators to active=false, then validated.
ALTER TABLE public.fda_asset_aliases
  ADD CONSTRAINT fda_asset_aliases_usable
  CHECK (
    NOT active
    OR public.asset_linker_alias_is_usable(alias_kind, alias_normalized)
  ) NOT VALID;

UPDATE public.fda_asset_aliases
SET active = false,
    inactive_reason = 'low_info_alias_2026-05-20'
WHERE active = true
  AND NOT public.asset_linker_alias_is_usable(alias_kind, alias_normalized);

ALTER TABLE public.fda_asset_aliases
  VALIDATE CONSTRAINT fda_asset_aliases_usable;

-- 4. Function: prepend GC of stranded candidates / prefilter_runs.
-- Only unanalyzed candidates are deleted — analyzed rows are historical
-- facts (we already classified that doc/asset pair) and must be preserved.
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
  v_orphan_candidates int := 0;
  v_orphan_runs int := 0;
BEGIN
  v_hash := public.asset_linker_alias_set_hash();

  DELETE FROM public.doc_asset_candidates
  WHERE alias_set_hash <> v_hash
    AND analyzed_at IS NULL;
  GET DIAGNOSTICS v_orphan_candidates = ROW_COUNT;

  DELETE FROM public.doc_asset_prefilter_runs
  WHERE alias_set_hash <> v_hash;
  GET DIAGNOSTICS v_orphan_runs = ROW_COUNT;

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
     AND public.asset_linker_alias_is_usable(a.alias_kind, a.alias_normalized)
     AND (coalesce(td.raw_text, '') || ' ' || coalesce(td.title, ''))
         ~* ('\m' || a.alias_normalized || '\M')
  ),
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
    'docs_scanned',          v_docs_scanned,
    'edges_emitted',         v_edges_emitted,
    'alias_set_hash',        v_hash,
    'orphan_candidates_gc',  v_orphan_candidates,
    'orphan_runs_gc',        v_orphan_runs
  );
END;
$$;

COMMIT;
