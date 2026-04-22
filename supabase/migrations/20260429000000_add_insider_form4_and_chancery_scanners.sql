-- Conan v2 — register two new scanners: insider_form4_scanner + delaware_chancery_scanner.
--
-- Context:
--   profile_short_positioning.md:3 committed Form 4 coverage ("once scanner is
--   built") and the Form 4 cluster rubric at lines 47-55 was drafted before any
--   scanner emitted into it. profile_litigation.md:114 called out Delaware
--   Chancery as "especially value-relevant" for fiduciary-duty cases and D-016
--   redesigned the scraping approach, but neither scanner was ever registered.
--
--   This migration inserts both scanner rows as status='paused' with
--   scheduled_hour_utc=21 (US post-close — Form 4 filings land in the evening,
--   Chancery opinions publish on court business days closing Friday afternoon).
--   Pedro flips to 'operational' when ready.
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
  'delaware_chancery_scanner',
  'tools/delaware_chancery_scanner.py',
  'paused',
  'US',
  'daily',
  21,
  'litigation',
  jsonb_build_object(
    'chancery_appraisal_filed',                 'litigation',
    'chancery_books_and_records_demand',        'litigation',
    'chancery_revlon_claim_filed',              'litigation',
    'chancery_motion_to_expedite_granted',      'litigation',
    'chancery_injunction_granted_blocking_deal','litigation',
    'chancery_opinion_released',                'litigation'
  ),
  jsonb_build_object(
    'primary',   'https://courts.delaware.gov/opinions/index.aspx?ag=court%20of%20chancery',
    'secondary', 'https://courts.delaware.gov/help/onlineservices/docketsearch.aspx'
  ),
  90,
  180,
  jsonb_build_object(
    'opinions_lookback_days',   14,
    'chancery_ag_filter',       'court of chancery',
    'degrade_if_courtconnect_blocked', true
  )
)
ON CONFLICT (name) DO NOTHING;
