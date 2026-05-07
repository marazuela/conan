-- 20260506000010_v3_phase_0_1_schema.sql
-- Conan v3 orchestrator: Phase 0 (eval harness) + Phase 1 (document buffer) schema
-- Plan ref: /Users/Pico/.claude/plans/confirm-orchestrator-cuddly-bubble.md
--
-- This migration lays the foundation for the v3 FDA + EDGAR orchestrator. It is
-- additive only — no existing tables are dropped or renamed. Existing v2 tables
-- (signals, candidates, thesis_jobs, fda_regulatory_events) continue to function
-- and only freeze for FDA-vertical writes after Phase 7 cutover.

-- ============================================================================
-- 1. fda_assets — additive extensions
-- ============================================================================

ALTER TABLE fda_assets
  ADD COLUMN IF NOT EXISTS indication_normalized text,
  ADD COLUMN IF NOT EXISTS program_status text,
  ADD COLUMN IF NOT EXISTS is_active boolean NOT NULL DEFAULT true,
  ADD COLUMN IF NOT EXISTS watch_priority smallint NOT NULL DEFAULT 3,
  ADD COLUMN IF NOT EXISTS reviewer_panel_id text,
  ADD COLUMN IF NOT EXISTS reference_class_signature text,
  ADD COLUMN IF NOT EXISTS memory_path text;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.check_constraints
    WHERE constraint_name = 'fda_assets_program_status_check'
  ) THEN
    ALTER TABLE fda_assets
      ADD CONSTRAINT fda_assets_program_status_check
      CHECK (program_status IS NULL OR program_status IN
        ('preclinical','phase1','phase2','phase3','filed','approved','discontinued','withdrawn'));
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.check_constraints
    WHERE constraint_name = 'fda_assets_watch_priority_check'
  ) THEN
    ALTER TABLE fda_assets
      ADD CONSTRAINT fda_assets_watch_priority_check
      CHECK (watch_priority BETWEEN 1 AND 5);
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS fda_assets_active_priority_idx
  ON fda_assets(is_active, watch_priority);
CREATE INDEX IF NOT EXISTS fda_assets_reference_class_idx
  ON fda_assets(reference_class_signature);

-- ============================================================================
-- 2. documents — canonical raw-doc buffer
-- Replaces the orphaned `filings` table. Every ingestion adapter writes here
-- via modal_workers/shared/document_writer.py. anthropic_file_id is populated
-- when the document is uploaded to Anthropic Files API (PDFs especially).
-- ============================================================================

CREATE TABLE IF NOT EXISTS documents (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source text NOT NULL CHECK (source IN (
    'edgar','federal_register','openfda','clinicaltrials','dailymed','faers',
    'fda_advisory','fda_warning_letter','fda_483','pubmed','biorxiv','medrxiv',
    'polygon_news','press_release'
  )),
  source_doc_id text NOT NULL,
  source_content_hash text NOT NULL,
  url text,
  doc_type text NOT NULL,
  storage_path text,                   -- Supabase Storage path when raw_text > 512KB
  raw_text text,
  raw_text_tokens int,
  anthropic_file_id text,              -- Files API persistent reference
  is_pdf boolean NOT NULL DEFAULT false,
  title text,
  published_at timestamptz NOT NULL,
  fetched_at timestamptz NOT NULL DEFAULT now(),
  language text DEFAULT 'en',
  extensions jsonb NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE (source, source_content_hash)
);

CREATE INDEX IF NOT EXISTS documents_published_idx
  ON documents(published_at DESC);
CREATE INDEX IF NOT EXISTS documents_source_type_idx
  ON documents(source, doc_type, published_at DESC);
CREATE INDEX IF NOT EXISTS documents_anthropic_file_idx
  ON documents(anthropic_file_id) WHERE anthropic_file_id IS NOT NULL;

COMMENT ON TABLE documents IS
  'v3: canonical raw-doc buffer. Replaces orphaned filings table. '
  'Every adapter writes here via document_writer. anthropic_file_id is set '
  'when uploaded to Files API.';

-- ============================================================================
-- 3. fda_asset_parties — co-development / licensing relationships
-- ============================================================================

CREATE TABLE IF NOT EXISTS fda_asset_parties (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  asset_id uuid NOT NULL REFERENCES fda_assets(id) ON DELETE CASCADE,
  entity_id uuid NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  role text NOT NULL CHECK (role IN
    ('sponsor','collaborator','licensee','licensor','manufacturer','royalty_holder')),
  economic_share numeric(5,4),
  effective_from date,
  effective_to date,
  source_document_id uuid REFERENCES documents(id),
  notes text,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (asset_id, entity_id, role, effective_from)
);

CREATE INDEX IF NOT EXISTS fda_asset_parties_entity_idx
  ON fda_asset_parties(entity_id) WHERE effective_to IS NULL;

COMMENT ON TABLE fda_asset_parties IS
  'v3: co-development/licensing relationships. Replaces single-entity '
  'assumption on fda_assets.entity_id for compounds with multiple sponsors '
  '(e.g. Regeneron/Sanofi Dupixent).';

-- ============================================================================
-- 4. asset_documents — junction with extraction confidence + spans
-- Written by the Sonnet asset linker (two-pass: pass-1 fast classification,
-- pass-2 verification on ambiguous links). Inserts here trigger orchestrator
-- runs when link_type IN (primary, safety_signal) AND confidence >= 0.7.
-- ============================================================================

CREATE TABLE IF NOT EXISTS asset_documents (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  asset_id uuid NOT NULL REFERENCES fda_assets(id) ON DELETE CASCADE,
  document_id uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  link_type text NOT NULL CHECK (link_type IN
    ('primary','mentions','pipeline_context','safety_signal','literature')),
  extraction_method text NOT NULL CHECK (extraction_method IN
    ('regex','ner','agent_pass1','agent_pass2','manual')),
  extraction_confidence numeric(3,2) CHECK (extraction_confidence BETWEEN 0 AND 1),
  extracted_spans jsonb,
  is_material boolean NOT NULL DEFAULT true,
  verified_by_pass2 boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (asset_id, document_id, link_type)
);

CREATE INDEX IF NOT EXISTS asset_documents_asset_idx
  ON asset_documents(asset_id, created_at DESC);

COMMENT ON TABLE asset_documents IS
  'v3: junction linking fda_assets to documents with extraction confidence '
  '+ spans. Written by Sonnet asset linker (two-pass). Triggers '
  'orchestrator_runs enqueue when link_type IN (primary, safety_signal) '
  'AND extraction_confidence >= 0.7.';

-- ============================================================================
-- 5. extracted_facts — per-doc structured fact layer (Sonnet extractor output)
-- Each fact has a verbatim evidence_quote + citation_span. Orchestrator
-- Stage 1 reads from here as the primary input, NOT raw documents
-- (raw docs are an escape hatch via fetch_full_document tool).
-- ============================================================================

CREATE TABLE IF NOT EXISTS extracted_facts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  document_id uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  asset_id uuid REFERENCES fda_assets(id) ON DELETE SET NULL,
  fact_type text NOT NULL,             -- 'pdufa_date','adcom_vote','phase3_endpoint','safety_signal','insider_buy', etc.
  fact_text text NOT NULL,
  evidence_quote text NOT NULL,        -- verbatim from source
  citation_span jsonb NOT NULL,        -- {start, end, page}
  confidence numeric(3,2),
  extraction_model text NOT NULL,      -- 'claude-sonnet-4-6'
  extracted_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS extracted_facts_asset_type_idx
  ON extracted_facts(asset_id, fact_type);
CREATE INDEX IF NOT EXISTS extracted_facts_document_idx
  ON extracted_facts(document_id);

COMMENT ON TABLE extracted_facts IS
  'v3: per-doc structured fact layer (Sonnet extractor output). Each fact '
  'has verbatim evidence_quote + citation_span. Orchestrator Stage 1 reads '
  'from here, NOT raw documents.';

-- ============================================================================
-- 6. memory_files — index into Supabase Storage memory hierarchy
-- Per-asset, per-indication, per-reviewer-panel, per-reference-class, and
-- per-sub-agent memory files in Supabase Storage. Hierarchical Bayesian
-- priors + sub-agent state preserved across runs.
-- ============================================================================

CREATE TABLE IF NOT EXISTS memory_files (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  scope text NOT NULL CHECK (scope IN
    ('asset','indication','reviewer_panel','reference_class','sub_agent')),
  scope_id text NOT NULL,              -- for sub_agent: '<role>/<asset_id>' or '<role>/<indication>'
  storage_path text NOT NULL,
  size_bytes int,
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (scope, scope_id)
);

COMMENT ON TABLE memory_files IS
  'v3: index into Supabase Storage memory hierarchy. scope=asset|'
  'indication|reviewer_panel|reference_class|sub_agent.';

-- ============================================================================
-- 7. reference_class_base_rates — empirical FDA approval rates by class
-- Queried by Stage 4 (reference-class anchoring) via compute-mcp's
-- compute_base_rate tool. Refit nightly from logged outcomes.
-- ============================================================================

CREATE TABLE IF NOT EXISTS reference_class_base_rates (
  reference_class text PRIMARY KEY,    -- e.g. 'phase3_oncology_breakthrough_no_prior_crl'
  n_cases int NOT NULL,
  approval_rate numeric(4,3) NOT NULL,
  approval_rate_ci_low numeric(4,3),
  approval_rate_ci_high numeric(4,3),
  median_realized_move_pct numeric(5,2),
  refit_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE reference_class_base_rates IS
  'v3: empirical FDA approval rates by reference class signature. Refit '
  'nightly from logged outcomes via post_mortem_runner.';

-- ============================================================================
-- 8. calibration_curves — isotonic regression coefficients for conviction
-- Stage 8 of orchestrator applies the active curve to raw conviction_pct.
-- Refit nightly when >=10 new resolved outcomes accumulated.
-- ============================================================================

CREATE TABLE IF NOT EXISTS calibration_curves (
  version text PRIMARY KEY,            -- e.g. 'iso-2026-05-06'
  curve_data jsonb NOT NULL,           -- isotonic regression knots
  n_training_samples int NOT NULL,
  brier_score numeric(5,4),
  fitted_at timestamptz NOT NULL DEFAULT now(),
  is_active boolean NOT NULL DEFAULT false
);

-- Only one active curve at a time
CREATE UNIQUE INDEX IF NOT EXISTS calibration_curves_active_idx
  ON calibration_curves(is_active) WHERE is_active = true;

COMMENT ON TABLE calibration_curves IS
  'v3: isotonic regression curves for post-hoc conviction calibration. '
  'is_active=true row applied at Stage 8 of orchestrator.';

-- ============================================================================
-- 9. eval_harness — held-out resolved FDA signals (gold standard)
-- Phase 0 deliverable. 50+ historical FDA decisions (2023-2025) with
-- realized outcomes. Document set snapshotted as of reference_assessment_date.
-- ============================================================================

CREATE TABLE IF NOT EXISTS eval_harness (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  asset_id uuid NOT NULL REFERENCES fda_assets(id),
  reference_assessment_date date NOT NULL,
  realized_outcome text NOT NULL,      -- 'approved','crl','withdrawn','adcom_positive','adcom_negative', etc.
  realized_outcome_data jsonb NOT NULL,-- {outcome_date, realized_move_pct, market_cap_change, etc.}
  document_set uuid[] NOT NULL,        -- documents available as of reference date
  is_holdout boolean NOT NULL DEFAULT true,
  difficulty text CHECK (difficulty IN ('easy','medium','hard')),
  notes text,
  added_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS eval_harness_holdout_idx
  ON eval_harness(is_holdout) WHERE is_holdout = true;
CREATE INDEX IF NOT EXISTS eval_harness_asset_idx
  ON eval_harness(asset_id);

COMMENT ON TABLE eval_harness IS
  'v3 Phase 0: 50+ held-out resolved historical FDA signals with realized '
  'outcomes. Gold standard for prompt iteration. Curated from 2023-2025 '
  'FDA decisions.';

-- ============================================================================
-- 10. eval_runs — every prompt change tested against eval_harness
-- CI integration: PR fails if non-regressive Brier on harness.
-- ============================================================================

CREATE TABLE IF NOT EXISTS eval_runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  orchestrator_version text NOT NULL,
  prompt_hash text NOT NULL,
  brier_score numeric(5,4),
  calibration_curve jsonb,
  ranking_auc numeric(4,3),
  per_assessment_results jsonb,
  passed_gate boolean NOT NULL,
  notes text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS eval_runs_passed_idx
  ON eval_runs(passed_gate, created_at DESC);
CREATE INDEX IF NOT EXISTS eval_runs_version_idx
  ON eval_runs(orchestrator_version, created_at DESC);

COMMENT ON TABLE eval_runs IS
  'v3: every prompt change tested against eval_harness. Brier + calibration '
  'curve + ranking AUC. PR fails if non-regressive Brier.';

-- ============================================================================
-- End of v3 Phase 0+1 foundation schema
-- ============================================================================
