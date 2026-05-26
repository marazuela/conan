-- v4 Phase 7: agentic feedback retrospective schema.
--
-- Two tables. The first is the input (per-category accuracy snapshots).
-- The second is the output (Opus-generated weight-change proposals
-- pending operator review).
--
-- Plan: ~/.claude/plans/proud-booping-seal.md (Phase 7).
--
-- ============================================================================
-- feedback_category_metrics — daily per-signal-category accuracy snapshot.
--
-- Producer: modal_workers/feedback/category_accuracy.py runs daily as part
-- of the existing daily_feedback_loop (modal_workers/feedback_loop_app.py).
-- For each (snapshot_date, profile, signal_category, horizon_days) tuple,
-- aggregates resolved post_mortem_queue rows over the trailing 90 days.
--
-- Consumer: the feedback_retrospective Cowork skill (Phase 7 weekly retro)
-- reads the last 30 days of snapshots and surfaces categories whose hit
-- rate has drifted or whose Brier exceeds the global Brier. Operator-side
-- dashboards may also drill into per-category accuracy over time.
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.feedback_category_metrics (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  snapshot_date date NOT NULL,
  profile text NOT NULL,
  signal_category text NOT NULL,
  horizon_days int NOT NULL CHECK (horizon_days > 0),
  n_cases int NOT NULL CHECK (n_cases >= 0),
  hit_rate numeric(5,4),                  -- fraction of HIT outcomes (0..1)
  mean_prediction_error numeric(8,4),     -- signed avg (predicted_pct - realized*100)
  mae numeric(8,4),                       -- mean absolute error
  brier_score numeric(6,4),               -- Σ(p - y)^2 / n where p is conviction/100, y∈{0,1}
  mean_conviction_pct numeric(6,2),       -- avg predicted conviction across cohort
  hit_count int NOT NULL DEFAULT 0,
  miss_count int NOT NULL DEFAULT 0,
  no_outcome_count int NOT NULL DEFAULT 0,
  cohort_window_start date NOT NULL,
  cohort_window_end date NOT NULL,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (snapshot_date, profile, signal_category, horizon_days)
);

CREATE INDEX IF NOT EXISTS idx_feedback_category_metrics_recent
  ON public.feedback_category_metrics (snapshot_date DESC, profile, signal_category);

CREATE INDEX IF NOT EXISTS idx_feedback_category_metrics_category_trend
  ON public.feedback_category_metrics (profile, signal_category, snapshot_date DESC);

COMMENT ON TABLE public.feedback_category_metrics IS
  'v4 Phase 7: daily per-signal-category accuracy snapshot. Driven by '
  'modal_workers/feedback/category_accuracy.py. Read by the weekly '
  'feedback_retrospective Cowork skill when proposing rubric weight changes.';

COMMENT ON COLUMN public.feedback_category_metrics.brier_score IS
  'Brier score: mean squared error between predicted probability '
  '(conviction_pct/100) and realized outcome (1=HIT, 0=MISS). Lower = '
  'better calibration. Global Brier lives on calibration_curves; this '
  'column gives the per-category breakdown the retrospective compares against.';

COMMENT ON COLUMN public.feedback_category_metrics.metadata IS
  'Free-form JSONB for snapshot context: aggregation rule version, '
  'data filters applied, anomalies flagged. Keeps the schema stable as '
  'the aggregation logic evolves.';


-- ============================================================================
-- rubric_proposals — Opus-generated rubric change proposals pending review.
--
-- Producer: the feedback_retrospective Cowork skill writes one row per run
-- when it has a concrete proposal. status='pending_operator_review' until
-- a human approves or rejects via the dashboard.
--
-- Consumer: the dashboard's pending-proposals view renders the diff vs the
-- currently-active rubrics row. On approve, dashboard code marks the active
-- rubrics row superseded_at = now() and inserts a new active row whose
-- dimension_weights match `proposed_weights`. On reject, status flips to
-- 'rejected' with a rejected_reason.
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.rubric_proposals (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  profile text NOT NULL,
  proposed_weights jsonb NOT NULL,        -- {dim: weight} matching dimension_weights shape
  current_weights jsonb NOT NULL,         -- snapshot at proposal time (audit)
  current_rubric_version int,             -- the rubrics.rubric_version this proposal targets
  rationale text NOT NULL,                -- Opus's natural-language explanation
  added_dimensions jsonb NOT NULL DEFAULT '{}'::jsonb,   -- {dim: {weight, reason}}
  dropped_dimensions text[] NOT NULL DEFAULT '{}'::text[],
  cohort_window_start date NOT NULL,
  cohort_window_end date NOT NULL,
  cohort_size int NOT NULL CHECK (cohort_size >= 0),
  agent_version text NOT NULL,            -- e.g. 'feedback_retrospective_v0'
  status text NOT NULL DEFAULT 'pending_operator_review'
    CHECK (status IN ('pending_operator_review', 'approved', 'rejected', 'superseded')),
  approved_by text,
  approved_at timestamptz,
  applied_rubric_id uuid REFERENCES public.rubrics(id),  -- set when status='approved'
  rejected_reason text,
  rejected_by text,
  rejected_at timestamptz,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT rubric_proposals_approval_consistent CHECK (
    (status = 'approved') = (approved_at IS NOT NULL AND approved_by IS NOT NULL)
  ),
  CONSTRAINT rubric_proposals_rejection_consistent CHECK (
    (status = 'rejected') = (rejected_at IS NOT NULL)
  )
);

CREATE INDEX IF NOT EXISTS idx_rubric_proposals_pending
  ON public.rubric_proposals (created_at DESC)
  WHERE status = 'pending_operator_review';

CREATE INDEX IF NOT EXISTS idx_rubric_proposals_by_profile
  ON public.rubric_proposals (profile, created_at DESC);

COMMENT ON TABLE public.rubric_proposals IS
  'v4 Phase 7: Opus-generated rubric weight-change proposals from the '
  'feedback_retrospective Cowork skill. Pending operator review until '
  'approved (dashboard inserts a new rubrics row + supersedes the old) '
  'or rejected. Never applied automatically — human-in-the-loop by design.';

COMMENT ON COLUMN public.rubric_proposals.current_weights IS
  'Snapshot of dimension_weights from the rubrics row that was active '
  'when this proposal was generated. Audit trail — proves the proposal '
  'was reasoning against a specific weight set, not a stale one.';

COMMENT ON COLUMN public.rubric_proposals.applied_rubric_id IS
  'FK to rubrics.id after operator approval. Lets us trace which rubric '
  'row implements which approved proposal. NULL until approved.';
