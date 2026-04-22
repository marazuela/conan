-- Add resolved_at to thesis_jobs so terminal (dlq) jobs can be manually dismissed
-- from the alerts inbox without mutating status — preserves the audit trail
-- (status stays 'dlq') while giving the UI a filter knob. Covers both:
--   1. DLQ jobs with a thesis_drafting_failures row (routine_declined path)
--   2. DLQ jobs without one (pre-drafting challenger_kill path)
-- The alerts page filters `.is('resolved_at', null)` on tracked statuses; the
-- partial index supports that predicate.

ALTER TABLE thesis_jobs
  ADD COLUMN IF NOT EXISTS resolved_at timestamptz;

COMMENT ON COLUMN thesis_jobs.resolved_at IS
  'Set when a terminal (dlq) job has been manually reviewed/dismissed. Null for active jobs and unreviewed DLQ jobs. Alerts inbox filters by resolved_at IS NULL.';

CREATE INDEX IF NOT EXISTS thesis_jobs_status_resolved_idx
  ON thesis_jobs (status)
  WHERE resolved_at IS NULL;
