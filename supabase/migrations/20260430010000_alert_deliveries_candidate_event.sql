-- 20260430010000 — alert_deliveries: support state-change emails
--
-- The fanout edge function inserts into alert_deliveries from two paths:
--   1. Immediate-band alerts        → alert_id IS NOT NULL
--   2. Candidate state-change emails → alert_id IS NULL, sourced from a
--      candidate_events row (e.g. promoted/declined transitions)
--
-- The original schema enforced alert_id NOT NULL, so path #2 was failing on
-- every state-change email. This migration:
--   - Drops the NOT NULL on alert_id
--   - Adds candidate_event_id (audit-parent; CASCADE so deleting the source
--     event also removes its delivery rows — they're audit children)
--   - Adds candidate_id (denormalized convenience for joins; SET NULL so
--     deleting a candidate doesn't cascade-burn its delivery history)
--   - Adds an XOR-ish CHECK: every row must reference at least one of
--     alert_id / candidate_event_id (prevents orphaned deliveries)

ALTER TABLE public.alert_deliveries
  ALTER COLUMN alert_id DROP NOT NULL;

ALTER TABLE public.alert_deliveries
  ADD COLUMN candidate_event_id uuid
    REFERENCES public.candidate_events(id) ON DELETE CASCADE;

ALTER TABLE public.alert_deliveries
  ADD COLUMN candidate_id uuid
    REFERENCES public.candidates(id) ON DELETE SET NULL;

ALTER TABLE public.alert_deliveries
  ADD CONSTRAINT alert_deliveries_subject_present
    CHECK (alert_id IS NOT NULL OR candidate_event_id IS NOT NULL);

CREATE INDEX IF NOT EXISTS alert_deliveries_candidate_event_idx
  ON public.alert_deliveries(candidate_event_id)
  WHERE candidate_event_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS alert_deliveries_candidate_idx
  ON public.alert_deliveries(candidate_id)
  WHERE candidate_id IS NOT NULL;

COMMENT ON COLUMN public.alert_deliveries.candidate_event_id IS
  'Audit-parent for state-change emails. CASCADE delete: removing the source '
  'candidate_event removes its delivery rows. NULL when the row was triggered '
  'by an Immediate-band alert (in which case alert_id is non-null).';

COMMENT ON COLUMN public.alert_deliveries.candidate_id IS
  'Denormalized convenience for joining deliveries to candidates without '
  'going through candidate_events. SET NULL on candidate delete so the '
  'delivery history outlives the candidate row.';
