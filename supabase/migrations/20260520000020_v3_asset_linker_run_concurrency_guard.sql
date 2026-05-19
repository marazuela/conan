-- Concurrency guard for asset_linker runs.
--
-- Without this, two cron ticks overlapping (e.g. one run running >15min while
-- the next 15-min tick fires) would both query GET documents?linker_classified_at=is.null
-- and pick the SAME top-200 docs, doubling Sonnet spend for one batch's
-- progress. The new partial unique index forces at most one row with
-- status='running' per pass; a concurrent _start_run_row INSERT trips the
-- unique violation and the caller cleanly skips.
--
-- Also solves zombie-row buildup: if main() crashes between _start_run_row
-- and _finish_run_row, the row sits in 'running' forever. The caller now
-- reclaims any stale 'running' row (started >30 min ago) by flipping its
-- status to 'failed' before attempting its own INSERT.

CREATE UNIQUE INDEX IF NOT EXISTS asset_linker_runs_one_running_per_pass
  ON public.asset_linker_runs (pass)
  WHERE status = 'running';

COMMENT ON INDEX public.asset_linker_runs_one_running_per_pass IS
  'Concurrency guard: at most one asset_linker run per pass may be in '
  'status=running. _start_run_row reclaims stale running rows (>30min) before '
  'INSERT; a remaining conflict means another instance is actively running '
  'and the caller should skip cleanly.';
