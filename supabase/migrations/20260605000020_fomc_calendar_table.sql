-- =============================================================================
-- Phase 3a — fomc_calendar table
--
-- Q1 confounder audit (WI-5) flags FDA events that landed on or ±1 calendar
-- day from a scheduled FOMC announcement (the FOMC overnight reaction shifts
-- the whole market, contaminating the post-event return for any same-day
-- catalyst). FOMC meetings are infrequent (8/year + minutes) so the table
-- stays small; one-time backfill from federalreserve.gov 2018-present
-- (~64 rows) covers our calibration training pool.
--
-- Daily refresh isn't needed (Fed releases the next year's schedule once
-- annually); a monthly pg_cron job picks up unscheduled emergency meetings
-- and rare schedule changes.
--
-- See plan: /Users/Pico/.claude/plans/plan-it-thoroughly-unified-scroll.md
-- (Phase 3a calendars)
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.fomc_calendar (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  fomc_date             date NOT NULL,
  statement_release_at  timestamptz,
  meeting_type          text NOT NULL CHECK (meeting_type IN ('scheduled','emergency','minutes')) DEFAULT 'scheduled',
  source                text NOT NULL CHECK (source IN ('federalreserve_gov','fred','manual')),
  source_url            text,
  fetched_at            timestamptz NOT NULL DEFAULT now(),
  UNIQUE (fomc_date, meeting_type)
);

CREATE INDEX IF NOT EXISTS fomc_calendar_date_idx
  ON public.fomc_calendar (fomc_date);

COMMENT ON TABLE public.fomc_calendar IS
  'FOMC meeting dates (scheduled + emergency + minutes-release). Q1 audit reads this to flag FDA events landing on or ±1 day from a FOMC date. Source-of-truth scrape: federalreserve.gov/monetarypolicy/fomccalendars.htm.';

COMMENT ON COLUMN public.fomc_calendar.meeting_type IS
  'scheduled = one of the 8 FOMC announcements per year. emergency = unscheduled rate action (e.g. 2020-03-15). minutes = release date of meeting minutes (typically 3 weeks after the meeting; less market-moving but still flagged for audit thoroughness).';

COMMENT ON COLUMN public.fomc_calendar.statement_release_at IS
  'Approximate UTC timestamp of the statement release (typically 14:00 ET on the second day of a two-day meeting). Optional — fill when we want intraday confounder math.';
