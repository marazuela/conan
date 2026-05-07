-- 20260510000000_v3_rag_infrastructure.sql
-- Stream 5: RAG Phase 4.5 infrastructure (D-123).
-- Eight new tables for the contextual-retrieval pipeline:
--   1. document_chunks                  — provider-agnostic chunk metadata
--   2. chunk_embeddings_literature      — pubmed/biorxiv/medrxiv (1024-dim Matryoshka)
--   3. chunk_embeddings_filings         — edgar/federal_register/fda_advisory (2000-dim Matryoshka)
--   4. chunk_embeddings_labels_aes      — dailymed/faers/openfda/warning_letter/483 (2000-dim)
--   5. chunk_embeddings_news            — polygon_news/press_release/clinicaltrials (2000-dim)
--
-- 2000-dim chosen over native 2048 because pgvector HNSW caps at 2000 dims;
-- Voyage-3 + OpenAI text-embedding-3-large both Matryoshka-truncate cleanly
-- to 2000 (Voyage retains ~99% of full ranking on retrieval benchmarks).
--   6. citation_graph_cache             — doc-to-doc citation edges
--   7. retrieval_cache                  — query-result cache, TTL-keyed
--   8. rag_eval_gold + rag_eval_log     — RAGAS gold set + run log
--
-- Provider stored as a column (NOT a separate table) so Voyage and OpenAI
-- can coexist in the same corpus for A/B without dropping data; HNSW partial
-- indexes per provider keep query plans clean. Re-embedding on a provider
-- switch walks chunks missing the new provider's row.
--
-- pgvector extension already installed (initial_schema.sql).

-- ============================================================================
-- 1. document_chunks
-- ============================================================================
-- Hierarchical chunks: every leaf has parent_chunk_id pointing to a section-
-- level summary chunk. tsv is generated from contextual_prefix + chunk_text
-- so BM25 retrieval benefits from the Haiku-augmented context.

CREATE TABLE IF NOT EXISTS public.document_chunks (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  document_id uuid NOT NULL REFERENCES public.documents(id) ON DELETE CASCADE,
  chunk_index int NOT NULL,
  parent_chunk_id uuid REFERENCES public.document_chunks(id) ON DELETE SET NULL,
  section_path text[] NOT NULL DEFAULT '{}'::text[],
  chunk_text text NOT NULL,
  chunk_tokens int NOT NULL,
  contextual_prefix text,
  tsv tsvector GENERATED ALWAYS AS (
    to_tsvector('english',
      coalesce(contextual_prefix, '') || ' ' || chunk_text)
  ) STORED,
  extensions jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (document_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS document_chunks_doc_idx
  ON public.document_chunks(document_id);
CREATE INDEX IF NOT EXISTS document_chunks_parent_idx
  ON public.document_chunks(parent_chunk_id);
CREATE INDEX IF NOT EXISTS document_chunks_tsv_idx
  ON public.document_chunks USING gin(tsv);

COMMENT ON TABLE public.document_chunks IS
  'v3 RAG: hierarchical chunks of documents. parent_chunk_id forms a tree '
  '(leaf → section → document). tsv enables BM25 leg of hybrid search.';

-- ============================================================================
-- 2. chunk_embeddings_literature (1024-dim Matryoshka)
-- ============================================================================
-- Sources: pubmed, biorxiv, medrxiv. Lit reranks heavily so 1024-dim is
-- adequate; the smaller vector reduces index cost.

CREATE TABLE IF NOT EXISTS public.chunk_embeddings_literature (
  chunk_id uuid NOT NULL REFERENCES public.document_chunks(id) ON DELETE CASCADE,
  provider text NOT NULL CHECK (provider IN ('voyage','openai')),
  model text NOT NULL,
  dim int NOT NULL,
  embedding vector(1024) NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (chunk_id, provider, model)
);

CREATE INDEX IF NOT EXISTS chunk_embeddings_literature_voyage_hnsw
  ON public.chunk_embeddings_literature
  USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 200)
  WHERE provider = 'voyage';

CREATE INDEX IF NOT EXISTS chunk_embeddings_literature_openai_hnsw
  ON public.chunk_embeddings_literature
  USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 200)
  WHERE provider = 'openai';

-- ============================================================================
-- 3. chunk_embeddings_filings (2048-dim)
-- ============================================================================
-- Sources: edgar, federal_register, fda_advisory.

CREATE TABLE IF NOT EXISTS public.chunk_embeddings_filings (
  chunk_id uuid NOT NULL REFERENCES public.document_chunks(id) ON DELETE CASCADE,
  provider text NOT NULL CHECK (provider IN ('voyage','openai')),
  model text NOT NULL,
  dim int NOT NULL,
  embedding vector(2000) NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (chunk_id, provider, model)
);

CREATE INDEX IF NOT EXISTS chunk_embeddings_filings_voyage_hnsw
  ON public.chunk_embeddings_filings
  USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 200)
  WHERE provider = 'voyage';

CREATE INDEX IF NOT EXISTS chunk_embeddings_filings_openai_hnsw
  ON public.chunk_embeddings_filings
  USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 200)
  WHERE provider = 'openai';

-- ============================================================================
-- 4. chunk_embeddings_labels_aes (2048-dim)
-- ============================================================================
-- Sources: dailymed, faers, openfda, fda_warning_letter, fda_483.

CREATE TABLE IF NOT EXISTS public.chunk_embeddings_labels_aes (
  chunk_id uuid NOT NULL REFERENCES public.document_chunks(id) ON DELETE CASCADE,
  provider text NOT NULL CHECK (provider IN ('voyage','openai')),
  model text NOT NULL,
  dim int NOT NULL,
  embedding vector(2000) NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (chunk_id, provider, model)
);

CREATE INDEX IF NOT EXISTS chunk_embeddings_labels_aes_voyage_hnsw
  ON public.chunk_embeddings_labels_aes
  USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 200)
  WHERE provider = 'voyage';

CREATE INDEX IF NOT EXISTS chunk_embeddings_labels_aes_openai_hnsw
  ON public.chunk_embeddings_labels_aes
  USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 200)
  WHERE provider = 'openai';

-- ============================================================================
-- 5. chunk_embeddings_news (2048-dim)
-- ============================================================================
-- Sources: polygon_news, press_release, clinicaltrials.

CREATE TABLE IF NOT EXISTS public.chunk_embeddings_news (
  chunk_id uuid NOT NULL REFERENCES public.document_chunks(id) ON DELETE CASCADE,
  provider text NOT NULL CHECK (provider IN ('voyage','openai')),
  model text NOT NULL,
  dim int NOT NULL,
  embedding vector(2000) NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (chunk_id, provider, model)
);

CREATE INDEX IF NOT EXISTS chunk_embeddings_news_voyage_hnsw
  ON public.chunk_embeddings_news
  USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 200)
  WHERE provider = 'voyage';

CREATE INDEX IF NOT EXISTS chunk_embeddings_news_openai_hnsw
  ON public.chunk_embeddings_news
  USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 200)
  WHERE provider = 'openai';

-- ============================================================================
-- 6. citation_graph_cache
-- ============================================================================
-- Edges built incrementally by citation_graph.py from inline parsers (PubMed
-- references, EDGAR cross-refs) + explicit joins (DailyMed → ClinicalTrials
-- via NCT, FDA approval → CT.gov NCT).

CREATE TABLE IF NOT EXISTS public.citation_graph_cache (
  from_doc_id uuid NOT NULL REFERENCES public.documents(id) ON DELETE CASCADE,
  to_doc_id uuid NOT NULL REFERENCES public.documents(id) ON DELETE CASCADE,
  relation text NOT NULL CHECK (relation IN
    ('cites','cited_by','supersedes','label_for_nct','approval_for_nct',
     'amends','responds_to','same_compound')),
  confidence numeric(3,2) NOT NULL DEFAULT 1.0
    CHECK (confidence BETWEEN 0 AND 1),
  source_method text NOT NULL,  -- 'inline_parser', 'explicit_join', 'manual'
  computed_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (from_doc_id, to_doc_id, relation)
);

CREATE INDEX IF NOT EXISTS citation_graph_cache_to_idx
  ON public.citation_graph_cache(to_doc_id, relation);

-- ============================================================================
-- 7. retrieval_cache
-- ============================================================================
-- Cache key = sha256(normalized_query + corpus_filter + k + reranker_model).
-- TTL: 24h for stable corpora (literature, filings, labels_aes), 1h for news.

CREATE TABLE IF NOT EXISTS public.retrieval_cache (
  cache_key text PRIMARY KEY,
  query_text text NOT NULL,
  corpus_filter jsonb NOT NULL,
  k int NOT NULL,
  reranker_model text,
  result_chunk_ids uuid[] NOT NULL,
  result_scores numeric[] NOT NULL,
  hit_count int NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS retrieval_cache_expires_idx
  ON public.retrieval_cache(expires_at);

-- ============================================================================
-- 8. rag_eval_gold + rag_eval_log
-- ============================================================================
-- Gold set is seeded from eval_harness.document_set: for each held-out asset,
-- an LLM-assisted curation pass generates 3-5 questions per asset answerable
-- from those docs. Target ~200 gold rows at v1.

CREATE TABLE IF NOT EXISTS public.rag_eval_gold (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  question text NOT NULL,
  corpus_filter jsonb NOT NULL,
  gold_chunk_ids uuid[] NOT NULL,
  gold_answer text NOT NULL,
  category text NOT NULL CHECK (category IN
    ('literature','safety','regulatory','financial','competitive','mechanism')),
  difficulty text NOT NULL DEFAULT 'medium'
    CHECK (difficulty IN ('easy','medium','hard')),
  source_asset_id uuid REFERENCES public.fda_assets(id) ON DELETE SET NULL,
  created_by text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS rag_eval_gold_category_idx
  ON public.rag_eval_gold(category, difficulty);

CREATE TABLE IF NOT EXISTS public.rag_eval_log (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  gold_id uuid NOT NULL REFERENCES public.rag_eval_gold(id) ON DELETE CASCADE,
  commit_sha text,
  provider_config jsonb NOT NULL,  -- {provider, embedder_model, reranker_model, k}
  retrieved_chunk_ids uuid[] NOT NULL,
  generated_answer text,
  answer_relevancy numeric(4,3),
  faithfulness numeric(4,3),
  context_recall numeric(4,3),
  context_precision numeric(4,3),
  passed boolean NOT NULL,
  fail_reason text,
  latency_ms int,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS rag_eval_log_commit_idx
  ON public.rag_eval_log(commit_sha, created_at DESC);
CREATE INDEX IF NOT EXISTS rag_eval_log_gold_idx
  ON public.rag_eval_log(gold_id, created_at DESC);

COMMENT ON TABLE public.rag_eval_log IS
  'v3 RAG: per-question eval result. Gate fails on faithfulness < 0.85, '
  'context_recall < 0.75, mean answer_relevancy < 0.70, or > 5% regression.';

-- ============================================================================
-- 9. RPCs — rag_bm25_search, rag_dense_search
-- ============================================================================
-- hybrid_search.py calls these via PostgREST `rpc/`. Both filter by corpus
-- via the document.source mapping. Dense search dispatches to the right
-- chunk_embeddings_<corpus> table via dynamic SQL.

CREATE OR REPLACE FUNCTION public._rag_corpus_sources(p_corpus text)
RETURNS text[] LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE p_corpus
    WHEN 'literature' THEN ARRAY['pubmed','biorxiv','medrxiv']
    WHEN 'filings' THEN ARRAY['edgar','federal_register','fda_advisory']
    WHEN 'labels_aes' THEN ARRAY['dailymed','faers','openfda',
                                 'fda_warning_letter','fda_483']
    WHEN 'news' THEN ARRAY['polygon_news','press_release','clinicaltrials']
    ELSE ARRAY[]::text[]
  END;
$$;

CREATE OR REPLACE FUNCTION public.rag_bm25_search(
  p_query text,
  p_corpus text,
  p_top int DEFAULT 50,
  p_document_ids uuid[] DEFAULT NULL
)
RETURNS TABLE (chunk_id uuid, score real)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_sources text[] := public._rag_corpus_sources(p_corpus);
  v_query tsquery;
BEGIN
  v_query := plainto_tsquery('english', p_query);
  RETURN QUERY
    SELECT dc.id AS chunk_id,
           ts_rank_cd(dc.tsv, v_query)::real AS score
      FROM public.document_chunks dc
      JOIN public.documents d ON d.id = dc.document_id
     WHERE dc.tsv @@ v_query
       AND (cardinality(v_sources) = 0 OR d.source = ANY(v_sources))
       AND (p_document_ids IS NULL OR dc.document_id = ANY(p_document_ids))
     ORDER BY score DESC
     LIMIT p_top;
END;
$$;

GRANT EXECUTE ON FUNCTION public.rag_bm25_search TO authenticated, anon, service_role;

CREATE OR REPLACE FUNCTION public.rag_dense_search(
  p_embedding double precision[],
  p_corpus text,
  p_provider text,
  p_top int DEFAULT 50,
  p_document_ids uuid[] DEFAULT NULL
)
RETURNS TABLE (chunk_id uuid, similarity real)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_table text;
  v_sql text;
BEGIN
  v_table := CASE p_corpus
    WHEN 'literature' THEN 'chunk_embeddings_literature'
    WHEN 'filings' THEN 'chunk_embeddings_filings'
    WHEN 'labels_aes' THEN 'chunk_embeddings_labels_aes'
    WHEN 'news' THEN 'chunk_embeddings_news'
    ELSE NULL
  END;
  IF v_table IS NULL THEN
    RETURN;
  END IF;
  -- HNSW search with cosine distance; partial index `WHERE provider=$1`
  -- already filters; chunks_id join hydrates document_id for the optional
  -- doc-allowlist clause.
  IF p_document_ids IS NULL THEN
    v_sql := format(
      'SELECT ce.chunk_id, (1 - (ce.embedding <=> $1::vector))::real AS similarity '
      'FROM public.%I ce '
      'WHERE ce.provider = $2 '
      'ORDER BY ce.embedding <=> $1::vector '
      'LIMIT $3', v_table);
    RETURN QUERY EXECUTE v_sql USING p_embedding, p_provider, p_top;
  ELSE
    v_sql := format(
      'SELECT ce.chunk_id, (1 - (ce.embedding <=> $1::vector))::real AS similarity '
      'FROM public.%I ce '
      'JOIN public.document_chunks dc ON dc.id = ce.chunk_id '
      'WHERE ce.provider = $2 AND dc.document_id = ANY($4) '
      'ORDER BY ce.embedding <=> $1::vector '
      'LIMIT $3', v_table);
    RETURN QUERY EXECUTE v_sql USING p_embedding, p_provider, p_top, p_document_ids;
  END IF;
END;
$$;

GRANT EXECUTE ON FUNCTION public.rag_dense_search TO authenticated, anon, service_role;
