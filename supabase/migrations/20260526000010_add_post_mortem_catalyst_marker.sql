-- 20260526000010_add_post_mortem_catalyst_marker.sql
-- Wave 4.3 — add `catalyst_resolution_marker` to post_mortem_queue.
--
-- Why: Stage 10 stub-inserts into post_mortem_queue when an assessment
-- persists. The outcome_window_end is derived from either an asset's pending
-- FDA event row (PDUFA / AdComm) or a +60d fallback. Today the source is
-- opaque — the nightly_calibration_refit script cannot distinguish a stub
-- anchored to a real catalyst (high signal) from a stub built on the +60d
-- default (low signal, often arbitrary).
--
-- The marker is a tagged text:
--   'fda_event:<uuid>'        — outcome_window_end derived from this event
--   'default_60d_fallback'    — no pending event; +60d default applied
--
-- Future markers may include 'operator_set:<note>' or 'adcom:<uuid>'.
-- Free-text on purpose so the refit script's filter logic owns the schema.
--
-- Backfill: existing rows get NULL (treated as "unknown source" — the refit
-- already had to handle these implicitly; nothing changes downstream until
-- the script is updated to filter on this column.

ALTER TABLE public.post_mortem_queue
  ADD COLUMN IF NOT EXISTS catalyst_resolution_marker text;

COMMENT ON COLUMN public.post_mortem_queue.catalyst_resolution_marker IS
  'How outcome_window_end was derived. Examples: "fda_event:<uuid>" (pending '
  'PDUFA/AdComm row), "default_60d_fallback" (no event found). NULL on rows '
  'inserted before 2026-05-26 (Wave 4.3). Used by nightly_calibration_refit '
  'to weight stubs by catalyst-anchored vs default-window provenance.';
