-- =============================================================================
-- FOMC emergency-meeting backfill
--
-- `modal_workers/fetchers/universe/fomc_calendar.py` classifies meetings on
-- the federalreserve.gov calendar by date-range presence: a date range like
-- "March 17-18" maps to 'scheduled', a bare date maps to 'minutes'. Single-
-- day emergency rate actions (e.g. 2020-03-15) have no range and therefore
-- misclassify as 'minutes'. The Q1 confounder audit excludes 'minutes' from
-- its FOMC-day check (`audit_event_data_quality._load_fomc_dates` filters
-- to `meeting_type IN ('scheduled','emergency')`), so an FDA event landing
-- on or ±1 day from a misclassified emergency goes unflagged.
--
-- Parser fix is fragile (the Fed HTML doesn't label emergencies, so any
-- detection rule would require an external date allowlist anyway). This
-- migration manually seeds the canonical post-2018 emergencies covered by
-- the calibration window described in 20260605000020_fomc_calendar_table.sql.
--
-- Idempotent: ON CONFLICT DO NOTHING preserves any operator-entered rows
-- with the same (fomc_date, meeting_type) key. Re-running is safe.
-- =============================================================================

INSERT INTO public.fomc_calendar
  (fomc_date, statement_release_at, meeting_type, source, source_url)
VALUES
  -- COVID emergency cut #1: 50bp inter-meeting move, single-day Tuesday
  -- press release; no scheduled meeting that week.
  ('2020-03-03', '2020-03-03T15:00:00Z', 'emergency', 'manual',
   'https://www.federalreserve.gov/newsevents/pressreleases/monetary20200303a.htm'),

  -- COVID emergency cut #2: 100bp inter-meeting move + QE5 announcement,
  -- Sunday afternoon. Cancelled the regularly-scheduled March 17-18 meeting.
  ('2020-03-15', '2020-03-15T21:00:00Z', 'emergency', 'manual',
   'https://www.federalreserve.gov/newsevents/pressreleases/monetary20200315a.htm'),

  -- COVID open-ended QE expansion: not a rate move, but an unscheduled
  -- BoG/FOMC announcement that materially moved Treasuries + equities.
  -- Included so the Q1 confounder catches FDA events landing on/around it.
  ('2020-03-23', '2020-03-23T12:00:00Z', 'emergency', 'manual',
   'https://www.federalreserve.gov/newsevents/pressreleases/monetary20200323a.htm')
ON CONFLICT (fomc_date, meeting_type) DO NOTHING;

-- Defensive cleanup: any prior cron run may have inserted these dates as
-- 'minutes' (the parser's misclassification). Remove those stale rows so
-- the emergency rows above are the only entries for these dates.
DELETE FROM public.fomc_calendar
 WHERE fomc_date IN (DATE '2020-03-03', DATE '2020-03-15', DATE '2020-03-23')
   AND meeting_type = 'minutes';
