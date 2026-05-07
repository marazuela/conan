-- 20260508000000_v3_stage_2_3_hypothesis_premortem.sql
-- v3 orchestrator Stage 2 (hypothesis enumeration) + Stage 3 (pre-mortem).
-- Plan ref: /Users/Pico/.claude/plans/stage-2-3-robust-wolf.md
--
-- The existing convergence_assessments table already has placeholder columns
-- (hypotheses jsonb, pre_mortem text, adversarial_challenges jsonb) from the
-- Phase 2 schema. Those stay populated with denormalized summaries for the
-- dashboard. The structured per-hypothesis data lives in two new tables so
-- it can be queried by hypothesis_id + linked to fact_ids without parsing
-- jsonb on every read.

-- ============================================================================
-- 1. hypothesis_enumeration — Stage 2 output, one row per hypothesis
-- ============================================================================

CREATE TABLE IF NOT EXISTS hypothesis_enumeration (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  assessment_id uuid NOT NULL
    REFERENCES convergence_assessments(id) ON DELETE CASCADE,
  hypothesis_id text NOT NULL,            -- 'H1', 'H2', ...
  label text NOT NULL CHECK (label IN
    ('bull','base','bear','event_specific')),
  claim text NOT NULL,                    -- one-sentence directional bet
  mechanism text NOT NULL,                -- 2-4 sentences, every clause cited
  direction text NOT NULL CHECK (direction IN
    ('bullish','bearish','event_specific')),
  supporting_fact_ids uuid[] NOT NULL DEFAULT '{}',
  contradicting_fact_ids uuid[] NOT NULL DEFAULT '{}',
  kill_conditions jsonb NOT NULL,         -- list[str]
  prior_estimate_pct int NOT NULL
    CHECK (prior_estimate_pct BETWEEN 0 AND 100),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (assessment_id, hypothesis_id)
);

CREATE INDEX IF NOT EXISTS hypothesis_enumeration_assessment_idx
  ON hypothesis_enumeration(assessment_id);

COMMENT ON TABLE hypothesis_enumeration IS
  'v3 Stage 2: enumerated competing hypotheses for an assessment. At minimum '
  '{bull, base, bear} per assessment; up to 5 total.';
COMMENT ON COLUMN hypothesis_enumeration.prior_estimate_pct IS
  'Stage 2 best-guess probability; Stage 4 reference-class anchoring may '
  'overwrite this with a base-rate-anchored value.';

-- ============================================================================
-- 2. premortem_assessments — Stage 3 output, one row per hypothesis
-- ============================================================================

CREATE TABLE IF NOT EXISTS premortem_assessments (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  assessment_id uuid NOT NULL
    REFERENCES convergence_assessments(id) ON DELETE CASCADE,
  hypothesis_id text NOT NULL,
  verdict text NOT NULL CHECK (verdict IN
    ('survives','weakened','falsified')),
  failure_modes jsonb NOT NULL,           -- list[FailureMode]
  disconfirming_searches jsonb NOT NULL DEFAULT '[]'::jsonb,
  update_triggers jsonb NOT NULL DEFAULT '[]'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (assessment_id, hypothesis_id),
  FOREIGN KEY (assessment_id, hypothesis_id)
    REFERENCES hypothesis_enumeration(assessment_id, hypothesis_id)
    DEFERRABLE INITIALLY DEFERRED
);

CREATE INDEX IF NOT EXISTS premortem_assessments_assessment_idx
  ON premortem_assessments(assessment_id);

COMMENT ON TABLE premortem_assessments IS
  'v3 Stage 3: pre-mortem verdict per hypothesis. Verdict feeds the Stage 9 '
  'conviction cap (all_falsified -> conviction_pct <= 30).';

-- ============================================================================
-- 3. convergence_assessments — Stage 3 verdict columns
-- ============================================================================

ALTER TABLE convergence_assessments
  ADD COLUMN IF NOT EXISTS pre_mortem_verdict text
    CHECK (pre_mortem_verdict IS NULL OR pre_mortem_verdict IN
      ('all_survive','partial','all_falsified','skipped'));

ALTER TABLE convergence_assessments
  ADD COLUMN IF NOT EXISTS surviving_hypothesis_ids text[]
    NOT NULL DEFAULT '{}';

COMMENT ON COLUMN convergence_assessments.pre_mortem_verdict IS
  'v3 Stage 3 rollup: all_survive | partial | all_falsified | skipped. '
  'When all_falsified, conviction_pct is capped at 30 by the Stage 9 wrapper.';
