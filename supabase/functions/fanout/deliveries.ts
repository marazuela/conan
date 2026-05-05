// Insert payload builder for `alert_deliveries`. Extracted from index.ts so
// the constraint-shape can be unit-tested without booting the supabase client.
//
// Schema (post-2026-04-30 migration): `alert_id` is nullable; rows must
// reference at least one of (alert_id, candidate_event_id). State-change /
// pre-edge promotion emails populate candidate_event_id (the audit-parent)
// and candidate_id (denormalized for joins). Immediate-band alerts populate
// alert_id only — candidate_event_id stays null.

export type DeliverySubject =
  | { kind: "alert"; alert_id: string }
  | { kind: "candidate_event"; candidate_event_id: string; candidate_id: string };

export interface DeliveryRow {
  alert_id: string | null;
  candidate_event_id: string | null;
  candidate_id: string | null;
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
      channel,
      target,
      status: "queued",
    };
  }
  return {
    alert_id: null,
    candidate_event_id: subject.candidate_event_id,
    candidate_id: subject.candidate_id,
    channel,
    target,
    status: "queued",
  };
}
