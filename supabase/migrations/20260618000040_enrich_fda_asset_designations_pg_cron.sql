-- =============================================================================
-- pg_cron: daily binary-catalyst pre-gate designation enrichment
--
-- Follow-up to PR #200 (the bc-pregate / "NDA filter" rebuild). The reactor's
-- supabase/functions/reactor/bc-pregate.ts reads five fda_assets columns at
-- dispatch — priority_review, breakthrough_designation, sponsor_prior_nda_count,
-- first_time_sponsor, designations_enriched_at. Those are hydrated by
-- modal_workers/scripts/enrich_fda_asset_designations.py (PR #200), which until
-- now only ran by hand (the 80-asset live universe was enriched once on
-- 2026-06-04).
--
-- The gate fail-opens on un-enriched assets (designations_enriched_at IS NULL =>
-- passes), so a missing cron is NOT a stall risk — but new/changed assets stay
-- ungated until enriched. This job keeps the live universe enriched daily so the
-- gate actually filters new noise.
--
-- Fires 06:25 UTC, just after bc-class-precedent-refresh-daily (06:20 UTC), so
-- the two bc-pregate refreshers stay adjacent + ordered and don't fire their
-- cron HTTP POSTs at the same instant. Posts
--   {"action":"enrich_fda_asset_designations","args":{"stale_hours":20}}
-- to the compute_v3 multiplex named by
-- internal_config.modal_url_enrich_fda_asset_designations. The multiplex spawns
-- enrich_fda_asset_designations_worker and returns in <1s. stale_hours=20 skips
-- assets enriched in the last 20h: the 24h cadence re-enriches the whole universe
-- while a same-day manual catch-up run isn't reprocessed.
--
-- The endpoint + action ship in modal_workers/orchestrator_app.py
-- (enrich_fda_asset_designations_worker). Mirrors the live
-- bc-class-precedent-refresh-daily cron (jobid 32) for body/auth and the
-- earnings-calendar-daily migration for the schedule envelope. Reads the compute
-- secret from internal_config.compute_secret at run time — NEVER stored here.
--
-- Operator deploy order (Modal redeploy is gated to the xenodochial worktree at
-- HEAD==origin/main, see memory orchestrator_deploy_topology):
--   1. Merge PR #200 (enricher + bc_pregate_inputs) to main.
--   2. Merge this PR (worker + cron) to main.
--   3. Redeploy conan-v3-orchestrator from the xenodochial worktree so the
--      multiplex recognizes the enrich_fda_asset_designations action.
--   4. Point the URL at the live multiplex (same slot as the other v3 actions):
--        UPDATE public.internal_config
--           SET value = 'https://marazuela--compute-v3.modal.run'
--         WHERE key = 'modal_url_enrich_fda_asset_designations';
-- Until step 4 the URL is '' and the job exits cleanly with a NOTICE, so
-- unconfigured days don't pile up cron errors (matches the Phase 3a /
-- bc-class-precedent pattern). Setting the URL before step 3 would only produce
-- harmless 400 "unknown action" responses from the multiplex.
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS pg_cron WITH SCHEMA extensions CASCADE;

-- internal_config indirection seed. Empty value keeps the cron a no-op until an
-- operator sets the live multiplex URL post-deploy (step 4 above). ON CONFLICT
-- DO NOTHING so a re-apply never clobbers an already-configured URL.
INSERT INTO public.internal_config (key, value)
VALUES ('modal_url_enrich_fda_asset_designations', '')
ON CONFLICT (key) DO NOTHING;

DO $$
DECLARE
  v_existing_jobid bigint;
BEGIN
  SELECT jobid INTO v_existing_jobid
    FROM cron.job
    WHERE jobname = 'enrich-fda-asset-designations-daily';

  IF v_existing_jobid IS NOT NULL THEN
    PERFORM cron.unschedule(v_existing_jobid);
  END IF;

  PERFORM cron.schedule(
    'enrich-fda-asset-designations-daily',
    '25 6 * * *',
    $cron$
      DO $job$
      DECLARE
        v_url text;
        v_secret text;
      BEGIN
        SELECT value INTO v_url
          FROM public.internal_config
          WHERE key = 'modal_url_enrich_fda_asset_designations';

        IF v_url IS NULL OR v_url = '' THEN
          RAISE NOTICE 'enrich-fda-asset-designations-daily: modal URL not configured; skipping';
          RETURN;
        END IF;

        SELECT value INTO v_secret
          FROM public.internal_config
          WHERE key = 'compute_secret';

        PERFORM net.http_post(
          url := v_url,
          body := jsonb_build_object(
            'action', 'enrich_fda_asset_designations',
            'args',   jsonb_build_object('stale_hours', 20)
          ),
          headers := jsonb_build_object(
            'Content-Type', 'application/json',
            'x-conan-compute-secret', COALESCE(v_secret, '')
          ),
          timeout_milliseconds := 60000
        );
      END
      $job$;
    $cron$
  );
END
$$;
