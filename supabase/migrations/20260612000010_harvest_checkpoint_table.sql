-- =============================================================================
-- WI-7 — harvest_checkpoint table for resumable FDA event harvest
--
-- harvest_fda_events.py walks openFDA + EDGAR 8-K date ranges and upserts
-- into fda_regulatory_events. The checkpoint table lets re-runs resume from
-- the last successful cursor without re-fetching what's already there.
--
-- One row per (source, cursor_date). `last_processed_id` is the openFDA
-- application_number (or EDGAR accession) of the last upserted row, used as
-- a tiebreaker within a date when API pages return >page_size results.
--
-- See plan: /Users/Pico/.claude/plans/plan-it-thoroughly-unified-scroll.md
-- (WI-7)
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.harvest_checkpoint (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source            text NOT NULL CHECK (source IN ('openfda','edgar_8k')),
  cursor_date       date NOT NULL,
  last_processed_id text,
  rows_processed    bigint NOT NULL DEFAULT 0,
  last_run_at       timestamptz NOT NULL DEFAULT now(),
  notes             text,
  UNIQUE (source, cursor_date)
);

CREATE INDEX IF NOT EXISTS harvest_checkpoint_source_date_idx
  ON public.harvest_checkpoint (source, cursor_date DESC);

COMMENT ON TABLE public.harvest_checkpoint IS
  'WI-7 — resumable harvest cursors for fda_regulatory_events. One row per (source, day) so a daily pg_cron job can pick up where the prior day left off. Idempotent: re-running on the same day refreshes last_run_at without breaking the cursor.';
