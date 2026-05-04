-- Conan v2 — extend scanners.status to include shadow modes.
--
-- Phase 3 of the FDA Event-Investing Cockpit V1 plan introduces an additional
-- scanner that runs side-by-side with the canonical fda_pdufa_pipeline. Two
-- new statuses gate the cutover sequence:
--
--   shadow            — bridge writes shadow_* columns on fda_event_features
--                       only. No signals row emission. Existing canonical
--                       binary_catalyst flow is unaffected.
--   shadow_with_emit  — Phase 6 interim: bridge writes shadow_* AND emits
--                       canonical fda_event signals rows. Used to confirm
--                       zero divergence before flipping to operational.
--
-- The dispatcher must learn to skip 'shadow' rows when promoting signals into
-- the live signals table — that gate is implemented in the bridge module
-- itself (modal_workers/scanners/fda_signal_bridge.py), not here.

ALTER TABLE public.scanners DROP CONSTRAINT IF EXISTS scanners_status_check;

ALTER TABLE public.scanners ADD CONSTRAINT scanners_status_check
  CHECK (status = ANY (ARRAY[
    'operational'::text,
    'planned'::text,
    'deprecated'::text,
    'experimental'::text,
    'paused'::text,
    'shadow'::text,
    'shadow_with_emit'::text
  ]));
