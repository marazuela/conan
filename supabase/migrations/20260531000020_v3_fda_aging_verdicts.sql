-- 20260531000020_v3_fda_aging_verdicts.sql
-- v3 aging verdict ledger. Stage A (SQL sweep) and Stage B (Cowork skill)
-- both write here, distinguished by `stage`. Separate table (not embedded in
-- convergence_assessments) because Stage A produces ~all rows as 'maintain'
-- verdicts and embedding would pollute the supersession chain on
-- convergence_assessments.superseded_at IS NULL.
-- Plan ref: /Users/Pico/.claude/plans/plan-it-thoroughly-tingly-fountain.md (M4)

CREATE TABLE IF NOT EXISTS public.fda_aging_verdicts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  asset_id uuid NOT NULL
    REFERENCES public.fda_assets(id) ON DELETE CASCADE,
  evaluated_at timestamptz NOT NULL DEFAULT now(),
  stage text NOT NULL
    CHECK (stage IN ('a_deterministic','b_claude_review')),
  recommendation text NOT NULL
    CHECK (recommendation IN
      ('maintain','promote_to_active','demote_to_watch','kill','deliver','flag_for_review')),
  trigger_rule text,                  -- e.g. 'aged_out_no_catalyst', 'catalyst_elapsed_gt_7d', 'kill_condition_K2_match'
  evidence_fact_ids uuid[] NOT NULL DEFAULT '{}',
  evidence_doc_ids uuid[] NOT NULL DEFAULT '{}',
  challenger_verdict text
    CHECK (challenger_verdict IS NULL OR challenger_verdict IN
      ('confirm','challenge','kill','decline')),
  consecutive_failures int NOT NULL DEFAULT 0,
  notes jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS fda_aging_verdicts_asset_recent_idx
  ON public.fda_aging_verdicts(asset_id, evaluated_at DESC);

CREATE INDEX IF NOT EXISTS fda_aging_verdicts_stage_recent_idx
  ON public.fda_aging_verdicts(stage, evaluated_at DESC);

-- Quota counter index: Stage B rows since UTC midnight.
CREATE INDEX IF NOT EXISTS fda_aging_verdicts_stage_b_today_idx
  ON public.fda_aging_verdicts(created_at)
  WHERE stage = 'b_claude_review';

COMMENT ON TABLE public.fda_aging_verdicts IS
  'v3 aging verdict ledger. stage=a_deterministic for SQL sweep (no Claude); '
  'stage=b_claude_review for Cowork skill (10/UTC-day cap). Stage A rows are '
  'high-volume bookkeeping (~all "maintain"); Stage B rows are decision-grade. '
  'Replaces v2 candidate_aging signals into candidate_events + outcomes; v3 '
  'reads from this table for the §8a prior-failure guard.';
COMMENT ON COLUMN public.fda_aging_verdicts.trigger_rule IS
  'Free-text label of the rule that produced this verdict. Stage A values: '
  'aged_out_no_catalyst | catalyst_elapsed_gt_7d | stale_active_no_catalyst | '
  'promote_catalyst_within_60d | catalyst_just_elapsed | maintain. Stage B '
  'values: kill_condition_<id>_match | deliver_condition_<id>_match | maintain.';
COMMENT ON COLUMN public.fda_aging_verdicts.consecutive_failures IS
  'Mirror of v2 candidate_aging_failures.consecutive_failures counter. '
  'Incremented when Stage B claims a triggered kill_condition but neither '
  'extracted_facts nor raw-doc regex matches; reset on next clean run. '
  'operator_flags fires (severity=warn, kind=aging_stuck) at >=3.';
