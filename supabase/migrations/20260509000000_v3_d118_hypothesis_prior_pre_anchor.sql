-- 20260509000000_v3_d118_hypothesis_prior_pre_anchor.sql
-- D-118: persist the model's pre-anchor prior alongside the post-anchor value
-- so we can A/B the renormalization off (set prior_estimate_pct =
-- prior_estimate_pct_pre_anchor) and audit anchor-blend behavior.
--
-- The post-anchor value lives in prior_estimate_pct (already required); the
-- pre-anchor value is nullable since rows from before D-118 don't have one.

ALTER TABLE hypothesis_enumeration
  ADD COLUMN IF NOT EXISTS prior_estimate_pct_pre_anchor int
    CHECK (prior_estimate_pct_pre_anchor IS NULL OR
           prior_estimate_pct_pre_anchor BETWEEN 0 AND 100);

COMMENT ON COLUMN hypothesis_enumeration.prior_estimate_pct_pre_anchor IS
  'D-118: the model-emitted prior before Stage-4 anchor renormalization. '
  'NULL for pre-D-118 rows. prior_estimate_pct is the post-blend value.';
