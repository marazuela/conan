-- Conan v2 — emissions ledger foundation (Phase 1a of accuracy rethink)
--
-- Context: the v2 gate infrastructure (two-gate thesis assess, signal_resolver,
-- candidate_aging) is prevention-only. Nothing measures whether gates are
-- calibrated or whether we're silently missing real catalysts (recall). This
-- migration adds the measurement foundation the coverage_auditor needs:
--
--   1. emission_outcome_label enum — post-resolution accuracy judgment,
--      orthogonal to outcomes.outcome_type (which is mechanical lifecycle state).
--      A candidate can be outcome_type=delivered AND outcome_label=post_edge_miss
--      (we emailed the user but it turned out to be post-edge).
--   2. outcomes extensions — realized_move at 1d/7d/30d horizons (the existing
--      realized_return is single-number), catalyst_hit_date (actual resolution
--      date, distinct from candidates.next_catalyst_date which is the prediction),
--      labeled_at / labeled_by for provenance.
--   3. catalyst_universe table — independent-truth catalyst ledger, populated by
--      fetchers (Phase 1b: modal_workers/fetchers/universe/*.py). Feeds the
--      coverage_auditor's "which catalysts did we miss" query.
--   4. emissions_ledger view — unified query surface joining
--      signals ← thesis_jobs ← candidates ← outcomes with derived gate_decision.
--      Primary consumer: coverage_auditor (Phase 1c, Cowork weekly task).
--      gate_decision derived from existing columns — no call-site writes needed.
--
-- Deviation from the approved plan: the plan described emissions_ledger as a new
-- TABLE. Existing tables (signals, thesis_jobs, candidates, outcomes) already
-- carry every field the plan listed, so a VIEW is strictly simpler — no
-- duplication, no write amplification, schema stays lean. The view is the
-- single query surface; underlying tables remain authoritative.
--
-- Idempotent. Safe to re-run.

-- ============================================================
-- 1. emission_outcome_label enum
-- ============================================================

DO $$ BEGIN
  CREATE TYPE emission_outcome_label AS ENUM (
    'pre_edge_hit',      -- we emitted pre-edge, catalyst resolved in our favor
    'post_edge_miss',    -- catalyst had already resolved / been priced when we emitted
    'dead_catalyst',     -- catalyst resolved unfavorably (e.g., deal broke, trial failed)
    'still_pending',     -- catalyst_date has not passed yet
    'unclear'            -- resolved but outcome ambiguous (no clean signal either way)
  );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ============================================================
-- 2. outcomes extensions
-- ============================================================

ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS outcome_label emission_outcome_label;
ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS catalyst_hit_date date;
ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS realized_move_1d numeric(6,3);
ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS realized_move_7d numeric(6,3);
ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS realized_move_30d numeric(6,3);
ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS labeled_at timestamptz;
ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS labeled_by uuid REFERENCES auth.users(id);

CREATE INDEX IF NOT EXISTS outcomes_label_idx
  ON outcomes(outcome_label) WHERE outcome_label IS NOT NULL;
CREATE INDEX IF NOT EXISTS outcomes_catalyst_hit_idx
  ON outcomes(catalyst_hit_date) WHERE catalyst_hit_date IS NOT NULL;
CREATE INDEX IF NOT EXISTS outcomes_candidate_created_idx
  ON outcomes(candidate_id, created_at DESC);

COMMENT ON COLUMN outcomes.outcome_label IS
  'Post-resolution accuracy judgment for the emissions ledger. Orthogonal to '
  'outcome_type (lifecycle): e.g., a delivered candidate can be post_edge_miss '
  'if the market had already priced the catalyst before our alert landed.';

COMMENT ON COLUMN outcomes.catalyst_hit_date IS
  'Actual date the catalyst resolved (FDA decision date, deal close, verdict). '
  'Distinct from candidates.next_catalyst_date which was the prediction.';

-- ============================================================
-- 3. catalyst_universe — independent-truth catalyst ledger
-- ============================================================
--
-- Populated daily by modal_workers/fetchers/universe/*.py (Phase 1b):
--   - fda_adcomm_pdufa.py     → fda_approval | fda_crl
--   - sec_8k_mna.py            → mna_announce | mna_close
--   - sec_13d_activist.py      → activist_13d | activist_proxy
--   - esma_short_resolved.py   → short_squeeze_resolved
--   - take_private_announce.py → take_private_announce | take_private_close
--   - litigation_verdicts.py   → litigation_verdict
--   - phase3_readouts.py       → phase3_readout (Phase 2)
--
-- Materiality gate: material_outcome='yes' when |realized_price_move| >= X%
-- (X varies by profile: 5% generic, 15% for binary_catalyst). Fetchers compute
-- realized_price_move from T-1 close to T+1 close (or T+5 for slow-motion
-- profiles like take_private). Non-material entries stay at material_outcome='no'
-- for false-positive rate analysis — they're not counted against recall targets.

CREATE TABLE IF NOT EXISTS catalyst_universe (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  profile text NOT NULL,
  ticker text,
  mic text,
  issuer_figi text,
  entity_id uuid REFERENCES entities(id),
  catalyst_type text NOT NULL CHECK (catalyst_type IN (
    'fda_approval','fda_crl',
    'mna_announce','mna_close',
    'activist_13d','activist_proxy',
    'short_squeeze_resolved',
    'litigation_verdict',
    'take_private_announce','take_private_close',
    'phase3_readout'
  )),
  catalyst_date date NOT NULL,
  material_outcome text NOT NULL CHECK (material_outcome IN ('yes','no','unclear')),
  realized_price_move numeric(6,3),
  price_move_window text CHECK (price_move_window IN ('t+1','t+5','t+30')),
  source_feed text NOT NULL,
  source_url text,
  raw_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  fetched_at timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (source_feed, catalyst_type, ticker, catalyst_date)
);

CREATE INDEX IF NOT EXISTS catalyst_universe_profile_date_idx
  ON catalyst_universe(profile, catalyst_date DESC);
CREATE INDEX IF NOT EXISTS catalyst_universe_ticker_mic_date_idx
  ON catalyst_universe(ticker, mic, catalyst_date DESC);
CREATE INDEX IF NOT EXISTS catalyst_universe_material_idx
  ON catalyst_universe(catalyst_date DESC) WHERE material_outcome = 'yes';
CREATE INDEX IF NOT EXISTS catalyst_universe_entity_date_idx
  ON catalyst_universe(entity_id, catalyst_date DESC)
  WHERE entity_id IS NOT NULL;

ALTER TABLE catalyst_universe ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE schemaname = 'public'
      AND tablename = 'catalyst_universe'
      AND policyname = 'catalyst_universe_select'
  ) THEN
    CREATE POLICY catalyst_universe_select ON catalyst_universe
      FOR SELECT TO authenticated USING (true);
  END IF;
END $$;

COMMENT ON TABLE catalyst_universe IS
  'Independent-truth catalyst ledger — what actually happened in the world, '
  'populated by fetchers from SEC EDGAR, FDA calendar, ESMA, CourtListener, etc. '
  'Coverage auditor joins this against emissions_ledger to identify recall gaps '
  '(material catalysts we never emitted a pre-edge signal for).';

-- ============================================================
-- 4. emissions_ledger view — unified query surface
-- ============================================================
--
-- Derived gate_decision buckets each signal by first-blocking decision:
--   promoted                   — thesis_jobs.status='promoted', candidate live
--   rejected_thesis            — thesis_jobs.status='dlq' (syntactic or challenger)
--   resolved_below_immediate   — signal_resolver scored below immediate
--   pending                    — still in queue (queued/drafting/scoring/retrying)
--   auto_capped                — rubric's auto-caps dropped band below immediate
--   below_band                 — rubric naturally scored below immediate
--   immediate_no_thesis_job    — anomaly (immediate band but no thesis_jobs row)
--   unknown                    — fall-through
--
-- Drill-down: gate_reason (jsonb) carries auto_caps_triggered array +
-- thesis_job_status + gate_reasons + attempt_count + challenge_count for
-- the auditor to slice by specific rule (e.g., merger_arb.rule_A_sub_scale_return).
--
-- LEFT JOINs preserve every signal even when it never reached thesis_jobs or
-- became a candidate — necessary for recall analysis (coverage auditor sees
-- "emitted but auto_capped" and "never_promoted" alongside "promoted").

CREATE OR REPLACE VIEW emissions_ledger AS
SELECT
  s.signal_id,
  s.scanner_id,
  sc.name                                   AS scanner_name,
  s.scoring_profile                         AS profile,
  s.entity_id,
  s.issuer_figi,
  e.primary_ticker                          AS ticker,
  e.primary_mic                             AS mic,
  s.signal_type,
  s.thesis_direction,
  s.scan_date                               AS scored_at,
  s.source_date,
  COALESCE(s.score_with_bonus, s.score)     AS score_total,
  COALESCE(s.band_with_bonus, s.band)       AS band,
  s.dimensions                              AS dims_json,
  s.auto_caps_triggered,
  s.convergence_bonus,

  CASE
    WHEN tj.status = 'promoted'                            THEN 'promoted'
    WHEN tj.status = 'dlq'                                 THEN 'rejected_thesis'
    WHEN tj.status = 'scoring_complete_below_immediate'    THEN 'resolved_below_immediate'
    WHEN tj.status IN ('queued','drafting','gate_failed_retrying',
                       'needs_scoring','scoring')          THEN 'pending'
    WHEN COALESCE(s.band_with_bonus, s.band) IN ('archive','watchlist','discard')
      AND array_length(s.auto_caps_triggered, 1) > 0       THEN 'auto_capped'
    WHEN COALESCE(s.band_with_bonus, s.band) IN ('archive','watchlist','discard')
                                                           THEN 'below_band'
    WHEN COALESCE(s.band_with_bonus, s.band) = 'immediate' THEN 'immediate_no_thesis_job'
    ELSE 'unknown'
  END                                       AS gate_decision,

  jsonb_build_object(
    'auto_caps_triggered',  s.auto_caps_triggered,
    'thesis_job_status',    tj.status,
    'thesis_gate_reasons',  tj.gate_reasons,
    'thesis_attempt_count', tj.attempt_count,
    'thesis_challenge_count', tj.challenge_count
  )                                         AS gate_reason,

  tj.id                                     AS thesis_job_id,
  tj.status                                 AS thesis_job_status,
  tj.candidate_id,
  c.state                                   AS candidate_state,
  c.thesis_approved_at                      AS promoted_at,
  c.next_catalyst_date                      AS predicted_catalyst_date,

  o.id                                      AS outcome_id,
  o.outcome_type                            AS resolution_type,
  o.created_at                              AS resolution_date,
  o.catalyst_hit_date,
  o.realized_move_1d,
  o.realized_move_7d,
  o.realized_move_30d,
  o.realized_return,
  o.outcome_label
FROM signals s
LEFT JOIN scanners sc ON sc.id = s.scanner_id
LEFT JOIN entities e  ON e.id  = s.entity_id
LEFT JOIN thesis_jobs tj ON tj.signal_id = s.signal_id
LEFT JOIN candidates c   ON c.id = tj.candidate_id
LEFT JOIN LATERAL (
  SELECT *
  FROM outcomes
  WHERE candidate_id = c.id
  ORDER BY created_at DESC
  LIMIT 1
) o ON TRUE;

COMMENT ON VIEW emissions_ledger IS
  'Unified ledger joining signals → thesis_jobs → candidates → outcomes. '
  'Derived gate_decision buckets each emission by first-blocking decision. '
  'Primary consumer: coverage_auditor (Cowork weekly task, Phase 1c). '
  'Deviation from plan: was spec''d as a table; implemented as a view to avoid '
  'data duplication and write amplification — existing tables carry all fields.';

GRANT SELECT ON emissions_ledger TO authenticated;
