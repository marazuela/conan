import { deliveryRowFor } from "./deliveries.ts";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

Deno.test("candidate_event subject populates candidate_event_id and candidate_id, leaves alert_id null", () => {
  const row = deliveryRowFor(
    { kind: "candidate_event", candidate_event_id: "evt-1", candidate_id: "cand-1" },
    "ops@example.com",
  );
  assert(row.alert_id === null, "alert_id must be null on candidate_event path");
  assert(row.candidate_event_id === "evt-1", "candidate_event_id must be set");
  assert(row.candidate_id === "cand-1", "candidate_id must be set");
  assert(row.channel === "email", "default channel is email");
  assert(row.target === "ops@example.com", "target must be passed through");
  assert(row.status === "queued", "fresh rows start queued");
});

Deno.test("alert subject populates alert_id, leaves candidate_event_id and candidate_id null", () => {
  const row = deliveryRowFor(
    { kind: "alert", alert_id: "a-1" },
    "ops@example.com",
  );
  assert(row.alert_id === "a-1", "alert_id must be set");
  assert(row.candidate_event_id === null, "candidate_event_id must be null on alert path");
  assert(row.candidate_id === null, "candidate_id must be null on alert path");
});

Deno.test("subject_present CHECK is satisfied on both paths", () => {
  // The migration's CHECK requires (alert_id IS NOT NULL OR candidate_event_id IS NOT NULL).
  // Verify the helper never produces a row that would violate it.
  const candEvtRow = deliveryRowFor(
    { kind: "candidate_event", candidate_event_id: "e", candidate_id: "c" },
    "x@y.z",
  );
  const alertRow = deliveryRowFor({ kind: "alert", alert_id: "a" }, "x@y.z");
  assert(
    candEvtRow.alert_id !== null || candEvtRow.candidate_event_id !== null,
    "candidate_event row must satisfy subject_present CHECK",
  );
  assert(
    alertRow.alert_id !== null || alertRow.candidate_event_id !== null,
    "alert row must satisfy subject_present CHECK",
  );
});

Deno.test("channel override flows through to the row", () => {
  const row = deliveryRowFor(
    { kind: "alert", alert_id: "a" },
    "ops@example.com",
    "realtime",
  );
  assert(row.channel === "realtime", "explicit channel arg must override default");
});
