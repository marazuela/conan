-- Conan v2 — register insider_form4_scanner.
--
-- Context:
--   profile_short_positioning.md:3 committed Form 4 coverage ("once scanner is
--   built") and the Form 4 cluster rubric at lines 47-55 was drafted before any
--   scanner emitted into it.
--
--   This migration inserts the scanner row as status='paused' with
--   scheduled_hour_utc=21 (US post-close — Form 4 filings land in the evening).
--   Pedro flips to 'operational' when ready.
--
-- Safe to re-run: ON CONFLICT (name) DO NOTHING.
--
-- 2026-05-11: removed delaware_chancery_scanner INSERT (CourtConnect surface
-- never implemented; scanner deprecated per D-125 and deleted ahead of Phase 7
-- cleanup because it never ran — no orphan data to manage). Live-DB row is
-- removed by 20260519000000_drop_delaware_chancery_scanner.sql.

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
  'insider_form4_scanner',
  'tools/insider_form4_scanner.py',
  'paused',
  'US',
  'daily',
  21,
  'short_positioning',
  jsonb_build_object(
    'insider_cluster_buy',     'short_positioning',
    'insider_cluster_sell',    'short_positioning',
    'c_suite_open_market_buy', 'short_positioning',
    'ten_percent_holder_buy',  'short_positioning'
  ),
  jsonb_build_object(
    'primary',   'https://efts.sec.gov/LATEST/search-index',
    'secondary', 'https://www.sec.gov/Archives/edgar/data/'
  ),
  120,
  240,
  jsonb_build_object(
    'lookback_days',        14,
    'cluster_window_days',  30,
    'min_net_value_usd',    50000,
    'max_filings_per_run',  500
  )
)
ON CONFLICT (name) DO NOTHING;

-- delaware_chancery_scanner INSERT removed 2026-05-11; see header comment.
