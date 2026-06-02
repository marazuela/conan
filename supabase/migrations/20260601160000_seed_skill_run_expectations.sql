-- 20260601160000_seed_skill_run_expectations.sql
--
-- Seed public.skill_run_expectations so the heartbeat watchdog
-- (_skill_run_watchdog, pg_cron every 15 min) has a registry to evaluate.
-- The tracker infra (20260613002000_skill_run_tracker.sql) shipped the table,
-- RPCs, view, and watchdog but never seeded the registry and no skill ever
-- called the RPCs, so v_skill_run_health was empty and the watchdog was a
-- no-op.
--
-- STAGED ROLLOUT (important):
--   enabled = true        -> stale_running detection is live immediately. Safe
--                            with zero skill_runs rows (running_count=0 -> 'ok').
--   max_silence = NULL    -> SILENCE detection is intentionally OFF for now.
--                            The CASE in v_skill_run_health only flags 'silent'
--                            when max_silence IS NOT NULL, so leaving it NULL
--                            means (a) no false-silent flood before skills are
--                            instrumented, and (b) no duplication of the
--                            existing skill_watchdog.md side-effect-SLA flags
--                            (kind 'skill_dark:%').
--   Once skill_run_start/finish instrumentation is verified writing rows,
--   set max_silence per skill (~3x expected_interval) to switch silence
--   detection on — and at that point skill_watchdog.md's overlapping
--   'skill_dark:' checks can be retired or downgraded.
--
-- expected_interval documents the intended cadence (informational until
-- max_silence is set). stale_running_after bounds a single run before it is
-- considered stuck (these skills are quota-bounded and finish in minutes;
-- weekly retrospectives get a longer budget).
--
-- skill_host: 'cowork-mac' = Pedro's Claude.app Cowork host; 'modal' =
-- runs as code inside a Modal function (coverage_auditor inside
-- reporting_weekly).
--
-- Idempotent: ON CONFLICT (skill_name) DO NOTHING so re-applying never
-- clobbers operator hand-tuning of thresholds.

BEGIN;

INSERT INTO public.skill_run_expectations
  (skill_name, skill_host, enabled, expected_interval, max_silence, stale_running_after, severity, notes)
VALUES
  -- high-frequency, pipeline-critical
  ('signal_resolver',          'cowork-mac', true, interval '10 minutes', NULL, interval '45 minutes', 'critical', 'P0 immediate-band scorer; every 10m'),
  ('thesis_writer',            'cowork-mac', true, interval '1 hour',      NULL, interval '1 hour',     'critical', 'P0 thesis drafter; hourly :00'),
  ('thesis_transcriber',       'cowork-mac', true, interval '1 hour',      NULL, interval '1 hour',     'warn',     'v4 transcription; hourly :15'),
  ('asset_linker_backfill',    'cowork-mac', true, interval '30 minutes',  NULL, interval '1 hour',     'warn',     'doc->asset pass-1 linker; every 30m'),
  ('fact_extractor_opus',      'cowork-mac', true, interval '45 minutes',  NULL, interval '1 hour',     'warn',     'Opus fact extraction; every 30-60m'),
  ('signal_entity_resolver',   'cowork-mac', true, interval '30 minutes',  NULL, interval '1 hour',     'warn',     'entity resolution; every 30m'),
  -- hourly FDA review drainers
  ('fda_medical_review',       'cowork-mac', true, interval '1 hour',      NULL, interval '1 hour',     'warn',     'hourly :15'),
  ('fda_regulatory_review',    'cowork-mac', true, interval '1 hour',      NULL, interval '1 hour',     'warn',     'hourly :30'),
  ('fda_microstructure_review','cowork-mac', true, interval '1 hour',      NULL, interval '1 hour',     'warn',     'hourly :45'),
  -- daily
  ('candidate_aging',          'cowork-mac', true, interval '1 day',       NULL, interval '2 hours',    'warn',     'daily 06:00 UTC'),
  ('fda_aging_review',         'cowork-mac', true, interval '1 day',       NULL, interval '2 hours',    'warn',     'daily 06:00 UTC'),
  -- watchdog (monitor the monitor)
  ('skill_watchdog',           'cowork-mac', true, interval '2 hours',     NULL, interval '1 hour',     'warn',     'side-effect-SLA watchdog; every 2h'),
  -- weekly retrospectives
  ('challenger_retro',         'cowork-mac', true, interval '7 days',      NULL, interval '3 hours',    'info',     'weekly Sun 09:00 UTC'),
  ('fda_challenger_replay',    'cowork-mac', true, interval '7 days',      NULL, interval '3 hours',    'info',     'weekly Sun 09:00 UTC'),
  ('feedback_retrospective',   'cowork-mac', true, interval '7 days',      NULL, interval '3 hours',    'info',     'weekly Sun 20:00 UTC'),
  ('prompt_retrospective',     'cowork-mac', true, interval '7 days',      NULL, interval '1 hour',     'info',     'weekly Sun; no-op unless quarter boundary (finish skipped)'),
  -- code-side (Modal), not a Cowork skill
  ('coverage_auditor',         'modal',      true, interval '7 days',      NULL, interval '1 hour',     'info',     'runs inside reporting_weekly Modal fn (cron 0 12 * * 0); instrument code-side')
ON CONFLICT (skill_name) DO NOTHING;

COMMIT;
