-- =============================================================================
-- D-131 follow-up — schedule q1_audit_run + q2_audit_run pg_cron jobs
--
-- The Phase 3b Q1 (confounder + coverage) and Q2 (sample-balance Herfindahl)
-- audits were introduced in PR #147 (commit fc7aa89) with:
--   - Modal workers q1_audit_run_worker / q2_audit_run_worker
--     (modal_workers/orchestrator_app.py:729,743)
--   - compute_v3_dispatch routing for both actions
--   - internal_config rows modal_url_q1_audit_run / modal_url_q2_audit_run
--     (20260605000070_internal_config_modal_urls_q1_q2.sql)
--
-- …but no pg_cron schedule was ever created, so the workers never fire from
-- the scheduler. Q2 gates curve promotion in nightly_calibration_refit when
-- Q2_GATE_MODE='required' (currently 'warn' for the 30d shadow window) — so
-- the gate is silently inert until these jobs run.
--
-- Schedule rationale:
--   - 06:30 UTC for Q1: runs after the earnings-calendar 06:10 + FOMC 06:15
--     feeders so Q1 sees today's confounder data.
--   - 06:45 UTC for Q2: runs after Q1 has stamped q1_verdict='clean' on its
--     subset (Q2 audits that subset).
--   - Daily cadence matches the daily eval_harness refresh + nightly refit.
--
-- Body shape mirrors 20260613005100 (bc-class-precedent-refresh) and
-- 20260613005200 (Phase 3a/3b drift fix): _conan_modal_post_enqueue with
-- the canonical {action, args} payload.
-- =============================================================================

DO $$
DECLARE
  v_existing_jobid bigint;
BEGIN
  -- q1-audit-daily ----------------------------------------------------------
  SELECT jobid INTO v_existing_jobid
    FROM cron.job WHERE jobname = 'q1-audit-daily';
  IF v_existing_jobid IS NOT NULL THEN
    PERFORM cron.unschedule(v_existing_jobid);
  END IF;

  PERFORM cron.schedule(
    'q1-audit-daily',
    '30 6 * * *',
    $cron$
      SELECT public._conan_modal_post_enqueue(
        'compute_v3',
        jsonb_build_object(
          'action', 'q1_audit_run',
          'args',   jsonb_build_object('re_audit', false)
        )
      );
    $cron$
  );

  -- q2-audit-daily ----------------------------------------------------------
  SELECT jobid INTO v_existing_jobid
    FROM cron.job WHERE jobname = 'q2-audit-daily';
  IF v_existing_jobid IS NOT NULL THEN
    PERFORM cron.unschedule(v_existing_jobid);
  END IF;

  PERFORM cron.schedule(
    'q2-audit-daily',
    '45 6 * * *',
    $cron$
      SELECT public._conan_modal_post_enqueue(
        'compute_v3',
        jsonb_build_object(
          'action', 'q2_audit_run',
          'args',   jsonb_build_object('profile', 'binary_catalyst')
        )
      );
    $cron$
  );
END
$$;
