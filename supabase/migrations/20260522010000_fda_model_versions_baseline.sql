-- F-310 / F-315 fix: seed a baseline fda_model_versions row + ditch the
-- hardcoded BAND_THRESHOLDS_DEFAULT / DESIGNATION_MODIFIERS_DEFAULT path.
--
-- Background
-- ----------
-- fda_model_versions has been empty since the table was created. The bridge's
-- documented cutover gate (`shadow → shadow_with_emit → operational`) requires
-- "calibration pass + activated model version", but with no model version row
-- in the table, the gate is structurally bypassable — flipping mode='operational'
-- would emit uncalibrated signals. fda_event_features reads thresholds and
-- modifiers from hardcoded module constants (BAND_THRESHOLDS_DEFAULT,
-- DESIGNATION_MODIFIERS_DEFAULT) every run.
--
-- This migration plants a v1_baseline row containing today's exact in-code
-- defaults. The bridge can now load the active row at scan start and use its
-- thresholds + modifiers instead of constants. Real Phase-6 calibration will
-- supersede this row (set superseded_at, INSERT a new row with effective_at).
--
-- Values verified against the live module on 2026-05-11:
--   BAND_THRESHOLDS_DEFAULT  = {immediate: 30, watchlist: 20, archive: 10}
--   DESIGNATION_MODIFIERS_DEFAULT (fda_event_features.py:64-71):
--     priority_review: +0.05
--     breakthrough:    +0.04
--     accelerated:     +0.03
--     rtor:            +0.02
--     fast_track:      +0.02
--     is_resubmission: -0.10
--   sizing_caps: {} (not yet defined; future)
--   priors_by_indication: {} (already DB-backed via phase3_base_rates; the
--     model_version row leaves this empty as a sentinel for "use the
--     phase3_base_rates table as the prior source").
--
-- Idempotent: INSERT ... ON CONFLICT (version) DO NOTHING.
--
-- Rollback
--   UPDATE public.fda_model_versions SET superseded_at = NOW()
--    WHERE version='v1_baseline_2026_05_11' AND superseded_at IS NULL;

INSERT INTO public.fda_model_versions (
  version, scope,
  priors_by_indication, designation_modifiers, band_thresholds, sizing_caps,
  effective_at, superseded_at, created_by, notes
)
VALUES (
  'v1_baseline_2026_05_11', 'both',
  '{}'::jsonb,
  jsonb_build_object(
    'priority_review',  0.05,
    'breakthrough',     0.04,
    'accelerated',      0.03,
    'rtor',             0.02,
    'fast_track',       0.02,
    'is_resubmission', -0.10
  ),
  jsonb_build_object(
    'immediate', 30.0,
    'watchlist', 20.0,
    'archive',   10.0
  ),
  '{}'::jsonb,
  NOW(), NULL,
  'migration:20260522010000',
  'v1 baseline. Mirrors fda_event_features module constants ' ||
  'BAND_THRESHOLDS_DEFAULT + DESIGNATION_MODIFIERS_DEFAULT as of 2026-05-11. ' ||
  'priors_by_indication is empty because phase3_base_rates table is the prior ' ||
  'source. Supersede by INSERTing a new row with effective_at and SETting ' ||
  'superseded_at on this one. F-310 baseline; not a real calibration run.'
)
ON CONFLICT (version) DO NOTHING;

-- Lightweight helper to fetch the currently active version. Used by the
-- bridge at scan start. Returns NULL if no active row (caller falls back to
-- module constants).
CREATE OR REPLACE FUNCTION public.fda_active_model_version()
RETURNS public.fda_model_versions
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $func$
  SELECT *
    FROM public.fda_model_versions
   WHERE effective_at IS NOT NULL
     AND effective_at <= NOW()
     AND superseded_at IS NULL
   ORDER BY effective_at DESC
   LIMIT 1;
$func$;

COMMENT ON FUNCTION public.fda_active_model_version() IS
  'Return the currently active fda_model_versions row (effective_at <= NOW() '
  'AND superseded_at IS NULL). NULL if none active. Used by fda_signal_bridge '
  'at scan start to load band_thresholds + designation_modifiers from DB.';
