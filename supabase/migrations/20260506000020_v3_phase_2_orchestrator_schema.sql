-- 20260506000020_v3_phase_2_orchestrator_schema.sql
-- Conan v3 orchestrator: Phase 2 schema (orchestrator runtime + outputs)
-- Plan ref: /Users/Pico/.claude/plans/confirm-orchestrator-cuddly-bubble.md
--
-- Adds the orchestrator output + queue + observability tables. Additive only.
-- v2 signals/candidates/thesis_jobs continue to function unchanged.

-- ============================================================================
-- 1. orchestrator_runs — queue + cost tracking
-- ============================================================================
-- Modal worker (or scheduled poller) drains pending rows; tier dispatches to
-- API SDK direct (Tier 1), Cowork bulk (Tier 2), or Batch backtest (Tier 3).
-- ============================================================================

CREATE TABLE IF NOT EXISTS orchestrator_runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  asset_id uuid NOT NULL REFERENCES fda_assets(id) ON DELETE CASCADE,
  trigger_type text NOT NULL CHECK (trigger_type IN
    ('new_doc','cross_source','scheduled','operator_refresh','market_move',
     'tier2_escalation','backtest','manual')),
  trigger_doc_id uuid REFERENCES documents(id),
  tier int NOT NULL DEFAULT 1 CHECK (tier IN (1, 2, 3)),
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','running','completed','skipped_dedupe',
                      'skipped_budget','failed')),
  scheduled_at timestamptz NOT NULL DEFAULT now(),
  started_at timestamptz,
  completed_at timestamptz,
  assessment_id uuid,                    -- FK added below after convergence_assessments created
  error_message text,
  cost_estimate_usd numeric(8,4),
  cost_actual_usd numeric(8,4),
  notes jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS orchestrator_runs_pending_idx
  ON orchestrator_runs(scheduled_at) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS orchestrator_runs_asset_idx
  ON orchestrator_runs(asset_id, created_at DESC);
CREATE INDEX IF NOT EXISTS orchestrator_runs_tier_status_idx
  ON orchestrator_runs(tier, status, scheduled_at);

COMMENT ON TABLE orchestrator_runs IS
  'v3: orchestrator queue. tier=1 API SDK direct (hot+escalations); tier=2 Cowork bulk; tier=3 Batch backtest.';

-- ============================================================================
-- 2. convergence_assessments — orchestrator output (the main row)
-- ============================================================================
-- One row per (asset, trigger). Holds the full 10-stage pipeline output:
-- evidence ledger (Stage 1) + hypotheses (Stage 2) + critique (Stage 3) +
-- reference class (Stage 4) + synthesis (Stage 5) + ensemble (Stage 6) +
-- constitutional pass (Stage 7) + isotonic calibration (Stage 8) + extraction
-- (Stage 9) + memory writeback receipt (Stage 10).
--
-- band is derived from conviction_pct via thresholds in internal_config (NOT
-- hardcoded — probabilistic outputs first, categorical labels second).
-- ============================================================================

CREATE TABLE IF NOT EXISTS convergence_assessments (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  asset_id uuid NOT NULL REFERENCES fda_assets(id) ON DELETE CASCADE,
  orchestrator_version text NOT NULL,
  model_id text NOT NULL,                -- e.g. 'claude-opus-4-7'
  trigger_type text NOT NULL CHECK (trigger_type IN
    ('new_doc','cross_source','scheduled','operator_refresh','market_move',
     'tier2_escalation','backtest','manual')),
  trigger_doc_id uuid REFERENCES documents(id),
  document_window_start timestamptz NOT NULL,
  document_window_end timestamptz NOT NULL,
  document_ids uuid[] NOT NULL,          -- corpus the agent saw
  fact_ids uuid[] NOT NULL DEFAULT '{}', -- extracted_facts the agent saw
  -- Stage 1 evidence
  evidence_ledger jsonb NOT NULL DEFAULT '{}'::jsonb,
  reasoning_trace text,
  cited_prose_blocks jsonb NOT NULL DEFAULT '[]'::jsonb,
  key_facts jsonb NOT NULL DEFAULT '[]'::jsonb,
  uncertainties jsonb NOT NULL DEFAULT '[]'::jsonb,
  -- Stage 2 hypotheses
  hypotheses jsonb,
  -- Stage 3 critique
  pre_mortem text,
  adversarial_challenges jsonb,
  -- Stage 4 reference class
  reference_class text,
  reference_class_base_rate numeric(4,3),
  similar_resolved_case_ids uuid[],
  -- Stage 5 synthesis
  raw_conviction_pct numeric(5,2)
    CHECK (raw_conviction_pct IS NULL OR (raw_conviction_pct BETWEEN 0 AND 100)),
  thesis_direction text CHECK (thesis_direction IS NULL OR thesis_direction IN
    ('long','short','neutral','straddle')),
  thesis_summary text,
  -- Stage 6 ensemble (N=7 batch or N=3 streaming)
  ensemble_n int NOT NULL DEFAULT 1,
  ensemble_runs jsonb,                   -- per-run conviction + direction
  ensemble_mean numeric(5,2),
  ensemble_dispersion numeric(5,2),
  shrinkage_factor numeric(3,2),
  -- Stage 7 constitutional check (Sonnet validator)
  constitutional_pass boolean,
  constitutional_findings jsonb,
  constitutional_retries int NOT NULL DEFAULT 0,
  -- Stage 8 isotonic calibration
  conviction_pct_calibrated numeric(5,2)
    CHECK (conviction_pct_calibrated IS NULL OR
           (conviction_pct_calibrated BETWEEN 0 AND 100)),
  calibration_curve_version text REFERENCES calibration_curves(version),
  -- Final output (= conviction_pct_calibrated minus dispersion shrinkage)
  conviction_pct numeric(5,2)
    CHECK (conviction_pct IS NULL OR (conviction_pct BETWEEN 0 AND 100)),
  evidence_quality numeric(3,2)
    CHECK (evidence_quality IS NULL OR (evidence_quality BETWEEN 0 AND 1)),
  band text CHECK (band IS NULL OR band IN
    ('immediate','watchlist','archive','discard')),
  -- Market context snapshot at run time
  market_implied_move numeric(5,2),
  expected_value_bps numeric(8,2),
  options_iv numeric(5,2),
  -- Bookkeeping
  superseded_by uuid REFERENCES convergence_assessments(id),
  superseded_at timestamptz,
  -- Cost / latency / cache (totals across all stages)
  total_input_tokens int NOT NULL DEFAULT 0,
  total_output_tokens int NOT NULL DEFAULT 0,
  total_thinking_tokens int NOT NULL DEFAULT 0,
  total_cache_read_tokens int NOT NULL DEFAULT 0,
  total_cache_creation_tokens int NOT NULL DEFAULT 0,
  cost_usd numeric(8,4) NOT NULL DEFAULT 0,
  latency_ms int,
  cache_hit_ratio numeric(3,2),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS convergence_assessments_asset_idx
  ON convergence_assessments(asset_id, created_at DESC) WHERE superseded_at IS NULL;
CREATE INDEX IF NOT EXISTS convergence_assessments_band_idx
  ON convergence_assessments(band, created_at DESC) WHERE superseded_at IS NULL;
CREATE INDEX IF NOT EXISTS convergence_assessments_trigger_idx
  ON convergence_assessments(trigger_type, created_at DESC);

COMMENT ON TABLE convergence_assessments IS
  'v3 orchestrator output. One row per (asset, trigger). Stage-1..10 pipeline data + ensemble + calibration + market snapshot + cost/latency.';

-- Backfill FK on orchestrator_runs.assessment_id
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.table_constraints
    WHERE constraint_name = 'orchestrator_runs_assessment_fk'
  ) THEN
    ALTER TABLE orchestrator_runs
      ADD CONSTRAINT orchestrator_runs_assessment_fk
      FOREIGN KEY (assessment_id) REFERENCES convergence_assessments(id)
      ON DELETE SET NULL;
  END IF;
END $$;

-- ============================================================================
-- 3. assessment_stage_metrics — per-stage observability
-- ============================================================================

CREATE TABLE IF NOT EXISTS assessment_stage_metrics (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  assessment_id uuid NOT NULL REFERENCES convergence_assessments(id) ON DELETE CASCADE,
  stage_name text NOT NULL,             -- 'stage_0_memory_load', 'stage_1_evidence', etc.
  model text NOT NULL,                  -- 'claude-opus-4-7', 'claude-sonnet-4-6', or 'compute'
  input_tokens int NOT NULL DEFAULT 0,
  output_tokens int NOT NULL DEFAULT 0,
  thinking_tokens int NOT NULL DEFAULT 0,
  cache_read_tokens int NOT NULL DEFAULT 0,
  cache_creation_tokens int NOT NULL DEFAULT 0,
  cost_usd numeric(8,4) NOT NULL DEFAULT 0,
  latency_ms int NOT NULL DEFAULT 0,
  status text NOT NULL DEFAULT 'completed'
    CHECK (status IN ('completed','failed','skipped','retry')),
  notes jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS assessment_stage_metrics_assessment_idx
  ON assessment_stage_metrics(assessment_id);
CREATE INDEX IF NOT EXISTS assessment_stage_metrics_stage_idx
  ON assessment_stage_metrics(stage_name, created_at DESC);

COMMENT ON TABLE assessment_stage_metrics IS
  'v3 per-stage cost/latency/tokens. One row per stage per assessment.';

-- ============================================================================
-- 4. sub_agent_calls — per-sub-agent observability
-- ============================================================================

CREATE TABLE IF NOT EXISTS sub_agent_calls (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  assessment_id uuid REFERENCES convergence_assessments(id) ON DELETE CASCADE,
  role text NOT NULL CHECK (role IN
    ('literature','competitive','regulatory_history','options_microstructure')),
  query text NOT NULL,
  output jsonb NOT NULL DEFAULT '{}'::jsonb,
  schema_pass boolean NOT NULL DEFAULT false,
  schema_retries int NOT NULL DEFAULT 0,
  tokens int NOT NULL DEFAULT 0,
  cost_usd numeric(8,4) NOT NULL DEFAULT 0,
  latency_ms int NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sub_agent_calls_assessment_idx
  ON sub_agent_calls(assessment_id);
CREATE INDEX IF NOT EXISTS sub_agent_calls_role_idx
  ON sub_agent_calls(role, created_at DESC);

COMMENT ON TABLE sub_agent_calls IS
  'v3 per-sub-agent observability. role=literature|competitive|regulatory_history|options_microstructure. Logged whether sub-agent ran via API or Cowork routine.';

-- ============================================================================
-- 5. post_mortem_queue — closed feedback loop
-- ============================================================================
-- After an assessment, a row enters here with predicted outcome + window end.
-- post_mortem_runner job fires when window_end passes, looks up realized
-- outcome, computes prediction_error, generates post-mortem text, and updates
-- reference_class_base_rates + calibration_curves accordingly.
-- ============================================================================

CREATE TABLE IF NOT EXISTS post_mortem_queue (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  assessment_id uuid NOT NULL REFERENCES convergence_assessments(id) ON DELETE CASCADE,
  asset_id uuid NOT NULL REFERENCES fda_assets(id) ON DELETE CASCADE,
  predicted_outcome text NOT NULL,        -- e.g. 'approved', 'crl', 'long_thesis'
  predicted_conviction_pct numeric(5,2) NOT NULL,
  predicted_direction text NOT NULL,
  outcome_window_end timestamptz NOT NULL,
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','outcome_resolved','post_mortem_complete','no_outcome')),
  realized_outcome jsonb,
  realized_at timestamptz,
  post_mortem_text text,
  prediction_error numeric(5,2),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS post_mortem_queue_pending_idx
  ON post_mortem_queue(outcome_window_end) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS post_mortem_queue_asset_idx
  ON post_mortem_queue(asset_id);

COMMENT ON TABLE post_mortem_queue IS
  'v3 closed feedback loop. Resolved outcomes update reference_class_base_rates + calibration_curves.';

-- ============================================================================
-- 6. Realtime publication so extractor + asset linker + run-poller can subscribe
-- ============================================================================

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_publication_tables
    WHERE pubname = 'supabase_realtime' AND tablename = 'documents'
  ) THEN
    ALTER PUBLICATION supabase_realtime ADD TABLE documents;
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_publication_tables
    WHERE pubname = 'supabase_realtime' AND tablename = 'orchestrator_runs'
  ) THEN
    ALTER PUBLICATION supabase_realtime ADD TABLE orchestrator_runs;
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_publication_tables
    WHERE pubname = 'supabase_realtime' AND tablename = 'asset_documents'
  ) THEN
    ALTER PUBLICATION supabase_realtime ADD TABLE asset_documents;
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_publication_tables
    WHERE pubname = 'supabase_realtime' AND tablename = 'convergence_assessments'
  ) THEN
    ALTER PUBLICATION supabase_realtime ADD TABLE convergence_assessments;
  END IF;
END $$;

-- ============================================================================
-- End of v3 Phase 2 schema
-- ============================================================================
