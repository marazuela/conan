-- Allow v3 convergence_assessments fanout deliveries to satisfy the same
-- subject-present invariant as v2 alert and candidate_event deliveries.
--
-- 20260507100000_v3_alert_triggers.sql added alert_deliveries.assessment_id,
-- but the existing CHECK constraint still only allowed alert_id or
-- candidate_event_id. That made the fanout edge function's assessment-path
-- insert fail before a delivery audit row could be recorded.

ALTER TABLE public.alert_deliveries
  DROP CONSTRAINT IF EXISTS alert_deliveries_subject_present;

ALTER TABLE public.alert_deliveries
  ADD CONSTRAINT alert_deliveries_subject_present
    CHECK (
      alert_id IS NOT NULL
      OR candidate_event_id IS NOT NULL
      OR assessment_id IS NOT NULL
    );
