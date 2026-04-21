-- Conan v2 — initial schema migration
-- Source of truth: spec.md Appendix A (verbatim).
-- Targets Supabase project ref xvwvwbnxdsjpnealarkh (eu-west-3).
-- Apply with: supabase db push (from project root after `supabase link`).
--
-- Storage buckets (filings, scanner-caches, reports) are created out-of-band
-- via the Supabase Dashboard or CLI, not via SQL. See spec §4.

-- Extensions
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;

-- Shared enums
CREATE TYPE signal_band AS ENUM ('immediate','watchlist','archive','discard');
CREATE TYPE candidate_state AS ENUM ('watch','active','killed','delivered');

-- set_updated_at helper
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END; $$;

-- ============================================================
-- Registry (config-as-data)
-- ============================================================

CREATE TABLE sources (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL UNIQUE,
  kind text NOT NULL CHECK (kind IN ('edgar','esma','fda','lse','tdnet','asx','sedar','hkex','kind','bse_nse','cvm','bmv','courtlistener','sec_enforcement','clinicaltrials')),
  base_url text,
  notes text,
  created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE sources ENABLE ROW LEVEL SECURITY;
CREATE POLICY sources_select ON sources FOR SELECT TO authenticated USING (true);

CREATE TABLE scanners (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL UNIQUE,
  tool_path text,
  status text NOT NULL DEFAULT 'operational' CHECK (status IN ('operational','planned','deprecated','experimental')),
  geography text,
  cadence text NOT NULL CHECK (cadence IN ('3h','daily','weekly','on_demand')),
  default_scoring_profile text NOT NULL,
  signal_type_profile_map jsonb NOT NULL DEFAULT '{}'::jsonb,
  endpoints jsonb NOT NULL DEFAULT '{}'::jsonb,
  timeout_soft_s int NOT NULL DEFAULT 60,
  timeout_hard_s int NOT NULL DEFAULT 120,
  config jsonb NOT NULL DEFAULT '{}'::jsonb,
  last_run_utc timestamptz,
  last_run_status text,
  last_run_signals int,
  last_probe_at timestamptz,
  last_probe_status text CHECK (last_probe_status IS NULL OR last_probe_status IN ('ok','fallback','drift','content_shape_drift','timeout','error')),
  last_probe_latency_ms int,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX scanners_status_idx ON scanners(status);
CREATE TRIGGER scanners_updated BEFORE UPDATE ON scanners FOR EACH ROW EXECUTE FUNCTION set_updated_at();
ALTER TABLE scanners ENABLE ROW LEVEL SECURITY;
CREATE POLICY scanners_select ON scanners FOR SELECT TO authenticated USING (true);

CREATE TABLE rubrics (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  profile text NOT NULL,
  rubric_version int NOT NULL,
  dimension_weights jsonb NOT NULL,
  effective_at timestamptz NOT NULL DEFAULT now(),
  superseded_at timestamptz,
  notes text,
  UNIQUE (profile, rubric_version)
);
CREATE INDEX rubrics_active_idx ON rubrics(profile) WHERE superseded_at IS NULL;
ALTER TABLE rubrics ENABLE ROW LEVEL SECURITY;
CREATE POLICY rubrics_select ON rubrics FOR SELECT TO authenticated USING (true);

CREATE TABLE pe_filer_allowlist (
  filer_name text PRIMARY KEY,
  cik text,
  filer_type text NOT NULL CHECK (filer_type IN ('pe','activist_crossover')),
  notes text,
  created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE pe_filer_allowlist ENABLE ROW LEVEL SECURITY;
CREATE POLICY pe_filer_select ON pe_filer_allowlist FOR SELECT TO authenticated USING (true);

CREATE TABLE phase3_base_rates (
  indication text PRIMARY KEY,
  phase3_to_approval numeric(4,3) NOT NULL CHECK (phase3_to_approval BETWEEN 0 AND 1),
  trial_design_adjustments jsonb NOT NULL DEFAULT '{}'::jsonb,
  notes text,
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER phase3_updated BEFORE UPDATE ON phase3_base_rates FOR EACH ROW EXECUTE FUNCTION set_updated_at();
ALTER TABLE phase3_base_rates ENABLE ROW LEVEL SECURITY;
CREATE POLICY phase3_select ON phase3_base_rates FOR SELECT TO authenticated USING (true);

-- ============================================================
-- Entity graph
-- ============================================================

CREATE TABLE entities (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  issuer_figi text UNIQUE,
  name text NOT NULL,
  primary_ticker text,
  primary_mic text,
  country text,
  market_cap_usd numeric(18,2),
  market_cap_as_of date,
  extensions jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX entities_ticker_mic_idx ON entities(primary_ticker, primary_mic);
CREATE TRIGGER entities_updated BEFORE UPDATE ON entities FOR EACH ROW EXECUTE FUNCTION set_updated_at();
ALTER TABLE entities ENABLE ROW LEVEL SECURITY;
CREATE POLICY entities_select ON entities FOR SELECT TO authenticated USING (true);

CREATE TABLE entity_identifiers (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  entity_id uuid NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  id_type text NOT NULL CHECK (id_type IN ('ticker_mic','codigo_cvm','id_empresa_biva','stock_code','cik','cnpj','isin','name_normalized')),
  id_value text NOT NULL,
  priority smallint NOT NULL DEFAULT 100,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (id_type, id_value)
);
CREATE INDEX entity_identifiers_entity_idx ON entity_identifiers(entity_id);
ALTER TABLE entity_identifiers ENABLE ROW LEVEL SECURITY;
CREATE POLICY entity_identifiers_select ON entity_identifiers FOR SELECT TO authenticated USING (true);

-- ============================================================
-- Raw evidence
-- ============================================================

CREATE TABLE filings (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_id uuid NOT NULL REFERENCES sources(id),
  entity_id uuid REFERENCES entities(id),
  source_content_hash text NOT NULL UNIQUE,
  storage_path text NOT NULL,
  url text,
  fetched_at timestamptz NOT NULL DEFAULT now(),
  published_at timestamptz,
  filing_type text,
  extensions jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX filings_entity_published_idx ON filings(entity_id, published_at DESC);
ALTER TABLE filings ENABLE ROW LEVEL SECURITY;
CREATE POLICY filings_select ON filings FOR SELECT TO authenticated USING (true);

-- ============================================================
-- Pipeline state
-- ============================================================

CREATE TABLE scanner_runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  scanner_id uuid NOT NULL REFERENCES scanners(id),
  started_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  status text NOT NULL CHECK (status IN ('running','ok','error','auth_required','partial','timeout')),
  signals_emitted int NOT NULL DEFAULT 0,
  errors jsonb NOT NULL DEFAULT '[]'::jsonb,
  modal_invocation_id text,
  raw_log_path text
);
CREATE INDEX scanner_runs_scanner_started_idx ON scanner_runs(scanner_id, started_at DESC);
ALTER TABLE scanner_runs ENABLE ROW LEVEL SECURITY;
CREATE POLICY scanner_runs_select ON scanner_runs FOR SELECT TO authenticated USING (true);

CREATE TABLE signals (
  signal_id text PRIMARY KEY,
  entity_id uuid REFERENCES entities(id),
  issuer_figi text,
  scanner_id uuid REFERENCES scanners(id),
  scanner_run_id uuid REFERENCES scanner_runs(id),
  scoring_profile text NOT NULL,
  rubric_version_id uuid NOT NULL REFERENCES rubrics(id),
  source_content_hash text NOT NULL,
  source_url text,
  source_date timestamptz NOT NULL,
  scan_date timestamptz NOT NULL,
  signal_type text NOT NULL,
  thesis_direction text CHECK (thesis_direction IN ('long','short','neutral')),
  strength_estimate smallint CHECK (strength_estimate BETWEEN 1 AND 5),
  imported boolean NOT NULL DEFAULT false,
  dimensions jsonb NOT NULL DEFAULT '{}'::jsonb,
  score numeric(5,2) NOT NULL,
  band signal_band NOT NULL,
  auto_caps_triggered text[] NOT NULL DEFAULT '{}',
  convergence_key text,
  convergence_bonus smallint NOT NULL DEFAULT 0 CHECK (convergence_bonus IN (0,5,10)),
  score_with_bonus numeric(5,2),
  band_with_bonus signal_band,
  convergence_evaluated_at timestamptz,
  raw_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  extensions jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (source_content_hash, scoring_profile)
);
CREATE INDEX signals_entity_scan_idx ON signals(entity_id, scan_date DESC);
CREATE INDEX signals_issuer_figi_scan_idx ON signals(issuer_figi, scan_date DESC);
CREATE INDEX signals_convergence_key_idx ON signals(convergence_key, scan_date DESC);
CREATE INDEX signals_immediate_idx ON signals(scan_date DESC) WHERE band_with_bonus = 'immediate';
ALTER TABLE signals ENABLE ROW LEVEL SECURITY;
CREATE POLICY signals_select ON signals FOR SELECT TO authenticated USING (true);

CREATE TABLE candidates (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  ticker text NOT NULL,
  mic text,
  entity_id uuid REFERENCES entities(id),
  state candidate_state NOT NULL DEFAULT 'watch',
  scoring_profile text,
  current_score numeric(5,2),
  current_band signal_band,
  dossier_markdown text,
  dossier_storage_path text,
  thesis_approved_at timestamptz,
  kill_conditions jsonb NOT NULL DEFAULT '[]'::jsonb,
  next_catalyst_date date,
  next_catalyst_window daterange,
  last_aging_evaluated_at timestamptz,
  extensions jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (ticker, mic),
  CONSTRAINT candidates_catalyst_exactly_one CHECK (
    (next_catalyst_date IS NULL) <> (next_catalyst_window IS NULL)
    OR (next_catalyst_date IS NULL AND next_catalyst_window IS NULL)
  ),
  CONSTRAINT candidates_kill_conditions_is_array CHECK (jsonb_typeof(kill_conditions) = 'array')
);
CREATE INDEX candidates_state_score_idx ON candidates(state, current_score DESC)
  WHERE state IN ('active','watch');
CREATE INDEX candidates_catalyst_date_idx ON candidates(next_catalyst_date)
  WHERE next_catalyst_date IS NOT NULL AND state IN ('active','watch');
CREATE INDEX candidates_aging_due_idx ON candidates(last_aging_evaluated_at NULLS FIRST)
  WHERE state IN ('active','watch');
CREATE TRIGGER candidates_updated BEFORE UPDATE ON candidates FOR EACH ROW EXECUTE FUNCTION set_updated_at();
ALTER TABLE candidates ENABLE ROW LEVEL SECURITY;
CREATE POLICY candidates_select ON candidates FOR SELECT TO authenticated USING (true);

CREATE TABLE candidate_aging_failures (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  candidate_id uuid NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
  attempt_at timestamptz NOT NULL DEFAULT now(),
  error_kind text NOT NULL CHECK (error_kind IN (
    'routine_error','routine_declined','hallucinated_trigger','quota_exhausted','gate_mismatch','other'
  )),
  error_message text,
  routine_output jsonb,
  consecutive_failures smallint NOT NULL DEFAULT 1
);
CREATE INDEX candidate_aging_failures_recent_idx
  ON candidate_aging_failures(candidate_id, attempt_at DESC);
ALTER TABLE candidate_aging_failures ENABLE ROW LEVEL SECURITY;
CREATE POLICY candidate_aging_failures_select
  ON candidate_aging_failures FOR SELECT TO authenticated USING (true);

CREATE TABLE operator_flags (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  severity text NOT NULL CHECK (severity IN ('info','warn','critical')),
  source text NOT NULL CHECK (source IN (
    'translation_health','scanner_probe','convergence_qa','candidate_aging',
    'thesis_writer','reactor','reporting_weekly','litigation_baselines','manual'
  )),
  kind text NOT NULL,
  scanner_id uuid REFERENCES scanners(id),
  entity_id uuid REFERENCES entities(id),
  signal_id text REFERENCES signals(signal_id),
  candidate_id uuid REFERENCES candidates(id),
  title text NOT NULL,
  body text,
  evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
  resolved_at timestamptz,
  resolved_by uuid REFERENCES auth.users(id),
  resolved_note text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX operator_flags_open_uniq
  ON operator_flags (
    source,
    kind,
    coalesce(scanner_id::text, ''),
    coalesce(entity_id::text, ''),
    coalesce(signal_id, ''),
    coalesce(candidate_id::text, '')
  )
  WHERE resolved_at IS NULL;
CREATE INDEX operator_flags_open_idx
  ON operator_flags(severity DESC, created_at DESC) WHERE resolved_at IS NULL;
CREATE TRIGGER operator_flags_updated BEFORE UPDATE ON operator_flags FOR EACH ROW EXECUTE FUNCTION set_updated_at();
ALTER TABLE operator_flags ENABLE ROW LEVEL SECURITY;
CREATE POLICY operator_flags_select ON operator_flags FOR SELECT TO authenticated USING (true);
CREATE POLICY operator_flags_resolve ON operator_flags FOR UPDATE TO authenticated
  USING (true) WITH CHECK (resolved_by = auth.uid());

CREATE TABLE candidate_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  candidate_id uuid NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
  event_type text NOT NULL CHECK (event_type IN ('created','state_changed','scored','note_added','thesis_drafted_by_claude','thesis_updated','thesis_approved_by_user','convergence','gate_rejected')),
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  user_id uuid REFERENCES auth.users(id),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX candidate_events_candidate_idx ON candidate_events(candidate_id, created_at DESC);
ALTER TABLE candidate_events ENABLE ROW LEVEL SECURITY;
CREATE POLICY candidate_events_select ON candidate_events FOR SELECT TO authenticated USING (true);

CREATE TABLE outcomes (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  candidate_id uuid NOT NULL REFERENCES candidates(id),
  outcome_type text NOT NULL CHECK (outcome_type IN ('delivered','killed','expired')),
  realized_return numeric(6,3),
  notes text,
  created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE outcomes ENABLE ROW LEVEL SECURITY;
CREATE POLICY outcomes_select ON outcomes FOR SELECT TO authenticated USING (true);

CREATE TABLE alerts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  entity_id uuid REFERENCES entities(id),
  signal_id text NOT NULL REFERENCES signals(signal_id),
  signal_fingerprint text NOT NULL,
  day_utc date NOT NULL DEFAULT (now() AT TIME ZONE 'UTC')::date,
  email_subject text,
  email_body_storage_path text,
  dispatched_at timestamptz,
  dispatched_to text[] NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (entity_id, signal_fingerprint, day_utc)
);
CREATE INDEX alerts_created_idx ON alerts(created_at DESC);
ALTER TABLE alerts ENABLE ROW LEVEL SECURITY;
CREATE POLICY alerts_select ON alerts FOR SELECT TO authenticated USING (true);

CREATE TABLE alert_deliveries (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  alert_id uuid NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
  channel text NOT NULL CHECK (channel IN ('email','realtime')),
  target text NOT NULL,
  status text NOT NULL CHECK (status IN ('queued','sent','failed','bounced')),
  resend_message_id text,
  response_body jsonb,
  attempt_count smallint NOT NULL DEFAULT 1,
  created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE alert_deliveries ENABLE ROW LEVEL SECURITY;
CREATE POLICY alert_deliveries_select ON alert_deliveries FOR SELECT TO authenticated USING (true);

CREATE TABLE failed_reactor_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  signal_id text,
  payload jsonb NOT NULL,
  error_message text NOT NULL,
  attempt_count smallint NOT NULL DEFAULT 1,
  last_attempted_at timestamptz NOT NULL DEFAULT now(),
  resolved_at timestamptz
);
ALTER TABLE failed_reactor_events ENABLE ROW LEVEL SECURITY;
-- service_role only; no authenticated policies.

-- gate_rejections intentionally not created — the /candidate-gate edge function
-- is deleted (spec §6.3). Thesis failures now land in thesis_drafting_failures.

CREATE TABLE thesis_jobs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  signal_id text NOT NULL REFERENCES signals(signal_id),
  alert_id uuid REFERENCES alerts(id),
  status text NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued','drafting','gate_failed_retrying','promoted','dlq')),
  attempt_count smallint NOT NULL DEFAULT 0,
  routine_run_ids text[] NOT NULL DEFAULT '{}',
  drafted_thesis jsonb,
  gate_reasons text[],
  candidate_id uuid REFERENCES candidates(id),
  started_at timestamptz,
  completed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (signal_id)
);
CREATE INDEX thesis_jobs_status_idx ON thesis_jobs(status, created_at);
CREATE TRIGGER thesis_jobs_updated BEFORE UPDATE ON thesis_jobs FOR EACH ROW EXECUTE FUNCTION set_updated_at();
ALTER TABLE thesis_jobs ENABLE ROW LEVEL SECURITY;
CREATE POLICY thesis_jobs_select ON thesis_jobs FOR SELECT TO authenticated USING (true);

CREATE TABLE thesis_drafting_failures (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  thesis_job_id uuid NOT NULL REFERENCES thesis_jobs(id),
  signal_id text NOT NULL REFERENCES signals(signal_id),
  final_reasons text[] NOT NULL,
  all_drafts jsonb NOT NULL,
  alerted boolean NOT NULL DEFAULT true,
  resolved_at timestamptz,
  resolved_candidate_id uuid REFERENCES candidates(id),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX thesis_drafting_failures_unresolved_idx
  ON thesis_drafting_failures(created_at DESC) WHERE resolved_at IS NULL;
ALTER TABLE thesis_drafting_failures ENABLE ROW LEVEL SECURITY;
CREATE POLICY thesis_drafting_failures_select
  ON thesis_drafting_failures FOR SELECT TO authenticated USING (true);

-- ============================================================
-- Human layer
-- ============================================================

CREATE TABLE watchlists (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  name text NOT NULL,
  filter jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX watchlists_user_idx ON watchlists(user_id);
CREATE TRIGGER watchlists_updated BEFORE UPDATE ON watchlists FOR EACH ROW EXECUTE FUNCTION set_updated_at();
ALTER TABLE watchlists ENABLE ROW LEVEL SECURITY;
CREATE POLICY watchlists_user_rw ON watchlists FOR ALL TO authenticated
  USING (user_id = auth.uid()) WITH CHECK (user_id = auth.uid());

CREATE TABLE notifications_prefs (
  user_id uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  email_on_immediate boolean NOT NULL DEFAULT true,
  email_weekly_report boolean NOT NULL DEFAULT true,
  realtime_channels text[] NOT NULL DEFAULT '{signals,alerts}',
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER notifications_prefs_updated BEFORE UPDATE ON notifications_prefs FOR EACH ROW EXECUTE FUNCTION set_updated_at();
ALTER TABLE notifications_prefs ENABLE ROW LEVEL SECURITY;
CREATE POLICY notifications_prefs_user_rw ON notifications_prefs FOR ALL TO authenticated
  USING (user_id = auth.uid()) WITH CHECK (user_id = auth.uid());

CREATE TABLE annotations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  candidate_id uuid NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
  body text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX annotations_user_candidate_idx ON annotations(user_id, candidate_id);
CREATE TRIGGER annotations_updated BEFORE UPDATE ON annotations FOR EACH ROW EXECUTE FUNCTION set_updated_at();
ALTER TABLE annotations ENABLE ROW LEVEL SECURITY;
CREATE POLICY annotations_user_rw ON annotations FOR ALL TO authenticated
  USING (user_id = auth.uid()) WITH CHECK (user_id = auth.uid());

CREATE TABLE candidate_rationales (
  ticker text PRIMARY KEY,
  one_liner text NOT NULL,
  hypothesis text NOT NULL,
  thesis text NOT NULL,
  expected_outcome text NOT NULL,
  price_targets jsonb NOT NULL,
  time_sensitivity text NOT NULL,
  kill_watch text NOT NULL,
  catalyst_date_iso date,
  archived boolean NOT NULL DEFAULT false,
  archived_meta jsonb,
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER candidate_rationales_updated BEFORE UPDATE ON candidate_rationales FOR EACH ROW EXECUTE FUNCTION set_updated_at();
ALTER TABLE candidate_rationales ENABLE ROW LEVEL SECURITY;
CREATE POLICY candidate_rationales_select ON candidate_rationales FOR SELECT TO authenticated USING (true);
