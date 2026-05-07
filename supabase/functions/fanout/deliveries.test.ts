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

Deno.test("alert subject populates alert_id, leaves candidate_event_id, candidate_id, and assessment_id null", () => {
  const row = deliveryRowFor(
    { kind: "alert", alert_id: "a-1" },
    "ops@example.com",
  );
  assert(row.alert_id === "a-1", "alert_id must be set");
  assert(row.candidate_event_id === null, "candidate_event_id must be null on alert path");
  assert(row.candidate_id === null, "candidate_id must be null on alert path");
  assert(row.assessment_id === null, "assessment_id must be null on alert path");
});

Deno.test("assessment subject populates assessment_id, leaves alert_id, candidate_event_id, and candidate_id null (v3 Stream 1)", () => {
  const row = deliveryRowFor(
    { kind: "assessment", assessment_id: "asmt-1" },
    "ops@example.com",
  );
  assert(row.alert_id === null, "alert_id must be null on assessment path");
  assert(row.candidate_event_id === null, "candidate_event_id must be null on assessment path");
  assert(row.candidate_id === null, "candidate_id must be null on assessment path");
  assert(row.assessment_id === "asmt-1", "assessment_id must be set");
  assert(row.channel === "email", "default channel is email");
  assert(row.status === "queued", "fresh rows start queued");
});

Deno.test("candidate_event subject leaves assessment_id null (back-compat)", () => {
  const row = deliveryRowFor(
    { kind: "candidate_event", candidate_event_id: "e", candidate_id: "c" },
    "x@y.z",
  );
  assert(row.assessment_id === null, "candidate_event row must not populate assessment_id");
});

Deno.test("subject_present CHECK is satisfied on all three paths", () => {
  // After v3 Stream 1, an alert_deliveries row must reference at least one of
  // (alert_id, candidate_event_id, assessment_id). Verify the helper never
  // produces a row that would violate that invariant.
  const candEvtRow = deliveryRowFor(
    { kind: "candidate_event", candidate_event_id: "e", candidate_id: "c" },
    "x@y.z",
  );
  const alertRow = deliveryRowFor({ kind: "alert", alert_id: "a" }, "x@y.z");
  const assessmentRow = deliveryRowFor(
    { kind: "assessment", assessment_id: "asmt-x" },
    "x@y.z",
  );
  for (const r of [candEvtRow, alertRow, assessmentRow]) {
    assert(
      r.alert_id !== null || r.candidate_event_id !== null || r.assessment_id !== null,
      "row must reference at least one parent",
    );
  }
});

Deno.test("each subject kind populates exactly one parent (mutual exclusion)", () => {
  const candEvtRow = deliveryRowFor(
    { kind: "candidate_event", candidate_event_id: "e", candidate_id: "c" },
    "x@y.z",
  );
  const alertRow = deliveryRowFor({ kind: "alert", alert_id: "a" }, "x@y.z");
  const assessmentRow = deliveryRowFor(
    { kind: "assessment", assessment_id: "asmt-x" },
    "x@y.z",
  );
  // candidate_event has BOTH candidate_event_id AND candidate_id (parent +
  // denormalized join key) — that's the documented exception. The other two
  // populate strictly one column each.
  assert(alertRow.alert_id !== null && alertRow.candidate_event_id === null && alertRow.assessment_id === null, "alert row populates only alert_id");
  assert(assessmentRow.assessment_id !== null && assessmentRow.alert_id === null && assessmentRow.candidate_event_id === null, "assessment row populates only assessment_id");
  assert(candEvtRow.candidate_event_id !== null && candEvtRow.alert_id === null && candEvtRow.assessment_id === null, "candidate_event row populates candidate_event_id (and candidate_id) but not other parents");
});

Deno.test("channel override flows through to the row", () => {
  const row = deliveryRowFor(
    { kind: "alert", alert_id: "a" },
    "ops@example.com",
    "realtime",
  );
  assert(row.channel === "realtime", "explicit channel arg must override default");
});
