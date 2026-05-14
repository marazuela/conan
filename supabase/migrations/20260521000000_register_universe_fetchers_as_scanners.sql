-- Register the two catalyst_universe fetchers as rows in public.scanners so
-- the F-214 telemetry (`partial_reasons`, `pages_failed[ct] > 0`) flows
-- through the existing scanner_runs / observability path instead of being
-- visible only in per-call Modal logs.
--
-- Background:
--   modal_workers/fetchers/universe/sec_8k_mna.py
--   modal_workers/fetchers/universe/fda_adcomm_pdufa.py
-- run as Modal functions sec_8k_mna_once / fda_adcomm_pdufa_once. The
-- dispatcher (modal_workers/app.py:dispatch_release_times) already de-dupes
-- registry_names + _FETCHERS_AT_HOUR so registering them here won't double-fire.
--
-- scanner_probe iterates status='operational' rows. Both fetchers hit endpoints
-- that are already probed via sibling scanners (edgar_filing_monitor,
-- fda_pdufa_pipeline), so we set config.probe_skip_reason to skip the duplicate.

INSERT INTO public.scanners (name, cadence, status, scheduled_hour_utc,
                             default_scoring_profile, tool_path, config)
VALUES
  ('sec_8k_mna', 'daily', 'operational', 13,
   'merger_arb',
   'modal_workers/fetchers/universe/sec_8k_mna.py',
   '{"probe_skip_reason": "fetcher: duplicate endpoint with edgar_filing_monitor"}'::jsonb),
  ('fda_adcomm_pdufa', 'daily', 'operational', 13,
   'binary_catalyst',
   'modal_workers/fetchers/universe/fda_adcomm_pdufa.py',
   '{"probe_skip_reason": "fetcher: duplicate endpoint with fda_pdufa_pipeline"}'::jsonb)
ON CONFLICT (name) DO NOTHING;
