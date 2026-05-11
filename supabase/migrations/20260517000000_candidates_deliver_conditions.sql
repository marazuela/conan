-- P1 #7: add candidates.deliver_conditions JSONB column for upside-resolution
-- observables, mirroring the existing kill_conditions structure.
--
-- Per-element shape (matches kill_conditions):
--   {
--     "id": "D1",
--     "description": "≥40 chars",
--     "observable": {"source_type": "...", "search_pattern": "..."},
--     "date_bound": "YYYY-MM-DD" | null,
--     "status": "pending" | "triggered" | "cleared"
--   }
--
-- Backfill: existing rows get '[]' default. Old candidates degrade gracefully
-- to the price-implied delivery rule in pre_edge_monitor (P0 #3) until they
-- are re-drafted by thesis_writer (P1 #8) with a delivers array populated.

ALTER TABLE public.candidates
  ADD COLUMN IF NOT EXISTS deliver_conditions jsonb NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE public.candidates
  DROP CONSTRAINT IF EXISTS candidates_deliver_conditions_is_array;

ALTER TABLE public.candidates
  ADD CONSTRAINT candidates_deliver_conditions_is_array
  CHECK (jsonb_typeof(deliver_conditions) = 'array');

COMMENT ON COLUMN public.candidates.deliver_conditions IS
  'Symmetric counterpart to kill_conditions. Each entry encodes an upside-resolution observable (FDA approval letter, deal close 8-K, regulatory clearance, etc.) that, when triggered, flips the candidate to state=delivered. Same JSONB shape as kill_conditions: {id, description, observable.{source_type,search_pattern}, date_bound, status}.';
