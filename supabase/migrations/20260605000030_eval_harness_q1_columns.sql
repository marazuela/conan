-- =============================================================================
-- WI-5 — eval_harness Q1 audit columns
--
-- Q1 (assess-event-data-quality, port of v2_skills) classifies each labeled
-- eval_harness row as {clean, confounded, discard} based on confounder
-- calendar hits (earnings ±5td, FOMC ±1d, SPY 3σ in window, in-window 8-K)
-- and coverage gaps (yfinance gap, low-volume %, pre-window delisting).
-- The verdict gates Q2 (sample-balance) which then gates curve promotion
-- in nightly_calibration_refit.
--
-- The audit script (modal_workers/scripts/audit_event_data_quality.py)
-- writes one row per eval_harness id. q1_audited_at lets the script skip
-- already-audited rows on re-run unless --re-audit is passed.
--
-- See plan: /Users/Pico/.claude/plans/plan-it-thoroughly-unified-scroll.md
-- (WI-5 + WI-6)
-- =============================================================================

ALTER TABLE public.eval_harness
  ADD COLUMN IF NOT EXISTS q1_verdict      text
    CHECK (q1_verdict IS NULL OR q1_verdict IN ('clean','confounded','discard')),
  ADD COLUMN IF NOT EXISTS q1_reasons      text[],
  ADD COLUMN IF NOT EXISTS q1_confounders  jsonb,
  ADD COLUMN IF NOT EXISTS q1_coverage     jsonb,
  ADD COLUMN IF NOT EXISTS q1_audited_at   timestamptz;

CREATE INDEX IF NOT EXISTS eval_harness_q1_verdict_idx
  ON public.eval_harness (q1_verdict)
  WHERE q1_verdict IS NOT NULL;

COMMENT ON COLUMN public.eval_harness.q1_verdict IS
  'WI-5 — Q1 audit verdict. clean = no confounder + no coverage gap + tradeable_filter_pass. confounded = confounder triggered but coverage OK. discard = coverage failure OR tradeable_filter_pass=false. NULL = not yet audited.';

COMMENT ON COLUMN public.eval_harness.q1_reasons IS
  'Top-level reason tokens. Examples: earnings_within_5td, fomc_day, spx_3sigma_t+12, material_8k_in_window, yfinance_window_gap, low_volume_days_pct, pre_window_delisting, tradeable_filter_failed.';

COMMENT ON COLUMN public.eval_harness.q1_confounders IS
  'JSONB per-check evidence. Keys: earnings_within_5td, fomc_day, spx_3sigma_during_window, material_8k_in_window. Each value: {triggered: bool, evidence: dict}.';

COMMENT ON COLUMN public.eval_harness.q1_coverage IS
  'JSONB per-check evidence. Keys: yfinance_window_gap, low_volume_days_pct, pre_window_delisting. Same shape as q1_confounders.';
