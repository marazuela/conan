-- =============================================================================
-- WI-6 — eval_sample_balance_audits table
--
-- Q2 (audit-historical-sample-balance) runs on the q1_verdict='clean' subset
-- of eval_harness and emits one row per cohort, hashed deterministically on
-- the sorted (asset_id, reference_assessment_date) list. The verdict gates
-- curve promotion in nightly_calibration_refit when Q2_GATE_MODE='required'
-- (warn-mode for the first 30 days lets the audit shadow without blocking).
--
-- 5 axes: HIT/MISS ratio, time/sector/sponsor Herfindahls, survivorship.
-- See plan: /Users/Pico/.claude/plans/plan-it-thoroughly-unified-scroll.md
-- (WI-6)
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.eval_sample_balance_audits (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  cohort_hash     text NOT NULL,
  cohort_size     int NOT NULL CHECK (cohort_size >= 0),
  audit_date      date NOT NULL DEFAULT current_date,
  verdict         text NOT NULL CHECK (verdict IN ('pass','pass_with_warnings','fail')),
  axes            jsonb NOT NULL,
  phase5_triggers text[] NOT NULL DEFAULT '{}',
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (cohort_hash, audit_date)
);

CREATE INDEX IF NOT EXISTS eval_sample_balance_audits_date_idx
  ON public.eval_sample_balance_audits (audit_date DESC);

COMMENT ON TABLE public.eval_sample_balance_audits IS
  'WI-6 — per-cohort sample-balance audit results. cohort_hash = sha256(sorted "asset_id|ref_date" pairs)[:16]. Q2_GATE_MODE in {off, warn, required} controls whether verdict=fail blocks curve promotion in nightly_calibration_refit.';

COMMENT ON COLUMN public.eval_sample_balance_audits.axes IS
  'JSONB per-axis evidence. Keys: hit_miss_ratio, time_concentration, sector_concentration, sponsor_concentration, survivorship. Each: {value, threshold_warn, threshold_fail, status: "pass"|"warn"|"fail"}.';

COMMENT ON COLUMN public.eval_sample_balance_audits.phase5_triggers IS
  'Operator-facing follow-ups generated when an axis fails. Examples: expand_miss_tail_via_8k, broaden_sponsor_coverage, run_survivorship_audit.';

-- Q2 verdict marker on eval_runs so the gate decision is visible in the
-- same row that records the rest of the calibration-refit outcome.
ALTER TABLE public.eval_runs
  ADD COLUMN IF NOT EXISTS q2_audit_verdict text
    CHECK (q2_audit_verdict IS NULL OR q2_audit_verdict IN ('pass','pass_with_warnings','fail'));

COMMENT ON COLUMN public.eval_runs.q2_audit_verdict IS
  'WI-6 — Q2 sample-balance audit verdict for the training cohort that fed this run. Captured for shadow-mode measurement; gates promotion when Q2_GATE_MODE=required.';

-- Q2_GATE_MODE flag — three-state. Default 'warn' for the first 30 days.
INSERT INTO public.internal_config (key, value, updated_at)
VALUES ('q2_gate_mode', 'warn', now())
ON CONFLICT (key) DO NOTHING;

COMMENT ON COLUMN public.internal_config.key IS
  'Conan-wide configuration keys. Operator-readable. See migrations for individual key semantics; some recent additions: discipline_gate_enabled (WI-1), bc_pregate_enabled/threshold (WI-2), pdufa_strength_rubric (WI-3), q2_gate_mode (WI-6).';
