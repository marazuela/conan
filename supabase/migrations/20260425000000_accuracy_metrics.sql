-- Conan v2 — accuracy_metrics (Phase 1d of accuracy rethink)
--
-- Precision-side auditors that complement Phase 1c coverage_auditor's recall work:
--   precision_auditor    — gate_decision × outcome_label × confidence × band calibration.
--   timing_auditor       — (catalyst_hit_date − predicted_catalyst_date) + return-decay.
--   challenger_retro     — would today's challenger kill historical winners / pass losers?
--
-- All three write time-series aggregates to this table. One sparse-column row per
-- (auditor × run × cell). Per-auditor columns are nullable — a `precision` row
-- fills delivery_rate etc; a `timing` row fills timing_error_*; a `challenger_retro`
-- row fills miss_rate etc. Cell dimensions (profile, gate_decision, confidence,
-- outcome_label) are nullable for cross-dim aggregates.
--
-- Consumers: reporting_weekly (future Accuracy page), dashboard (future panel),
-- operator_flags producers (the three auditors themselves, comparing current run
-- to prior baseline by querying this table).
--
-- Idempotent. Safe to re-run.

CREATE TABLE IF NOT EXISTS public.accuracy_metrics (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  measured_at timestamptz NOT NULL DEFAULT now(),
  window_days int NOT NULL,
  auditor text NOT NULL CHECK (auditor IN ('precision','timing','challenger_retro')),

  -- Cell dimensions (nullable = cross-dim aggregate for this row)
  profile text,
  gate_decision text,
  confidence text,
  outcome_label text,

  -- Sample stats (universal)
  sample_n int NOT NULL,
  labeled_n int,
  insufficient_sample boolean NOT NULL DEFAULT false,

  -- precision_auditor metrics
  delivered_n int,
  killed_n int,
  expired_n int,
  pre_edge_hit_n int,
  post_edge_miss_n int,
  dead_catalyst_n int,
  delivery_rate numeric(5,4),
  pre_edge_hit_rate numeric(5,4),
  post_edge_miss_rate numeric(5,4),
  dead_catalyst_rate numeric(5,4),
  band_discrimination numeric(5,4),
  confidence_discrimination numeric(5,4),
  auto_cap_inversion numeric(5,4),

  -- timing_auditor metrics
  timing_error_median_days int,
  timing_error_abs_p50 int,
  timing_error_abs_p90 int,
  emission_lead_days int,
  decay_ratio_30d_over_1d numeric(6,3),
  mean_realized_move_1d numeric(6,3),
  mean_realized_move_7d numeric(6,3),
  mean_realized_move_30d numeric(6,3),
  mean_realized_return numeric(6,3),

  -- challenger_retro metrics
  sampled_total int,
  calibrated_hit_n int,
  ambiguous_hit_n int,
  miss_n int,
  save_n int,
  partial_save_n int,
  pass_through_n int,
  timing_catch_n int,
  timing_miss_n int,
  miss_rate numeric(5,4),
  pass_through_rate numeric(5,4),
  save_rate numeric(5,4),
  calibrated_hit_rate numeric(5,4),

  -- Audit trail
  evidence jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS accuracy_metrics_auditor_time_idx
  ON public.accuracy_metrics (auditor, measured_at DESC);
CREATE INDEX IF NOT EXISTS accuracy_metrics_profile_time_idx
  ON public.accuracy_metrics (profile, measured_at DESC)
  WHERE profile IS NOT NULL;
CREATE INDEX IF NOT EXISTS accuracy_metrics_gate_decision_time_idx
  ON public.accuracy_metrics (gate_decision, measured_at DESC)
  WHERE gate_decision IS NOT NULL;

ALTER TABLE public.accuracy_metrics ENABLE ROW LEVEL SECURITY;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE schemaname = 'public'
      AND tablename = 'accuracy_metrics'
      AND policyname = 'accuracy_metrics_select'
  ) THEN
    CREATE POLICY accuracy_metrics_select ON public.accuracy_metrics
      FOR SELECT TO authenticated USING (true);
  END IF;
END $$;

COMMENT ON TABLE public.accuracy_metrics IS
  'Time-series rollup of precision / timing / challenger-retro audits. Sparse-column '
  'design: each row fills only the metric block for its auditor. Primary consumers '
  'are the three Phase 1d auditors themselves (compare current run to prior baseline) '
  'and the weekly reporting PDF.';

COMMENT ON COLUMN public.accuracy_metrics.auditor IS
  'Which auditor produced this row. Determines which metric columns are populated. '
  'precision = gate+label calibration; timing = catalyst-date forecast accuracy; '
  'challenger_retro = would-today''s-challenger-change-past-verdicts.';

COMMENT ON COLUMN public.accuracy_metrics.insufficient_sample IS
  'True when sample_n < the auditor''s MIN_SAMPLE_N. The row is still written '
  '(preserves the time series / shows that the auditor ran) but rate metrics are '
  'NULL and no operator_flag is raised for this cell.';
