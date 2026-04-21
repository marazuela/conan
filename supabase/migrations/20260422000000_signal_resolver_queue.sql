-- signal_resolver queue: extend thesis_jobs.status with three new values
-- used by the new `signal_resolver` Cowork skill to drain unscored signals in
-- activist_governance / merger_arb / litigation.
--
--   needs_scoring                    — enqueued by reactor for unscored signals
--   scoring                          — claimed by signal_resolver during dim estimation
--   scoring_complete_below_immediate — scored but didn't reach immediate; terminal
--
-- Queued/drafting/gate_failed_retrying/promoted/dlq semantics unchanged. thesis_writer
-- still filters to status='queued'; signal_resolver filters to status='needs_scoring'.
-- When a resolved signal lands at immediate, the skill transitions the same row
-- through scoring → drafting → promoted (sharing the 15/day thesis quota).

ALTER TABLE thesis_jobs DROP CONSTRAINT thesis_jobs_status_check;
ALTER TABLE thesis_jobs ADD CONSTRAINT thesis_jobs_status_check
  CHECK (status IN (
    'queued',
    'drafting',
    'gate_failed_retrying',
    'promoted',
    'dlq',
    'needs_scoring',
    'scoring',
    'scoring_complete_below_immediate'
  ));

-- Partial index for signal_resolver's polling query: ORDER BY created_at ASC.
-- Kept separate from the existing thesis_jobs_status_idx to avoid bloating the
-- hot index with the larger needs_scoring volume (which can spike during scanner bursts).
CREATE INDEX thesis_jobs_needs_scoring_idx
  ON thesis_jobs(created_at)
  WHERE status = 'needs_scoring';
