-- Conan v2 — delta migration: post-approval spec amendments
--
-- Context: the initial schema landed in the live DB before the post-approval
-- amendments to spec.md (§3.4, §7.5, §7.6) were incorporated. This migration
-- closes that gap without disturbing populated tables.
--
-- Additions:
--   1. candidates: kill_conditions jsonb, next_catalyst_date, next_catalyst_window daterange,
--      last_aging_evaluated_at timestamptz, CHECKs + 3 partial indexes for aging hot paths.
--   2. scanners: last_probe_at, last_probe_status, last_probe_latency_ms (for §7.6.2 scanner_probe).
--   3. candidate_events: expand event_type CHECK to include
--      thesis_drafted_by_claude + thesis_approved_by_user.
--   4. New table: candidate_aging_failures (DLQ for §7.5 candidate_aging routine).
--   5. New table: operator_flags (structured drift surface, replaces v1 OPEN_QUESTIONS.md;
--      written by §7.6 observability functions + candidate_aging + thesis_writer).
--
-- Idempotent: all DDL guarded with IF NOT EXISTS / DROP-then-ADD patterns where
-- safe. Can be re-run without disturbing existing rows.

-- 1. candidates additions
ALTER TABLE candidates ADD COLUMN IF NOT EXISTS kill_conditions jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE candidates ADD COLUMN IF NOT EXISTS next_catalyst_date date;
ALTER TABLE candidates ADD COLUMN IF NOT EXISTS next_catalyst_window daterange;
ALTER TABLE candidates ADD COLUMN IF NOT EXISTS last_aging_evaluated_at timestamptz;

-- CHECK constraints — guarded with dropN+add pattern so re-runs are safe.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'public.candidates'::regclass
      AND conname = 'candidates_catalyst_exactly_one'
  ) THEN
    ALTER TABLE candidates ADD CONSTRAINT candidates_catalyst_exactly_one CHECK (
      (next_catalyst_date IS NULL) <> (next_catalyst_window IS NULL)
      OR (next_catalyst_date IS NULL AND next_catalyst_window IS NULL)
    );
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'public.candidates'::regclass
      AND conname = 'candidates_kill_conditions_is_array'
  ) THEN
    ALTER TABLE candidates ADD CONSTRAINT candidates_kill_conditions_is_array
      CHECK (jsonb_typeof(kill_conditions) = 'array');
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS candidates_state_score_idx
  ON candidates(state, current_score DESC) WHERE state IN ('active','watch');
CREATE INDEX IF NOT EXISTS candidates_catalyst_date_idx
  ON candidates(next_catalyst_date) WHERE next_catalyst_date IS NOT NULL AND state IN ('active','watch');
CREATE INDEX IF NOT EXISTS candidates_aging_due_idx
  ON candidates(last_aging_evaluated_at NULLS FIRST) WHERE state IN ('active','watch');

-- 2. scanners probe columns
ALTER TABLE scanners ADD COLUMN IF NOT EXISTS last_probe_at timestamptz;
ALTER TABLE scanners ADD COLUMN IF NOT EXISTS last_probe_status text;
ALTER TABLE scanners ADD COLUMN IF NOT EXISTS last_probe_latency_ms int;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'public.scanners'::regclass
      AND conname = 'scanners_last_probe_status_check'
  ) THEN
    ALTER TABLE scanners ADD CONSTRAINT scanners_last_probe_status_check
      CHECK (last_probe_status IS NULL OR last_probe_status IN ('ok','fallback','drift','content_shape_drift','timeout','error'));
  END IF;
END $$;

-- 3. candidate_events event_type CHECK — drop + re-add with expanded set
ALTER TABLE candidate_events DROP CONSTRAINT IF EXISTS candidate_events_event_type_check;
ALTER TABLE candidate_events ADD CONSTRAINT candidate_events_event_type_check
  CHECK (event_type IN (
    'created','state_changed','scored','note_added',
    'thesis_drafted_by_claude','thesis_updated','thesis_approved_by_user',
    'convergence','gate_rejected'
  ));

-- 4. candidate_aging_failures (§7.5 DLQ)
CREATE TABLE IF NOT EXISTS candidate_aging_failures (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  candidate_id uuid NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
  attempt_at timestamptz NOT NULL DEFAULT now(),
  error_kind text NOT NULL CHECK (error_kind IN (
    'routine_error','routine_declined','hallucinated_trigger','quota_exhausted','gate_mismatch','other'
  )),
  error_message text,
  routine_output jsonb,
  consecutive_failures smallint NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS candidate_aging_failures_recent_idx
  ON candidate_aging_failures(candidate_id, attempt_at DESC);
ALTER TABLE candidate_aging_failures ENABLE ROW LEVEL SECURITY;
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='public' AND tablename='candidate_aging_failures' AND policyname='candidate_aging_failures_select') THEN
    CREATE POLICY candidate_aging_failures_select ON candidate_aging_failures FOR SELECT TO authenticated USING (true);
  END IF;
END $$;

-- 5. operator_flags (§3.4, §7.6 common sink, replaces OPEN_QUESTIONS.md)
CREATE TABLE IF NOT EXISTS operator_flags (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  severity text NOT NULL CHECK (severity IN ('info','warn','critical')),
  source text NOT NULL CHECK (source IN (
    'translation_health','scanner_probe','convergence_qa','candidate_aging',
    'thesis_writer','reactor','reporting_weekly','litigation_baselines','manual'
  )),
  kind text NOT NULL,
  scanner_id uuid REFERENCES scanners(id),
  entity_id uuid REFERENCES entities(id),
  signal_id text REFERENCES signals(signal_id),
  candidate_id uuid REFERENCES candidates(id),
  title text NOT NULL,
  body text,
  evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
  resolved_at timestamptz,
  resolved_by uuid REFERENCES auth.users(id),
  resolved_note text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

-- Partial unique prevents duplicate open flags for the same (source, kind, subject) tuple.
-- Producers use INSERT … ON CONFLICT DO UPDATE to bump `evidence` instead of inserting duplicates.
CREATE UNIQUE INDEX IF NOT EXISTS operator_flags_open_uniq
  ON operator_flags (
    source,
    kind,
    coalesce(scanner_id::text, ''),
    coalesce(entity_id::text, ''),
    coalesce(signal_id, ''),
    coalesce(candidate_id::text, '')
  )
  WHERE resolved_at IS NULL;
CREATE INDEX IF NOT EXISTS operator_flags_open_idx
  ON operator_flags(severity DESC, created_at DESC) WHERE resolved_at IS NULL;

-- updated_at trigger (set_updated_at function already exists from initial migration)
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgrelid = 'public.operator_flags'::regclass
      AND tgname = 'operator_flags_updated'
  ) THEN
    CREATE TRIGGER operator_flags_updated BEFORE UPDATE ON operator_flags
      FOR EACH ROW EXECUTE FUNCTION set_updated_at();
  END IF;
END $$;

ALTER TABLE operator_flags ENABLE ROW LEVEL SECURITY;
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='public' AND tablename='operator_flags' AND policyname='operator_flags_select') THEN
    CREATE POLICY operator_flags_select ON operator_flags FOR SELECT TO authenticated USING (true);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='public' AND tablename='operator_flags' AND policyname='operator_flags_resolve') THEN
    CREATE POLICY operator_flags_resolve ON operator_flags FOR UPDATE TO authenticated
      USING (true) WITH CHECK (resolved_by = auth.uid());
  END IF;
END $$;
