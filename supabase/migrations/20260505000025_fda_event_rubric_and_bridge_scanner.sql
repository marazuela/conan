-- Conan v2 — register the new fda_event scoring profile and the bridge scanner.
--
-- Phase 3 of the FDA Event-Investing Cockpit V1 plan. Two registry rows:
--
--   rubrics row (profile='fda_event', rubric_version=1)
--     The dimension_weights mirror the deterministic compose_features() math
--     in modal_workers/scanners/fda_event_features.py — six dimensions
--     totaling 10 weight units (multiplied by 5 maximum per-dim score = 50).
--
--   scanners row (name='fda_signal_bridge', status='shadow')
--     Initial status is 'shadow' so the bridge writes only shadow_* columns
--     on fda_event_features and does NOT emit signals rows. The Phase 6
--     cutover sequence flips this to 'shadow_with_emit' then 'operational'.
--
-- Idempotent: ON CONFLICT DO NOTHING on both inserts.

INSERT INTO public.rubrics (profile, rubric_version, dimension_weights, notes)
VALUES (
  'fda_event',
  1,
  jsonb_build_object(
    'approval_probability', 2.5,
    'pricing_edge',         2.5,
    'magnitude',            1.5,
    'expected_value',       1.5,
    'catalyst_timeline',    1.0,
    'liquidity',            1.0
  ),
  'FDA Event-Investing Cockpit V1 — EV-driven scoring (FDA-specific). '
  || 'Pricing edge replaces market_mispricing dim; expected_value joins as a '
  || 'separate scored dimension. Non-FDA binary catalysts continue to use '
  || 'profile=binary_catalyst.'
)
ON CONFLICT (profile, rubric_version) DO NOTHING;

INSERT INTO public.scanners (
  name,
  tool_path,
  status,
  geography,
  cadence,
  default_scoring_profile,
  signal_type_profile_map,
  endpoints,
  timeout_soft_s,
  timeout_hard_s,
  config
)
VALUES (
  'fda_signal_bridge',
  'modal_workers/scanners/fda_signal_bridge.py',
  'shadow',
  'US',
  '3h',
  'fda_event',
  jsonb_build_object(
    'pdufa', 'fda_event',
    'adcom', 'fda_event',
    'phase3_readout', 'fda_event',
    'eop2', 'fda_event'
  ),
  jsonb_build_object(
    'polygon', 'https://api.polygon.io',
    'federal_register', 'https://www.federalregister.gov/api/v1/documents'
  ),
  90,
  180,
  jsonb_build_object(
    'mode', 'shadow',
    'block_resolution_events', true,
    'block_immediate_without_market_p', true,
    'notes', 'Phase 3 shadow run. Flip to shadow_with_emit then operational at cutover.'
  )
)
ON CONFLICT (name) DO NOTHING;
