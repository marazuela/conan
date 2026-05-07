-- Conan v2 — FDA Event-Investing Cockpit V1, Phase 1 schema.
--
-- Separates regulatory event truth from trade-signal scoring:
--   - fda_assets: ticker/entity/drug/application identity.
--   - fda_regulatory_events: one row per PDUFA / AdCom / Phase 3 readout / EOP2 /
--     approval / CRL / date_change / withdrawal. Pending vs resolved.
--   - fda_event_evidence: append-only evidence from public sources + agents + manual.
--   - fda_event_features: latest model-ready snapshot. shadow_* columns hold
--     parallel-run values for Phase 3 comparison without polluting `signals`.
--   - fda_model_versions: versioned priors / designation modifiers / thresholds.
--     Mirrors the rubrics partial-unique pattern.
--   - fda_calibration_runs: holdout metrics + guardrail evidence + activation flag.
--   - fda_agent_reviews: structured outputs from medical / regulatory /
--     microstructure specialist agents. Citations + snapshot hash required.
--
-- Apply order: this migration creates tables only. Backfill from
-- pdufa_watchlist.json is a Python script (modal_workers/scripts/fda_backfill_watchlist.py)
-- since the JSON lives in Supabase Storage, not the DB.

-- ============================================================
-- fda_model_versions  (no inbound FKs from other fda_* tables; create first)
-- ============================================================

CREATE TABLE fda_model_versions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  version text NOT NULL UNIQUE,
  scope text NOT NULL CHECK (scope IN ('priors','thresholds','both')),
  priors_by_indication jsonb NOT NULL DEFAULT '{}'::jsonb,
  designation_modifiers jsonb NOT NULL DEFAULT '{}'::jsonb,
  band_thresholds jsonb NOT NULL DEFAULT '{}'::jsonb,
  sizing_caps jsonb NOT NULL DEFAULT '{}'::jsonb,
  effective_at timestamptz,
  superseded_at timestamptz,
  created_by text,
  notes text,
  created_at timestamptz NOT NULL DEFAULT now()
);
-- One active model version per scope (priors/thresholds/both) at a time.
CREATE UNIQUE INDEX fda_model_versions_one_active_per_scope_idx
  ON fda_model_versions(scope)
  WHERE superseded_at IS NULL AND effective_at IS NOT NULL;
ALTER TABLE fda_model_versions ENABLE ROW LEVEL SECURITY;
CREATE POLICY fda_model_versions_select ON fda_model_versions
  FOR SELECT TO authenticated USING (true);

-- ============================================================
-- fda_assets
-- ============================================================

CREATE TABLE fda_assets (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  ticker text NOT NULL,
  mic text,
  entity_id uuid REFERENCES entities(id) ON DELETE SET NULL,
  drug_name text NOT NULL,
  generic_name text,
  application_number text NOT NULL DEFAULT '',
  application_type text,
  indication text,
  mechanism text,
  sponsor_name text,
  extensions jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (ticker, drug_name, application_number)
);
CREATE INDEX fda_assets_ticker_mic_idx ON fda_assets(ticker, mic);
CREATE INDEX fda_assets_entity_idx ON fda_assets(entity_id);
CREATE TRIGGER fda_assets_updated BEFORE UPDATE ON fda_assets
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();
ALTER TABLE fda_assets ENABLE ROW LEVEL SECURITY;
CREATE POLICY fda_assets_select ON fda_assets
  FOR SELECT TO authenticated USING (true);

-- ============================================================
-- fda_regulatory_events
-- ============================================================

CREATE TABLE fda_regulatory_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  asset_id uuid NOT NULL REFERENCES fda_assets(id) ON DELETE CASCADE,
  event_type text NOT NULL CHECK (event_type IN (
    'pdufa','adcom','phase3_readout','eop2','approval','crl','presumed_crl','date_change','withdrawal'
  )),
  event_date date,
  event_status text NOT NULL DEFAULT 'pending' CHECK (event_status IN ('pending','resolved','superseded')),
  prior_event_id uuid REFERENCES fda_regulatory_events(id) ON DELETE SET NULL,
  source_content_hash text NOT NULL,
  notes text,
  extensions jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (asset_id, event_type, event_date, source_content_hash)
);
CREATE INDEX fda_regulatory_events_asset_idx ON fda_regulatory_events(asset_id);
CREATE INDEX fda_regulatory_events_event_date_idx ON fda_regulatory_events(event_date);
CREATE INDEX fda_regulatory_events_pending_idx
  ON fda_regulatory_events(event_date)
  WHERE event_status = 'pending';
CREATE TRIGGER fda_regulatory_events_updated BEFORE UPDATE ON fda_regulatory_events
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();
ALTER TABLE fda_regulatory_events ENABLE ROW LEVEL SECURITY;
CREATE POLICY fda_regulatory_events_select ON fda_regulatory_events
  FOR SELECT TO authenticated USING (true);

-- ============================================================
-- fda_event_evidence  (append-only)
-- ============================================================

CREATE TABLE fda_event_evidence (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  event_id uuid NOT NULL REFERENCES fda_regulatory_events(id) ON DELETE CASCADE,
  source text NOT NULL CHECK (source IN (
    'edgar','openfda','clinicaltrials','federal_register','polygon','manual',
    'agent_medical','agent_regulatory','agent_microstructure'
  )),
  evidence_type text NOT NULL,
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  citation_url text,
  hash text NOT NULL,
  evidence_status text NOT NULL DEFAULT 'active'
    CHECK (evidence_status IN ('active','rejected')),
  rejected_reason text,
  rejected_at timestamptz,
  rejected_by uuid,
  fetched_at timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (event_id, source, hash)
);
CREATE INDEX fda_event_evidence_event_fetched_idx
  ON fda_event_evidence(event_id, fetched_at DESC);
CREATE INDEX fda_event_evidence_active_idx
  ON fda_event_evidence(event_id)
  WHERE evidence_status = 'active';
ALTER TABLE fda_event_evidence ENABLE ROW LEVEL SECURITY;
CREATE POLICY fda_event_evidence_select ON fda_event_evidence
  FOR SELECT TO authenticated USING (true);

-- ============================================================
-- fda_event_features  (one current snapshot per event; shadow_* during Phase 3)
-- ============================================================

CREATE TABLE fda_event_features (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  event_id uuid NOT NULL UNIQUE REFERENCES fda_regulatory_events(id) ON DELETE CASCADE,
  snapshot_at timestamptz NOT NULL DEFAULT now(),
  -- Probability + payoff
  fair_probability numeric(5,4) CHECK (fair_probability IS NULL OR fair_probability BETWEEN 0 AND 1),
  market_implied_probability numeric(5,4) CHECK (market_implied_probability IS NULL OR market_implied_probability BETWEEN 0 AND 1),
  upside_pct numeric(7,3),
  downside_pct numeric(7,3),
  expected_value_pct numeric(7,3),
  pricing_edge numeric(7,4),
  -- Quality + liquidity
  evidence_confidence numeric(4,3) CHECK (evidence_confidence IS NULL OR evidence_confidence BETWEEN 0 AND 1),
  options_liquidity_score numeric(4,2),
  market_cap_usd numeric(18,2),
  adv_usd numeric(18,2),
  implied_move_pct numeric(7,3),
  -- Live scoring (post-cutover)
  score numeric(5,2),
  band signal_band,
  model_version_id uuid REFERENCES fda_model_versions(id) ON DELETE SET NULL,
  -- Shadow scoring (Phase 3 parallel run; nullable until populated)
  shadow_score numeric(5,2),
  shadow_band signal_band,
  shadow_model_version_id uuid REFERENCES fda_model_versions(id) ON DELETE SET NULL,
  shadow_expected_value_pct numeric(7,3),
  shadow_pricing_edge numeric(7,4),
  shadow_recorded_at timestamptz,
  -- Reproducibility
  raw_inputs jsonb NOT NULL DEFAULT '{}'::jsonb,
  inputs_hash text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX fda_event_features_snapshot_idx
  ON fda_event_features(snapshot_at DESC);
CREATE INDEX fda_event_features_band_idx ON fda_event_features(band);
CREATE INDEX fda_event_features_shadow_band_idx ON fda_event_features(shadow_band);
CREATE TRIGGER fda_event_features_updated BEFORE UPDATE ON fda_event_features
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();
ALTER TABLE fda_event_features ENABLE ROW LEVEL SECURITY;
CREATE POLICY fda_event_features_select ON fda_event_features
  FOR SELECT TO authenticated USING (true);

-- ============================================================
-- fda_calibration_runs
-- ============================================================

CREATE TABLE fda_calibration_runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  model_version_id uuid REFERENCES fda_model_versions(id) ON DELETE SET NULL,
  sample_size int NOT NULL,
  holdout_brier_old numeric(8,6),
  holdout_brier_new numeric(8,6),
  brier_relative_gain numeric(7,4),
  max_param_drift_pct numeric(6,3),
  recall_old numeric(5,4),
  recall_new numeric(5,4),
  post_edge_avoidance_old numeric(5,4),
  post_edge_avoidance_new numeric(5,4),
  realized_ev_old numeric(7,4),
  realized_ev_new numeric(7,4),
  passed boolean NOT NULL DEFAULT false,
  activated boolean NOT NULL DEFAULT false,
  rolled_back_at timestamptz,
  ran_at timestamptz NOT NULL DEFAULT now(),
  notes text
);
CREATE INDEX fda_calibration_runs_ran_idx ON fda_calibration_runs(ran_at DESC);
CREATE INDEX fda_calibration_runs_model_idx ON fda_calibration_runs(model_version_id);
ALTER TABLE fda_calibration_runs ENABLE ROW LEVEL SECURITY;
CREATE POLICY fda_calibration_runs_select ON fda_calibration_runs
  FOR SELECT TO authenticated USING (true);

-- ============================================================
-- fda_agent_reviews
-- ============================================================

CREATE TABLE fda_agent_reviews (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  event_id uuid NOT NULL REFERENCES fda_regulatory_events(id) ON DELETE CASCADE,
  agent_kind text NOT NULL CHECK (agent_kind IN ('medical','regulatory','microstructure')),
  version text NOT NULL,
  structured_output jsonb NOT NULL DEFAULT '{}'::jsonb,
  citations jsonb NOT NULL DEFAULT '[]'::jsonb,
  confidence numeric(4,3) CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
  snapshot_hash text NOT NULL,
  status text NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued','running','completed','failed')),
  error_message text,
  ran_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (event_id, agent_kind, snapshot_hash)
);
CREATE INDEX fda_agent_reviews_event_kind_idx ON fda_agent_reviews(event_id, agent_kind, ran_at DESC);
CREATE INDEX fda_agent_reviews_queued_idx
  ON fda_agent_reviews(created_at)
  WHERE status = 'queued';
CREATE TRIGGER fda_agent_reviews_updated BEFORE UPDATE ON fda_agent_reviews
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();
ALTER TABLE fda_agent_reviews ENABLE ROW LEVEL SECURITY;
CREATE POLICY fda_agent_reviews_select ON fda_agent_reviews
  FOR SELECT TO authenticated USING (true);
