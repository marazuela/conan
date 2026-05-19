-- Move v3 fact extraction from blanket nightly cron to lazy on-demand.
--
-- The hourly `v3-fact-extractor` job (scheduled by
-- 20260511112403_v3_asset_linker_pg_cron.sql) pre-built extracted_facts for
-- every material asset_document. At 2026-05-11 it had consumed ~$24/day for
-- 5 days yet covered only 3 of 88 fda_assets with facts, while exactly two
-- assets (AXS-05, Veligrotug) were being assessed by Tier-1 — i.e. the spend
-- was building inputs nothing was reading.
--
-- New design: orchestrator_app.py calls _ensure_facts_extracted_for_asset()
-- synchronously before run_one in both entry points
-- (orchestrator_run_one + orchestrator_drain_queue). The extractor's
-- load_unextracted_links() already skips documents that have any
-- extracted_facts row, so this is idempotent and bounded
-- (LAZY_FACT_EXTRACT_MAX_DOCS=50, LAZY_FACT_EXTRACT_BUDGET_USD=5.0 per asset).
--
-- Tier-2 (Cowork bulk path) is intentionally NOT covered by lazy extraction.
-- enqueue_tier2_bulk builds the input blob synchronously and returns it for
-- Cowork to cache, so any fire-and-forget spawn would race the blob build
-- and lose. Tier-2 reads whatever facts already exist; Tier-1's lazy hook
-- populates them over time. If Tier-2 volume grows enough that fact staleness
-- hurts assessment quality, the right fix is a two-stage queue (new
-- orchestrator_runs.status value 'awaiting_facts' + an async extractor that
-- transitions rows to 'pending') — separate PR, deferred until evidence
-- demands it.
--
-- Reversal: re-run the schedule block from
-- 20260511112403_v3_asset_linker_pg_cron.sql lines 119–131.

do $$
declare
  v_jobid bigint;
begin
  select jobid into v_jobid from cron.job where jobname = 'v3-fact-extractor';
  if v_jobid is not null then
    perform cron.unschedule(v_jobid);
  end if;
end
$$;
