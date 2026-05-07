// Insert payload builder for `alert_deliveries`. Extracted from index.ts so
// the constraint-shape can be unit-tested without booting the supabase client.
//
// Schema (post-2026-04-30 migration + v3 Stream 1 amendment 2026-05-07):
// `alert_id`, `candidate_event_id`, and `assessment_id` are all nullable;
// rows must reference exactly one parent.
//   • Immediate-band v2 alerts → alert_id only.
//   • State-change / pre-edge promotion emails → candidate_event_id (audit
//     parent) + candidate_id (denormalized).
//   • v3 convergence_assessments band='immediate' → assessment_id only.

export type DeliverySubject =
  | { kind: "alert"; alert_id: string }
  | { kind: "candidate_event"; candidate_event_id: string; candidate_id: string }
  | { kind: "assessment"; assessment_id: string };

export interface DeliveryRow {
  alert_id: string | null;
  candidate_event_id: string | null;
  candidate_id: string | null;
  assessment_id: string | null;
  channel: "email" | "realtime";
  target: string;
  status: "queued" | "sent" | "failed" | "bounced";
}

export function deliveryRowFor(
  subject: DeliverySubject,
  target: string,
  channel: DeliveryRow["channel"] = "email",
): DeliveryRow {
  if (subject.kind === "alert") {
    return {
      alert_id: subject.alert_id,
      candidate_event_id: null,
      candidate_id: null,
      assessment_id: null,
      channel,
      target,
      status: "queued",
    };
  }
  if (subject.kind === "candidate_event") {
    return {
      alert_id: null,
      candidate_event_id: subject.candidate_event_id,
      candidate_id: subject.candidate_id,
      assessment_id: null,
      channel,
      target,
      status: "queued",
    };
  }
  // kind === "assessment"
  return {
    alert_id: null,
    candidate_event_id: null,
    candidate_id: null,
    assessment_id: subject.assessment_id,
    channel,
    target,
    status: "queued",
  };
}
