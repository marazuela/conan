-- Conan v3 — register anthropic_files_backfill scanner.
--
-- Context:
--   DocumentWriter now uploads eligible documents to Anthropic's Files API at
--   ingest time (size-gated by MIN_UPLOAD_BYTES). This scanner is the daily
--   safety net: it drains any row that has anthropic_file_id IS NULL despite
--   meeting the size gate — covering pre-existing backlog, transient API
--   failures, and ingest paths that didn't opt in.
--
--   Signal-less; scan() returns ScannerResult(signals=[]) and only writes
--   anthropic_file_id back to documents. default_scoring_profile is required
--   NOT NULL so we set 'binary_catalyst' (matches the FDA-family default) but
--   no signals ever flow through it.
--
--   Scheduled at 17 UTC — the dispatch_release_times cron only fires at
--   06/08/13/17/21 UTC and 17 is the lightest bucket (only congressional_trading
--   runs there). By 17 UTC the EU+APAC+US-morning ingest sweeps have all
--   completed, giving the at-ingest upload path a full window to handle that
--   day's new rows before the backfill catches stragglers.
--
--   Daily cap = 500 (enforced in scanner code, env-overridable). Drains ~11k
--   backlog over ~3 weeks at predictable cost.
--
-- Safe to re-run: ON CONFLICT (name) DO NOTHING.

INSERT INTO public.scanners (
  name,
  tool_path,
  status,
  geography,
  cadence,
  scheduled_hour_utc,
  default_scoring_profile,
  signal_type_profile_map,
  endpoints,
  timeout_soft_s,
  timeout_hard_s,
  config
) VALUES (
  'anthropic_files_backfill',
  'modal_workers/scanners/anthropic_files_backfill.py',
  'operational',
  'US',
  'daily',
  17,
  'binary_catalyst',
  '{}'::jsonb,
  jsonb_build_object(
    'anthropic_files', 'https://api.anthropic.com/v1/files'
  ),
  600,
  1200,
  jsonb_build_object(
    'daily_limit',           500,
    'min_upload_bytes',      20000,
    'emits_signals',         false,
    'purpose',               'Drain documents.anthropic_file_id IS NULL backlog into Anthropic Files API',
    'env_overrides',         jsonb_build_array(
      'ANTHROPIC_FILES_BACKFILL_DAILY_LIMIT',
      'ANTHROPIC_FILES_MIN_UPLOAD_BYTES'
    )
  )
)
ON CONFLICT (name) DO NOTHING;
