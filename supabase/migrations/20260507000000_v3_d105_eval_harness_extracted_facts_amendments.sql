-- D-105 (2026-05-06) + D-103 (2026-05-06) — schema amendments for the
-- export-bundle integration. Builds on 20260506000010_v3_phase_0_1_schema.sql.
--
-- Three changes:
--
-- 1. eval_harness — add `tradeable_filter_pass` (NOT NULL DEFAULT false) and
--    `issuer_status` text. Survivorship-bias-free curation rule documented in
--    comment (include delisted/acquired/bankrupt issuers).
--
-- 2. extracted_facts — make `confidence` NOT NULL. Backfill any existing nulls
--    to 0.50 (the D-105 sentinel for "needs review"). New rows must populate
--    confidence per the [0,1] semantics in the v3 plan.
--
-- 3. eval_runs — add gate-criterion fields per D-103 (paired-bootstrap p,
--    Brier delta, AUC delta, n_eval_cases, max_single_asset_contribution_pct,
--    gate_reason). passed_gate stays as the boolean output of the gate; the
--    new fields record the *inputs* so failed gates are diagnosable.
--
-- Idempotent: every ADD COLUMN uses IF NOT EXISTS; the NOT NULL on
-- extracted_facts.confidence runs only if the column is currently nullable.

BEGIN;

-- ============================================================================
-- 1. eval_harness — D-105
-- ============================================================================

ALTER TABLE eval_harness
  ADD COLUMN IF NOT EXISTS tradeable_filter_pass boolean NOT NULL DEFAULT false;

ALTER TABLE eval_harness
  ADD COLUMN IF NOT EXISTS issuer_status text;

ALTER TABLE eval_harness
  DROP CONSTRAINT IF EXISTS eval_harness_issuer_status_check;
ALTER TABLE eval_harness
  ADD CONSTRAINT eval_harness_issuer_status_check
  CHECK (issuer_status IS NULL
         OR issuer_status IN ('active','acquired','delisted','bankrupt'));

CREATE INDEX IF NOT EXISTS eval_harness_tradeable_idx
  ON eval_harness(tradeable_filter_pass) WHERE tradeable_filter_pass = true;

COMMENT ON COLUMN eval_harness.tradeable_filter_pass IS
  'D-105 (2026-05-06): true iff the issuer passed the tradeable filter at '
  'reference_assessment_date (mcap ≥ $215M USD, listed on NYSE/NASDAQ/AMEX/LSE, '
  '90-day ADV ≥ $500K USD). Rows with false stay in the table for audit but are '
  'excluded from primary calibration fits. Default false until backfilled.';

COMMENT ON COLUMN eval_harness.issuer_status IS
  'D-105 (2026-05-06): issuer state at reference_assessment_date — '
  'active|acquired|delisted|bankrupt. Survivorship-bias audit: a healthy '
  'eval_harness MUST contain a representative tail of acquired+delisted+bankrupt. '
  'Nullable while the curation pipeline backfills.';

COMMENT ON TABLE eval_harness IS
  'v3 Phase 0: held-out resolved historical FDA signals with realized outcomes. '
  'Gold standard for prompt iteration. D-105: NO SURVIVORSHIP BIAS — include '
  'delisted, acquired, taken-private, bankrupt issuers; the negative tail is '
  'essential for calibration. Stratify holdout sampling on (indication × phase × '
  'outcome).';

-- ============================================================================
-- 2. extracted_facts.confidence NOT NULL — D-105
-- ============================================================================

-- Backfill existing nulls to 0.50 — the D-105 sentinel meaning "extraction
-- predates the NOT NULL contract; treat as derived/inferred and review before
-- the orchestrator consumes."
UPDATE extracted_facts
SET confidence = 0.50
WHERE confidence IS NULL;

ALTER TABLE extracted_facts
  ALTER COLUMN confidence SET NOT NULL;

-- Range constraint — every confidence ∈ [0, 1].
ALTER TABLE extracted_facts
  DROP CONSTRAINT IF EXISTS extracted_facts_confidence_range;
ALTER TABLE extracted_facts
  ADD CONSTRAINT extracted_facts_confidence_range
  CHECK (confidence >= 0.0 AND confidence <= 1.0);

COMMENT ON COLUMN extracted_facts.confidence IS
  'D-105 (2026-05-06): NOT NULL, [0,1] semantics: '
  '1.00 primary-source verbatim quote; '
  '0.70-0.99 LLM-extracted from primary source with quote span verified; '
  '0.50-0.69 derived/inferred from primary fields; '
  '< 0.50 speculative — flagged for review, downstream consumers should '
  'down-weight or filter.';

-- ============================================================================
-- 3. eval_runs gate-criterion fields — D-103
-- ============================================================================

ALTER TABLE eval_runs
  ADD COLUMN IF NOT EXISTS brier_delta_vs_prod numeric(6,4);

ALTER TABLE eval_runs
  ADD COLUMN IF NOT EXISTS paired_bootstrap_p numeric(5,4);

ALTER TABLE eval_runs
  ADD COLUMN IF NOT EXISTS ranking_auc_delta_vs_prod numeric(5,3);

ALTER TABLE eval_runs
  ADD COLUMN IF NOT EXISTS n_eval_cases int;

ALTER TABLE eval_runs
  ADD COLUMN IF NOT EXISTS max_single_asset_contribution_pct numeric(5,2);

ALTER TABLE eval_runs
  ADD COLUMN IF NOT EXISTS gate_reason text;

-- Range / domain constraints. Idempotent via DROP IF EXISTS.
ALTER TABLE eval_runs
  DROP CONSTRAINT IF EXISTS eval_runs_paired_bootstrap_p_range;
ALTER TABLE eval_runs
  ADD CONSTRAINT eval_runs_paired_bootstrap_p_range
  CHECK (paired_bootstrap_p IS NULL
         OR (paired_bootstrap_p >= 0.0 AND paired_bootstrap_p <= 1.0));

ALTER TABLE eval_runs
  DROP CONSTRAINT IF EXISTS eval_runs_n_eval_cases_nonneg;
ALTER TABLE eval_runs
  ADD CONSTRAINT eval_runs_n_eval_cases_nonneg
  CHECK (n_eval_cases IS NULL OR n_eval_cases >= 0);

ALTER TABLE eval_runs
  DROP CONSTRAINT IF EXISTS eval_runs_asset_contribution_range;
ALTER TABLE eval_runs
  ADD CONSTRAINT eval_runs_asset_contribution_range
  CHECK (max_single_asset_contribution_pct IS NULL
         OR (max_single_asset_contribution_pct >= 0.0
             AND max_single_asset_contribution_pct <= 100.0));

ALTER TABLE eval_runs
  DROP CONSTRAINT IF EXISTS eval_runs_gate_reason_check;
ALTER TABLE eval_runs
  ADD CONSTRAINT eval_runs_gate_reason_check
  CHECK (gate_reason IS NULL
         OR gate_reason IN
            ('pass','n_too_low','p_above_threshold','auc_delta_below',
             'asset_concentration','brier_regression','no_baseline'));

COMMENT ON COLUMN eval_runs.brier_delta_vs_prod IS
  'D-103: candidate Brier minus production Brier; positive = improvement.';
COMMENT ON COLUMN eval_runs.paired_bootstrap_p IS
  'D-103: p-value on paired-bootstrap of Brier delta (10000 resamples). '
  'Gate requires p < 0.05.';
COMMENT ON COLUMN eval_runs.ranking_auc_delta_vs_prod IS
  'D-103: candidate ranking AUC minus production AUC. Gate requires ≥ 0.05.';
COMMENT ON COLUMN eval_runs.n_eval_cases IS
  'D-103: number of resolved cases in the eval set. Gate requires ≥ 200.';
COMMENT ON COLUMN eval_runs.max_single_asset_contribution_pct IS
  'D-103: largest single-asset contribution to the Brier-delta win, in pct. '
  'Gate requires ≤ 5.0 to prevent lucky-batch promotions.';
COMMENT ON COLUMN eval_runs.gate_reason IS
  'D-103: ''pass'' | ''n_too_low'' | ''p_above_threshold'' | ''auc_delta_below'' | '
  '''asset_concentration'' | ''brier_regression'' | ''no_baseline''. Diagnostic '
  'for failed gates.';

COMMENT ON TABLE eval_runs IS
  'v3: every prompt change tested against eval_harness. D-103 (2026-05-06) '
  'locked passed_gate=true iff ALL of: brier_delta_vs_prod > 0; '
  'paired_bootstrap_p < 0.05; n_eval_cases ≥ 200; ranking_auc_delta_vs_prod ≥ '
  '0.05; max_single_asset_contribution_pct ≤ 5.0. On pass: snapshot prior '
  'prompt + calibration_curve before promotion (D-104).';

COMMIT;
