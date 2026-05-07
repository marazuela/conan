-- Conan v2 — fda_shadow_compare view.
--
-- Joins fda_event_features (shadow_* columns from the new bridge) with the
-- live binary_catalyst signal row for the same regulatory event. Surfaces
-- score/band deltas and immediate-eligibility deltas so the Phase 3 shadow
-- report can answer:
--   - How many events were scored under both pipelines?
--   - Distribution of band changes (what would Immediate become under v2?)
--   - Recall vs catalyst_universe outcomes (did v2 surface events v1 missed?)
--   - Post-edge avoidance (did v2 stop scoring resolution events as new opps?)
--
-- The "live" signal row is the most recent binary_catalyst signal whose
-- raw_payload links to the same (ticker, drug_name, pdufa_date). Until the
-- bridge has its own dedicated linkage column, we match on raw_payload->>'ticker'
-- and raw_payload->>'drug_name' which the existing fda_pdufa_pipeline emits.
--
-- Read-only: SELECT TO authenticated. Service-role and dashboard RPCs see all.

CREATE OR REPLACE VIEW public.fda_shadow_compare AS
WITH bridge AS (
  SELECT
    re.id                                    AS event_id,
    re.event_type,
    re.event_date,
    re.event_status,
    re.asset_id,
    a.ticker,
    a.drug_name,
    a.indication,
    a.application_number,
    fef.snapshot_at                          AS shadow_snapshot_at,
    fef.fair_probability,
    fef.market_implied_probability,
    fef.expected_value_pct                   AS shadow_ev_pct,
    fef.shadow_expected_value_pct,
    fef.pricing_edge                         AS shadow_pricing_edge,
    fef.shadow_pricing_edge,
    fef.shadow_score,
    fef.shadow_band,
    fef.shadow_recorded_at,
    fef.options_liquidity_score,
    fef.implied_move_pct,
    fef.evidence_confidence
  FROM public.fda_regulatory_events re
  JOIN public.fda_assets a ON a.id = re.asset_id
  LEFT JOIN public.fda_event_features fef ON fef.event_id = re.id
),
canonical AS (
  -- Pick the most recent binary_catalyst signal that mentions the same
  -- (ticker, drug_name) under the legacy raw_payload contract.
  SELECT DISTINCT ON (
    s.raw_payload->>'ticker', s.raw_payload->>'drug_name'
  )
    s.raw_payload->>'ticker'    AS ticker,
    s.raw_payload->>'drug_name' AS drug_name,
    s.signal_id,
    s.signal_type,
    s.score                     AS canonical_score,
    s.band                      AS canonical_band,
    s.score_with_bonus          AS canonical_score_with_bonus,
    s.band_with_bonus           AS canonical_band_with_bonus,
    s.scan_date                 AS canonical_scan_date,
    s.thesis_direction          AS canonical_direction
  FROM public.signals s
  WHERE s.scoring_profile = 'binary_catalyst'
    AND s.raw_payload ? 'ticker'
    AND s.raw_payload ? 'drug_name'
  ORDER BY
    s.raw_payload->>'ticker',
    s.raw_payload->>'drug_name',
    s.scan_date DESC
)
SELECT
  b.event_id,
  b.event_type,
  b.event_date,
  b.event_status,
  b.ticker,
  b.drug_name,
  b.indication,
  b.fair_probability,
  b.market_implied_probability,
  b.shadow_score,
  b.shadow_band,
  b.shadow_ev_pct,
  b.shadow_pricing_edge,
  b.shadow_recorded_at,
  c.signal_id          AS canonical_signal_id,
  c.signal_type        AS canonical_signal_type,
  c.canonical_score,
  c.canonical_band,
  c.canonical_score_with_bonus,
  c.canonical_band_with_bonus,
  c.canonical_scan_date,
  c.canonical_direction,
  -- Deltas (NULL when one side hasn't scored)
  (b.shadow_score - c.canonical_score)::numeric(5,2)              AS score_delta,
  CASE
    WHEN b.shadow_band IS NULL OR c.canonical_band IS NULL THEN NULL
    WHEN b.shadow_band::text = c.canonical_band::text THEN 'same'
    ELSE b.shadow_band::text || '<-' || c.canonical_band::text
  END                                                              AS band_change,
  -- Immediate-eligibility deltas
  (b.shadow_band = 'immediate'::signal_band)                       AS shadow_immediate,
  (c.canonical_band_with_bonus = 'immediate'::signal_band)         AS canonical_immediate,
  -- Resolution-event flag (these should never get shadow_band='immediate')
  (b.event_type IN ('approval','crl','presumed_crl','withdrawal')) AS is_resolution_event
FROM bridge b
LEFT JOIN canonical c
  ON c.ticker = b.ticker
 AND c.drug_name = b.drug_name;

COMMENT ON VIEW public.fda_shadow_compare IS
  'Phase 3 shadow comparison: joins fda_event_features (bridge) with the most '
  'recent binary_catalyst signal (canonical) by (ticker, drug_name). Surfaces '
  'score/band deltas, immediate-eligibility deltas, and the is_resolution_event '
  'flag for the post-edge-avoidance metric. Read by '
  'modal_workers/scripts/fda_shadow_report.py.';

-- Views inherit the base table RLS; explicit GRANT to anon/authenticated is
-- not needed because the underlying tables already expose SELECT TO authenticated.
