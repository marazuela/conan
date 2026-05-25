-- v4 Phase 3: track which v4 assessments have been transcribed to candidates.
--
-- The v4 thesis_transcriber Cowork skill drains
-- convergence_assessments rows where orchestrator_version_v4=true AND
-- band='immediate' AND alert_gate_status='pass' AND thesis_emitted_at IS NULL,
-- and renders each into a candidates row. Stamping thesis_emitted_at on
-- successful render is what prevents re-draining the same assessment on
-- the next tick. The v3 pipeline tracks this via thesis_jobs.status — v4
-- skips the thesis_jobs queue entirely so it needs its own marker.
--
-- Partial index keeps the transcriber's queue scan O(rows-pending-transcription)
-- rather than O(all-v4-rows).

ALTER TABLE public.convergence_assessments
  ADD COLUMN IF NOT EXISTS thesis_emitted_at timestamptz;

COMMENT ON COLUMN public.convergence_assessments.thesis_emitted_at IS
  'v4 path: timestamp when the thesis_transcriber rendered this assessment '
  'into a candidates row. NULL means transcription pending. v3 rows always '
  'leave this NULL (they use thesis_jobs.status instead).';

-- Transcription queue scan: pending v4 immediate-band rows that passed the
-- alert gate. The thesis_transcriber polls this on its scheduled cadence.
CREATE INDEX IF NOT EXISTS idx_convergence_assessments_v4_pending_transcription
  ON public.convergence_assessments (created_at ASC)
  WHERE orchestrator_version_v4 = true
    AND band = 'immediate'
    AND alert_gate_status = 'pass'
    AND thesis_emitted_at IS NULL;
