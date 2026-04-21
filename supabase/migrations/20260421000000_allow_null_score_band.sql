-- Allow signals.score and signals.band to be NULL.
--
-- NULL = "unscored": the scanner did not produce a `raw_data.dimensions`
-- payload, so the rubric engine has nothing to score. Previously such
-- signals silently defaulted every dimension to 3, producing a score of
-- exactly 30 (every profile's weights sum to 10) and landing every
-- unscored signal in the watchlist band. That masked scanner gaps by
-- painting the UI with fake 30s.
--
-- After this migration, the rubric engine returns score=NULL, band=NULL
-- for signals missing dimensions. The UI renders them blank; downstream
-- index filters like `WHERE band_with_bonus = 'immediate'` already
-- exclude NULL rows, so no index changes are needed.
--
-- Companion code change: modal_workers/shared/rubric_engine.py and
-- unified_system/unified_system/tools/run_post_scan.py (score_signal).

ALTER TABLE signals ALTER COLUMN score DROP NOT NULL;
ALTER TABLE signals ALTER COLUMN band DROP NOT NULL;

COMMENT ON COLUMN signals.score IS
  'Pre-convergence weighted score. NULL = scanner did not supply raw_data.dimensions (unscored).';
COMMENT ON COLUMN signals.band IS
  'Pre-convergence band. NULL = unscored (scanner did not supply dimensions).';
