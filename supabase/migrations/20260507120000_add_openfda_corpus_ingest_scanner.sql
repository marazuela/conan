-- Conan v3 — register openfda_corpus_ingest scanner.
--
-- Context:
--   The v3 RAG ingest path (modal_workers/ingestion/openfda_ingest.py) was not
--   wired to any schedule until this migration. It now feeds drugsfda + dailymed
--   labels into the documents table for sub-agent retrieval. This row registers
--   it as a daily scanner at 06 UTC; dispatch_release_times will spawn
--   openfda_corpus_ingest_once at that hour.
--
--   The scanner is signal-less — its scan() returns ScannerResult(signals=[])
--   and only writes to documents. default_scoring_profile is required NOT NULL
--   so we set 'binary_catalyst' (matches the FDA-family default) but no signals
--   ever flow through it.
--
--   Sunday auto-deep: scanner code switches to a 180d sweep when
--   datetime.utcnow().weekday() == 6, folding the weekly backfill catch-up
--   into the daily 06 UTC slot so we don't need a second registry row.
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
  'openfda_corpus_ingest',
  'modal_workers/scanners/openfda_corpus_ingest.py',
  'operational',
  'US',
  'daily',
  6,
  'binary_catalyst',
  '{}'::jsonb,
  jsonb_build_object(
    'openfda_drugsfda', 'https://api.fda.gov/drug/drugsfda.json',
    'openfda_label',    'https://api.fda.gov/drug/label.json'
  ),
  600,
  900,
  jsonb_build_object(
    'shallow_lookback_days', 30,
    'deep_lookback_days',    180,
    'deep_trigger',          'weekday=6 (Sunday) or env OPENFDA_INGEST_MODE=deep',
    'page_limit',            100,
    'max_pages_hard_cap',    100,
    'emits_signals',         false,
    'purpose',               'v3 RAG corpus ingest (drugsfda + dailymed labels)'
  )
)
ON CONFLICT (name) DO NOTHING;
