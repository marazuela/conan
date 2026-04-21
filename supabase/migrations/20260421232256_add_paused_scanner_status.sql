-- Allow scanners to be intentionally paused without overloading the semantics
-- of planned/experimental, and set the current active fleet to EDGAR, FDA,
-- and the dedicated M&A scanner while analyst bandwidth is constrained.

ALTER TABLE scanners
  DROP CONSTRAINT IF EXISTS scanners_status_check;

ALTER TABLE scanners
  ADD CONSTRAINT scanners_status_check
  CHECK (status IN ('operational', 'planned', 'deprecated', 'experimental', 'paused'));

UPDATE scanners
SET status = CASE
  WHEN name IN (
    'edgar_filing_monitor',
    'fda_pdufa_pipeline',
    'takeover_candidate_scanner'
  ) THEN 'operational'
  ELSE 'paused'
END;
