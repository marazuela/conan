-- Conan v3-only adoption — Phase 1 (producer halt)
--
-- Context:
--   2026-05-13: user decision to fully adopt v3 (FDA + FDA-relevant EDGAR
--   8-K/S-1 only). All non-FDA scoring profiles deprecated:
--     activist_governance, litigation, short_positioning, takeover_candidate
--     were already status='deprecated' (Phase 1 of v2 teardown, conan PR #30,
--     2026-05-11). This migration finishes the job by deprecating the last
--     non-FDA operational scanner: sec_8k_mna (default_scoring_profile=
--     'merger_arb').
--
--   Companion code change in modal_workers/app.py:
--     - sec_8k_mna_once removed (no more fetcher entry point).
--     - sec_8k_mna removed from _FETCHERS_AT_HOUR[13] so dispatch_release_times
--       no longer spawns it even on registry-failure fallback.
--     - dispatch_weekly removed (its only bucket scanner, takeover_candidate,
--       is deprecated; no operational weekly scanners remain).
--     - _DEFAULT_SCANNERS_3H and _DEFAULT_SCANNERS_WEEKLY emptied so the
--       fallback-bypass bug (5/11 audit) can't reintroduce deprecated scanners
--       on a Supabase reachability error.
--
--   Idempotent: WHERE clause limits the UPDATE to a single name; re-running
--   is a no-op.
--
--   Reversible: to revive, UPDATE ... SET status='operational' WHERE name='sec_8k_mna';
--   plus restore the line in _FETCHERS_AT_HOUR. archive_v2 still holds all
--   historical merger_arb data per the 2026-05-11 retention policy.

UPDATE public.scanners
SET status = 'deprecated',
    updated_at = now()
WHERE name = 'sec_8k_mna'
  AND status = 'operational';

-- Verification (run manually after apply):
--   SELECT name, status, default_scoring_profile
--   FROM public.scanners
--   WHERE status = 'operational'
--   ORDER BY name;
--
-- Expected: every row has default_scoring_profile IN ('binary_catalyst','fda_event').
