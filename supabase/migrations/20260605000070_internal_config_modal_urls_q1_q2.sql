-- =============================================================================
-- Phase 3a / 3b — Modal endpoint URL placeholders in internal_config
--
-- The earnings + FOMC fetchers + Q1/Q2 audit scripts run on Modal. pg_cron
-- jobs in 20260605000050/000060 (calendar refreshers) and the Q1/Q2 wrappers
-- read the endpoint URL from internal_config at job-run time so we can deploy
-- the Modal functions first, fill these rows, and the schedulers pick them up
-- without further DB migrations.
--
-- Until each URL is populated, the corresponding pg_cron job is effectively
-- a no-op (the scheduler exits early when the URL is empty — see the cron
-- migrations for the guard).
--
-- See plan: /Users/Pico/.claude/plans/plan-it-thoroughly-unified-scroll.md
-- (Phase 3a calendars + Phase 3b Q1/Q2)
-- =============================================================================

INSERT INTO public.internal_config (key, value, updated_at) VALUES
  ('modal_url_earnings_calendar_fetch_daily', '', now()),
  ('modal_url_fomc_calendar_refresh', '', now()),
  ('modal_url_q1_audit_run', '', now()),
  ('modal_url_q2_audit_run', '', now())
ON CONFLICT (key) DO NOTHING;
