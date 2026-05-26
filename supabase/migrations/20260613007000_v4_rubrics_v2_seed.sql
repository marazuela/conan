-- v4 Phase 5: rubrics v2 seed.
--
-- Supersedes the v1 rubric rows (all profiles) and inserts v2 rows. The
-- only material change in v2 is on the binary_catalyst profile, which
-- gains two new dimensions sourced from the Phase 4 scanner work:
--
--   insider_pressure     (weight 1.0) — Form 4 cluster reroute
--                                       (insider_form4_scanner.py)
--   shareholder_structure (weight 0.5) — 13D/13G filings
--                                       (edgar_13d_13g_scanner.py)
--
-- Both new dims emit unscored from the scanners — the AI resolver fills
-- the dim values from raw_payload + asset context before scoring lands.
-- Same pattern as fda_event signals (UNSCORED_PROFILES).
--
-- The other five profiles in v2 carry byte-identical weights to v1. They
-- ship as new rows so signals.rubric_version_id can pin to a single
-- version (v2) for the entire active rubric set, instead of stitching
-- across versions per profile.
--
-- Preservation covenant (rubric_engine.py:1-25):
--   1. v1 rubrics are NOT mutated — they get superseded_at stamped and
--      stay queryable for backfill / historical scoring.
--   2. WEIGHTS dict in rubric_engine.py is bumped in lockstep
--      (RUBRIC_VERSION = 2; new dims added to binary_catalyst only).
--   3. dimension_weights JSON values match the Python dict byte-for-byte.

BEGIN;

-- 1. Supersede the v1 rubric rows (idempotent if migration re-runs:
--    superseded_at IS NULL is already false on a second apply).
UPDATE public.rubrics
SET superseded_at = now()
WHERE rubric_version = 1
  AND superseded_at IS NULL;

-- 2. Insert v2 rows. Six profiles. Idempotent via the UNIQUE (profile,
--    rubric_version) constraint — ON CONFLICT DO NOTHING.

INSERT INTO public.rubrics (profile, rubric_version, dimension_weights, notes)
VALUES
  ('merger_arb', 2, jsonb_build_object(
      'spread_size', 3.0,
      'deal_certainty', 2.5,
      'annualized_return', 2.0,
      'break_risk', 1.5,
      'liquidity', 1.0
   ), 'v4 Phase 5: weights unchanged from v1; bumped for unified active set.'),

  ('activist_governance', 2, jsonb_build_object(
      'signal_strength', 2.0,
      'information_asymmetry', 2.0,
      'activist_track_record', 1.5,
      'risk_reward', 1.5,
      'catalyst_clarity', 1.0,
      'edge_decay', 1.0,
      'liquidity', 1.0
   ), 'v4 Phase 5: weights unchanged from v1; bumped for unified active set.'),

  ('binary_catalyst', 2, jsonb_build_object(
      'approval_probability', 2.5,
      'market_mispricing', 2.5,
      'magnitude', 1.5,
      'competitive_landscape', 1.5,
      'catalyst_timeline', 1.0,
      'liquidity', 1.0,
      'insider_pressure', 1.0,
      'shareholder_structure', 0.5
   ), 'v4 Phase 5: adds insider_pressure (Form 4 cluster reroute) and shareholder_structure (13D/13G scanner) dims. Other six dims unchanged from v1.'),

  ('short_positioning', 2, jsonb_build_object(
      'crowding_intensity', 2.5,
      'trend_direction', 2.0,
      'catalyst_proximity', 2.0,
      'size_vs_float', 1.5,
      'historical_analog', 1.0,
      'liquidity', 1.0
   ), 'v4 Phase 5: weights unchanged from v1; bumped for unified active set.'),

  ('litigation', 2, jsonb_build_object(
      'financial_materiality', 3.0,
      'legal_outcome_probability', 2.0,
      'market_pricing', 2.0,
      'resolution_timeline', 1.5,
      'liquidity', 1.0,
      'party_resolution_confidence', 0.5
   ), 'v4 Phase 5: weights unchanged from v1; bumped for unified active set.'),

  ('takeover_candidate', 2, jsonb_build_object(
      'setup_strength', 3.0,
      'edge_freshness', 2.0,
      'valuation_cushion', 2.0,
      'strategic_buyer_clarity', 2.0,
      'liquidity', 1.0
   ), 'v4 Phase 5: weights unchanged from v1; bumped for unified active set.')
ON CONFLICT (profile, rubric_version) DO NOTHING;

COMMIT;
