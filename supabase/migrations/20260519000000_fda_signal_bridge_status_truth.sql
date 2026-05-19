-- Conan v2 — F-206: collapse fda_signal_bridge's dual control plane onto
-- scanners.status as the single source of truth, and add a `primary`
-- endpoint so scanner_probe gets standard coverage.
--
-- Audit context: live registry had `status='operational' AND config.mode='shadow'`,
-- so dashboards showed the bridge as fully operational while emission was
-- still gated off internally. Operators saw `last_run_signals=0` and assumed
-- something was broken. The dispatcher (modal_workers/app.py) and the bridge
-- (modal_workers/scanners/fda_signal_bridge.py) have been taught to treat
-- `shadow` / `shadow_with_emit` / `operational` uniformly as runnable
-- lifecycle stages — see _ALLOWED_DISPATCH_STATUSES in app.py and STATUS_TO_MODE in
-- the bridge. With those code changes in place, this migration:
--
--   1. Flips fda_signal_bridge.status from 'operational' to 'shadow' so the
--      lifecycle stage matches the bridge's actual emission behavior. The
--      Phase 6 cutover flow re-promotes via UPDATE to 'shadow_with_emit'
--      then 'operational' — no migration needed at flip time.
--   2. Adds endpoints.primary so scanner_probe (modal_workers/observability.py)
--      can probe it. Federal Register is public/no-auth; Polygon is gated by
--      requires_auth elsewhere and would only contribute 401s in a probe.
--   3. Drops the now-redundant config.mode field. Bridge reads cfg.status.
--
-- Idempotent — no-op if the bridge row is absent (initial registration in
-- migration 20260505000025 uses ON CONFLICT DO NOTHING).

UPDATE public.scanners
SET
  status = 'shadow',
  endpoints = jsonb_set(
    COALESCE(endpoints, '{}'::jsonb),
    '{primary}',
    to_jsonb('https://www.federalregister.gov/api/v1/documents'::text),
    true
  ),
  config = (COALESCE(config, '{}'::jsonb) - 'mode')
WHERE name = 'fda_signal_bridge';
