-- =============================================================================
-- WI-1: thesis_writer 6-field discipline gate — shadow-mode flag
--
-- Seeds a boolean toggle (stored as text 'true'/'false' per internal_config
-- convention) for the external thesis_writer §6.7 discipline gate.
--
-- Default = 'false'. The local repo currently contains the pure validator and
-- tests; the caller that reads this flag lives in the thesis_writer/Cowork
-- routine, not in this migration. Flip to 'true' only after that caller has
-- shadow-logged the 6 required v2 fields (variant_perception, preconditions,
-- kill_criteria, return_distribution, time_horizon, sizing_inputs) and FP rate
-- is <5%.
--
-- Promotion path when enabled = 'true':
--   - field totally absent  -> no retry, route to §8c-flagged with
--     gate_reasons += ['discipline_missing_<field>']
--   - field present but below min_chars -> ONE retry via §8b
--
-- See plan: /Users/Pico/.claude/plans/plan-it-thoroughly-unified-scroll.md (WI-1)
--
-- Idempotent: ON CONFLICT (key) DO NOTHING.
-- =============================================================================
INSERT INTO public.internal_config (key, value, updated_at)
VALUES ('discipline_gate_enabled', 'false', now())
ON CONFLICT (key) DO NOTHING;
