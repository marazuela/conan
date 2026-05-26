-- Investment workflow hardening: evidence status, claim ledger, EV ranking,
-- and alert-suppression audit surface.

ALTER TABLE public.extracted_facts
  ADD COLUMN IF NOT EXISTS quote_verified boolean,
  ADD COLUMN IF NOT EXISTS quote_verification_status text,
  ADD COLUMN IF NOT EXISTS quote_verification_detail text,
  ADD COLUMN IF NOT EXISTS quote_verified_at timestamptz;

COMMENT ON COLUMN public.extracted_facts.quote_verified IS
  'Deterministic verification that evidence_quote appears verbatim in the text shown to the extractor.';

ALTER TABLE public.convergence_assessments
  ADD COLUMN IF NOT EXISTS claim_ledger jsonb,
  ADD COLUMN IF NOT EXISTS alert_gate_status text,
  ADD COLUMN IF NOT EXISTS alert_gate_reasons text[],
  ADD COLUMN IF NOT EXISTS top_model_review jsonb;

ALTER TABLE public.convergence_assessments
  DROP CONSTRAINT IF EXISTS convergence_assessments_alert_gate_status_check,
  ADD CONSTRAINT convergence_assessments_alert_gate_status_check
  CHECK (
    alert_gate_status IS NULL
    OR alert_gate_status IN ('pass', 'suppress', 'not_evaluated')
  );

COMMENT ON COLUMN public.convergence_assessments.claim_ledger IS
  'Structured claim ledger derived from Stage 1/2/9 outputs. Claims carry supporting/contradicting fact ids and verifier status.';

COMMENT ON COLUMN public.convergence_assessments.alert_gate_status IS
  'Fail-closed alert gate outcome. pass means eligible for attention; suppress means reasons are recorded in alert_gate_reasons.';

COMMENT ON COLUMN public.convergence_assessments.top_model_review IS
  'Optional top-model final challenge/review payload for high-EV Tier-1 cases. NULL unless explicitly invoked.';

CREATE TABLE IF NOT EXISTS public.assessment_alert_suppressions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  assessment_id uuid NOT NULL REFERENCES public.convergence_assessments(id) ON DELETE CASCADE,
  target text,
  reason text NOT NULL,
  evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS assessment_alert_suppressions_assessment_idx
  ON public.assessment_alert_suppressions (assessment_id, created_at DESC);

CREATE INDEX IF NOT EXISTS assessment_alert_suppressions_reason_idx
  ON public.assessment_alert_suppressions (reason, created_at DESC);

ALTER TABLE public.assessment_alert_suppressions ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS assessment_alert_suppressions_select
  ON public.assessment_alert_suppressions;

CREATE POLICY assessment_alert_suppressions_select
  ON public.assessment_alert_suppressions
  FOR SELECT TO authenticated USING (true);

COMMENT ON TABLE public.assessment_alert_suppressions IS
  'Suppression ledger for immediate-band convergence assessments that failed a recipient or global alert gate.';

CREATE OR REPLACE VIEW public.v_fda_evidence_supply_chain_status
WITH (security_invoker = true) AS
WITH material_docs AS (
  SELECT
    asset_id,
    count(*) FILTER (
      WHERE is_material IS TRUE
        AND link_type IN ('primary', 'safety_signal')
    ) AS material_primary_docs,
    count(*) FILTER (
      WHERE is_material IS TRUE
        AND link_type IN ('primary', 'safety_signal')
        AND extraction_confidence < 0.80
    ) AS low_confidence_material_docs,
    max(created_at) AS latest_asset_document_at
  FROM public.asset_documents
  GROUP BY asset_id
),
facts AS (
  SELECT
    asset_id,
    count(*) AS extracted_fact_count,
    count(*) FILTER (WHERE quote_verified IS FALSE) AS quote_verification_failures,
    count(*) FILTER (WHERE quote_verified IS DISTINCT FROM TRUE) AS quote_unverified_or_legacy,
    max(extracted_at) AS latest_fact_at
  FROM public.extracted_facts
  GROUP BY asset_id
),
queue AS (
  SELECT
    asset_id,
    count(*) AS linker_queue_edges
  FROM public.v_asset_linker_skill_queue
  GROUP BY asset_id
)
SELECT
  a.id AS asset_id,
  a.ticker,
  a.drug_name,
  a.sponsor_name,
  a.indication,
  a.watch_priority,
  a.is_active,
  coalesce(md.material_primary_docs, 0) AS material_primary_docs,
  coalesce(md.low_confidence_material_docs, 0) AS low_confidence_material_docs,
  coalesce(f.extracted_fact_count, 0) AS extracted_fact_count,
  coalesce(f.quote_verification_failures, 0) AS quote_verification_failures,
  coalesce(f.quote_unverified_or_legacy, 0) AS quote_unverified_or_legacy,
  coalesce(q.linker_queue_edges, 0) AS linker_queue_edges,
  md.latest_asset_document_at,
  f.latest_fact_at,
  CASE
    WHEN a.ticker IS NULL OR a.drug_name IS NULL THEN 'identity_incomplete'
    WHEN coalesce(md.material_primary_docs, 0) = 0 THEN 'missing_material_primary_document'
    WHEN coalesce(f.extracted_fact_count, 0) = 0 THEN 'missing_extracted_facts'
    WHEN coalesce(f.quote_verification_failures, 0) > 0 THEN 'quote_verification_failed'
    WHEN coalesce(f.quote_unverified_or_legacy, 0) > 0 THEN 'legacy_unverified_quotes'
    ELSE 'tier1_ready'
  END AS evidence_status,
  (
    a.ticker IS NOT NULL
    AND a.drug_name IS NOT NULL
    AND coalesce(md.material_primary_docs, 0) > 0
    AND coalesce(f.extracted_fact_count, 0) > 0
    AND coalesce(f.quote_verification_failures, 0) = 0
  ) AS tier1_ready
FROM public.fda_assets a
LEFT JOIN material_docs md ON md.asset_id = a.id
LEFT JOIN facts f ON f.asset_id = a.id
LEFT JOIN queue q ON q.asset_id = a.id
WHERE a.is_active = true;

COMMENT ON VIEW public.v_fda_evidence_supply_chain_status IS
  'One row per active FDA asset showing local-skill queue backlog, material document coverage, extracted fact coverage, and quote-verification readiness for Tier-1.';

CREATE OR REPLACE VIEW public.v_fda_ranked_opportunities
WITH (security_invoker = true) AS
SELECT
    asset_id,
    ticker,
    mic,
    entity_id,
    drug_name,
    generic_name,
    application_number,
    application_type,
    indication,
    indication_normalized,
    mechanism,
    sponsor_name,
    program_status,
    is_active,
    watch_priority,
    reference_class_signature,
    next_event_id,
    next_event_type,
    next_event_date,
    days_to_next_catalyst,
    latest_assessment_id,
    tier,
    band,
    thesis_direction,
    thesis_summary,
    conviction_pct_calibrated,
    conviction_pct,
    ensemble_dispersion,
    expected_value_bps,
    constitutional_pass,
    latest_assessment_at,
    hours_since_assessment,
    latest_run_id,
    latest_run_status,
    latest_run_tier,
    latest_run_trigger,
    latest_run_started_at,
    latest_run_completed_at,
    latest_run_cost_usd,
    latest_run_latency_ms,
    latest_run_error,
    runs_30d_count,
    cost_30d_usd,
    runs_30d_failed,
    CASE
      WHEN expected_value_bps IS NOT NULL AND expected_value_bps > 0
        THEN expected_value_bps
      ELSE NULL
    END AS ranking_score,
    CASE
        WHEN latest_assessment_id IS NOT NULL
          AND expected_value_bps IS NOT NULL
          AND expected_value_bps <= 0
          THEN 'low_or_negative_ev'
        WHEN latest_assessment_id IS NOT NULL
          AND latest_assessment_at < now() - interval '7 days'
          THEN 'stale_assessment'
        WHEN latest_assessment_id IS NOT NULL
          AND expected_value_bps IS NULL
          THEN 'missing_market_ev'
        WHEN latest_assessment_id IS NOT NULL
          THEN 'assessed'
        WHEN latest_run_status in ('pending', 'running')
          THEN 'assessment_pending'
        WHEN latest_run_status = 'declined'
          THEN 'pregate_declined'
        WHEN latest_run_status in ('failed', 'failed_constitutional')
          THEN 'assessment_failed'
        WHEN latest_run_status in ('skipped_dedupe', 'skipped_budget', 'killed_budget')
          THEN latest_run_status
        ELSE 'unassessed'
    END AS ranking_status,
    (
      latest_assessment_id IS NOT NULL
      AND expected_value_bps IS NOT NULL
      AND expected_value_bps > 0
    ) AS is_rankable,
    'v3_expected_value'::text AS score_source,
    candidate_id AS legacy_candidate_id,
    candidate_state AS legacy_candidate_state,
    candidate_band AS legacy_candidate_band,
    candidate_score AS legacy_candidate_score,
    thesis_approved_at AS legacy_thesis_approved_at
FROM public.v_latest_assessments_by_asset
WHERE is_active = true;

COMMENT ON VIEW public.v_fda_ranked_opportunities IS
  'Canonical FDA opportunity ranking surface. ranking_score is expected_value_bps, not conviction, so high-conviction but fully-priced cases do not outrank actionable mispricings.';
