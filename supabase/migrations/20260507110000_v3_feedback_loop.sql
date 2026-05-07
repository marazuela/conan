-- Conan v3 — closed feedback loop substrate (Stream 2).
--
-- Three additive pieces for the Phase 8 closed-loop machinery:
--
--   1. prompt_versions — append-only history of stage prompts (D-104).
--      Snapshot-before-mutation policy: every active prompt change inserts
--      a new row; existing rows are never updated. is_active is exclusive
--      per stage (partial unique index). Rollback flips is_active.
--
--   2. calibration_drift_log — daily Spearman correlation series for the
--      rollback monitor (D-104). Records every monitor pass + the rollback
--      decision so we can audit drift detection over time.
--
--   3. memory_files Storage bucket + RLS — backing store for the per-asset
--      memory files (Contract C5 between Stream 2 and Stream 3). Service-role
--      writes only; read-only via authenticated.

-- ---------------------------------------------------------------------------
-- 1) prompt_versions
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.prompt_versions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  stage text NOT NULL CHECK (stage IN (
    'stage_1','stage_2','stage_3','stage_5','stage_6','stage_7','stage_9',
    'extractor','asset_linker','post_mortem'
  )),
  version text NOT NULL,
  prompt_hash text NOT NULL,
  prompt_text text NOT NULL,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  is_active boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  superseded_at timestamptz,
  superseded_by uuid REFERENCES public.prompt_versions(id),
  UNIQUE (stage, version)
);

COMMENT ON TABLE public.prompt_versions IS
  'v3 D-104 snapshot-before-mutation policy. Append-only prompt history per stage. is_active is exclusive per stage (see partial unique index). Rollback = flip is_active to a prior row.';

-- Exactly one row per stage may have is_active=true.
CREATE UNIQUE INDEX IF NOT EXISTS prompt_versions_active_per_stage_idx
  ON public.prompt_versions (stage)
  WHERE is_active = true;

CREATE INDEX IF NOT EXISTS prompt_versions_stage_created_idx
  ON public.prompt_versions (stage, created_at DESC);

-- ---------------------------------------------------------------------------
-- 2) calibration_drift_log
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.calibration_drift_log (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  computed_at timestamptz NOT NULL DEFAULT now(),
  -- Spearman correlation between realized_30d_return and conviction_pct_calibrated
  -- over the last 30 days of resolved post_mortem_queue rows.
  spearman_corr numeric(5,4),
  n_resolved_in_window int NOT NULL,
  -- Spearman delta vs the window 24h prior (negative = correlation dropped).
  delta_from_prior numeric(5,4),
  -- Did this run fire a rollback?
  rollback_triggered boolean NOT NULL DEFAULT false,
  rollback_reason text CHECK (rollback_reason IN
    ('low_correlation','correlation_drop','below_min_n','no_baseline','no_drift')
  ),
  -- Snapshot the active curve version before/after this run for audit.
  active_curve_version_pre text,
  active_curve_version_post text,
  notes jsonb NOT NULL DEFAULT '{}'::jsonb
);

COMMENT ON TABLE public.calibration_drift_log IS
  'v3 D-104 rollback monitor audit log. One row per daily monitor pass. rollback_triggered=true rows correspond to operator_flags inserts. Used to chart drift over time + diagnose false positives.';

CREATE INDEX IF NOT EXISTS calibration_drift_log_computed_idx
  ON public.calibration_drift_log (computed_at DESC);

-- ---------------------------------------------------------------------------
-- 3) memory_files Storage bucket
--
-- Backing store for the C5 memory file contract. Service-role writes only
-- (orchestrator + post_mortem_runner); authenticated reads (dashboard memory
-- viewer in Phase 6).
-- ---------------------------------------------------------------------------
INSERT INTO storage.buckets (id, name, public)
VALUES ('memory_files', 'memory_files', false)
ON CONFLICT (id) DO NOTHING;

-- RLS policies on storage.objects scoped to bucket_id='memory_files'.
-- Drop existing policies idempotently.
DROP POLICY IF EXISTS "memory_files service role full access"
  ON storage.objects;
DROP POLICY IF EXISTS "memory_files authenticated read"
  ON storage.objects;

CREATE POLICY "memory_files service role full access"
  ON storage.objects FOR ALL
  TO service_role
  USING (bucket_id = 'memory_files')
  WITH CHECK (bucket_id = 'memory_files');

CREATE POLICY "memory_files authenticated read"
  ON storage.objects FOR SELECT
  TO authenticated
  USING (bucket_id = 'memory_files');

-- ---------------------------------------------------------------------------
-- 4) Notes
--
-- - prompt_versions is consumed by:
--   - nightly_calibration_refit.py (snapshot active row before activating new curve)
--   - rollback_monitor.py (no direct write; reads `is_active` to surface "what's live now?")
--   - Future Stage 1/2/3 prompt iteration tooling.
-- - calibration_drift_log is written ONLY by rollback_monitor.py. Read by the
--   dashboard's Phase 6 calibration page.
-- - The memory_files bucket holds markdown per the C5 contract:
--     /memory_files/asset_<asset_id>.md
--     /memory_files/indication_<slug>.md          (Stream 3 future)
--     /memory_files/reviewer_<panel_id>.md        (Stream 3 future)
--     /memory_files/sub_agent/<role>/<scope>.md   (Stream 4 future)
-- - Rollback: DROP both tables + the bucket.
-- ---------------------------------------------------------------------------
