-- Conan v3 — register fed_register_adcom + edgar_8k_pdufa fetchers.
--
-- Context:
--   `fda_regulatory_events` is the v3 orchestrator's opportunity-event store.
--   Until this migration, the only writer was the one-shot operator script
--   `modal_workers/scripts/fda_backfill_watchlist.py` (reads pdufa_watchlist.json
--   from Supabase Storage). That left the table frozen at 35 rows since
--   2026-05-04, with no organic arrival.
--
--   Two new fetchers close the gap:
--
--     fed_register_adcom — Federal Register API. AdComm meeting notices →
--       fda_regulatory_events(event_type='adcom', event_status='pending',
--       event_date=<parsed meeting date>).
--
--     edgar_8k_pdufa    — EDGAR full-text search. 8-K filings with PDUFA-date
--       phrases → fda_regulatory_events(event_type='pdufa',
--       event_status='pending', event_date=NULL). Date refinement deferred to
--       the medical specialist via fda_agent_reviews.
--
--   Both are lookup-only on `fda_assets` — they emit no events for sponsors /
--   filers absent from the curated asset set. This keeps the watchlist
--   uncontaminated; new assets enter via the watchlist JSON or dashboard, then
--   subsequent fetcher runs pick them up.
--
--   Both run at 13 UTC via `dispatch_release_times` — already wired in
--   `modal_workers/app.py` `_FETCHERS_AT_HOUR[13]`.
--
--   The AFTER INSERT trigger
--   `enqueue_fda_agent_reviews_on_event_insert_tg` (live since 2026-05-11
--   per migration 20260520000000) auto-fans each pending non-resolution event
--   into 3 fda_agent_reviews rows (medical/regulatory/microstructure), which
--   the Cowork specialist crons then drain.
--
-- Safe to re-run: ON CONFLICT (name) DO NOTHING on both inserts.

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
  'fed_register_adcom',
  'modal_workers/fetchers/universe/fed_register_adcom.py',
  'operational',
  'US',
  'daily',
  13,
  'binary_catalyst',
  '{}'::jsonb,
  jsonb_build_object(
    'federal_register_api', 'https://www.federalregister.gov/api/v1/documents.json'
  ),
  120,
  300,
  jsonb_build_object(
    'lookback_days',  30,
    'page_size',      100,
    'page_limit',     50,
    'emits_signals',  false,
    'writes',         'fda_regulatory_events',
    'event_type',     'adcom',
    'asset_resolution', 'lookup_only_via_fda_assets_sponsor_name_or_entities_name',
    'purpose',        'v3 organic AdComm-event arrival for fda_regulatory_events'
  )
)
ON CONFLICT (name) DO NOTHING;

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
  'edgar_8k_pdufa',
  'modal_workers/fetchers/universe/edgar_8k_pdufa.py',
  'operational',
  'US',
  'daily',
  13,
  'binary_catalyst',
  '{}'::jsonb,
  jsonb_build_object(
    'edgar_search', 'https://efts.sec.gov/LATEST/search-index'
  ),
  180,
  300,
  jsonb_build_object(
    'lookback_days',  14,
    'page_size',      100,
    'queries',        jsonb_build_array(
      'PDUFA goal date',
      'PDUFA action date',
      'PDUFA target action'
    ),
    'emits_signals',  false,
    'writes',         'fda_regulatory_events',
    'event_type',     'pdufa',
    'event_date',     null,
    'asset_resolution', 'cik_via_entity_identifiers_then_ticker_via_fda_assets',
    'purpose',        'v3 organic PDUFA-event arrival; date refinement deferred to medical specialist'
  )
)
ON CONFLICT (name) DO NOTHING;
