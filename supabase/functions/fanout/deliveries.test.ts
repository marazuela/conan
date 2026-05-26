import {
  assessmentConvictionValue,
  assessmentSubjectTag,
  deliveryRowFor,
  shouldSendAssessmentImmediateEmail,
} from "./deliveries.ts";

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

Deno.test("assessmentConvictionValue prefers calibrated conviction", () => {
  const value = assessmentConvictionValue({
    asset_id: "asset-1",
    conviction_pct: 92,
    conviction_pct_calibrated: 81,
  });
  assert(value === 81, "calibrated conviction must win when present");
});

Deno.test("assessment email gate sends first immediate assessment", () => {
  const decision = shouldSendAssessmentImmediateEmail({
    asset_id: "asset-1",
    band: "immediate",
    thesis_direction: "long",
    conviction_pct_calibrated: 82,
    document_set_hash: "hash-1",
    created_at: "2026-05-23T10:00:00Z",
  }, null);
  assert(decision.send === true, "first assessment should send");
  assert(decision.reason === "first_assessment_for_recipient_asset", "reason should explain first-send path");
});

Deno.test("assessment email gate suppresses failed constitutional gate", () => {
  const decision = shouldSendAssessmentImmediateEmail({
    asset_id: "asset-1",
    band: "immediate",
    gate_status: "pass",
    constitutional_pass: false,
    thesis_direction: "long",
    conviction_pct_calibrated: 82,
    expected_value_bps: 250,
    target_type: "price_move",
    label_rule: "forward_return_t30_calendar",
  }, null);
  assert(decision.send === false, "failed constitutional check must suppress email");
  assert(decision.reason === "constitutional_not_passed", "reason should identify constitutional gate");
});

Deno.test("assessment email gate suppresses non-positive expected value", () => {
  const decision = shouldSendAssessmentImmediateEmail({
    asset_id: "asset-1",
    band: "immediate",
    gate_status: "pass",
    constitutional_pass: true,
    thesis_direction: "long",
    conviction_pct_calibrated: 82,
    expected_value_bps: 0,
    target_type: "price_move",
    label_rule: "forward_return_t30_calendar",
  }, null);
  assert(decision.send === false, "non-positive EV must suppress email");
  assert(decision.reason === "non_positive_expected_value", "reason should identify EV gate");
});

Deno.test("assessment email gate suppresses explicit alert gate reasons", () => {
  const decision = shouldSendAssessmentImmediateEmail({
    asset_id: "asset-1",
    band: "immediate",
    alert_gate_status: "suppress",
    alert_gate_reasons: ["unsupported_claims_present"],
    thesis_direction: "long",
  }, null);
  assert(decision.send === false, "alert_gate_status=suppress must suppress email");
  assert(decision.reason === "unsupported_claims_present", "reason should preserve first gate reason");
});


Deno.test("assessment email gate suppresses unchanged evidence without material change", () => {
  const prior = {
    asset_id: "asset-1",
    band: "immediate",
    thesis_direction: "long",
    conviction_pct_calibrated: 82,
    document_set_hash: "same-hash",
    created_at: "2026-05-22T10:00:00Z",
  };
  const decision = shouldSendAssessmentImmediateEmail({
    asset_id: "asset-1",
    band: "immediate",
    thesis_direction: "long",
    conviction_pct_calibrated: 84,
    document_set_hash: "same-hash",
    created_at: "2026-05-23T10:00:00Z",
  }, prior, new Date("2026-05-24T10:00:00Z"));
  assert(decision.send === false, "same evidence and small conviction drift should not re-email");
  assert(decision.reason === "unchanged_evidence_no_material_change", "reason should identify unchanged evidence");
});

Deno.test("assessment email gate sends on material conviction change despite same evidence", () => {
  const decision = shouldSendAssessmentImmediateEmail({
    asset_id: "asset-1",
    band: "immediate",
    thesis_direction: "long",
    conviction_pct_calibrated: 88,
    document_set_hash: "same-hash",
  }, {
    asset_id: "asset-1",
    band: "immediate",
    thesis_direction: "long",
    conviction_pct_calibrated: 82,
    document_set_hash: "same-hash",
    created_at: "2026-05-23T10:00:00Z",
  });
  assert(decision.send === true, "5pp+ conviction move should send");
  assert(decision.reason === "conviction_changed", "reason should identify conviction change");
});

Deno.test("assessment email gate sends on thesis direction change despite same evidence", () => {
  const decision = shouldSendAssessmentImmediateEmail({
    asset_id: "asset-1",
    band: "immediate",
    thesis_direction: "short",
    conviction_pct_calibrated: 82,
    document_set_hash: "same-hash",
  }, {
    asset_id: "asset-1",
    band: "immediate",
    thesis_direction: "long",
    conviction_pct_calibrated: 82,
    document_set_hash: "same-hash",
    created_at: "2026-05-23T10:00:00Z",
  });
  assert(decision.send === true, "direction change should send");
  assert(decision.reason === "direction_changed", "reason should identify direction change");
});

Deno.test("assessment email gate suppresses no-hash churn inside cooldown", () => {
  const decision = shouldSendAssessmentImmediateEmail({
    asset_id: "asset-1",
    band: "immediate",
    thesis_direction: "long",
    conviction_pct_calibrated: 82,
    created_at: "2026-05-23T12:00:00Z",
  }, {
    asset_id: "asset-1",
    band: "immediate",
    thesis_direction: "long",
    conviction_pct_calibrated: 84,
    created_at: "2026-05-23T10:00:00Z",
  }, new Date("2026-05-23T12:00:00Z"));
  assert(decision.send === false, "missing hashes should still cooldown non-material repeats");
  assert(decision.reason === "cooldown_no_material_change", "reason should identify cooldown");
});

// ---------------------------------------------------------------------------
// assessmentSubjectTag — per-recipient subject prefix based on gate.reason.
// Lets recipients distinguish [NEW] / [DIRECTION CHANGE] / [Δ+12pp] / [REFRESH]
// at a glance instead of seeing the same [IMMEDIATE] · LONG · cross_source
// line every day.
// ---------------------------------------------------------------------------

const _baseCurrent = {
  asset_id: "asset-1",
  band: "immediate",
  thesis_direction: "long",
  conviction_pct_calibrated: 72,
};

Deno.test("assessmentSubjectTag returns NEW on first-time recipient", () => {
  const tag = assessmentSubjectTag(_baseCurrent, null, "first_assessment_for_recipient_asset");
  assert(tag === "NEW", `expected NEW, got ${tag}`);
});

Deno.test("assessmentSubjectTag returns DIRECTION CHANGE on direction flip", () => {
  const tag = assessmentSubjectTag(
    _baseCurrent,
    { ..._baseCurrent, thesis_direction: "short" },
    "direction_changed",
  );
  assert(tag === "DIRECTION CHANGE", `expected DIRECTION CHANGE, got ${tag}`);
});

Deno.test("assessmentSubjectTag returns signed Δpp on positive conviction change", () => {
  const tag = assessmentSubjectTag(
    { ..._baseCurrent, conviction_pct_calibrated: 84 },
    { ..._baseCurrent, conviction_pct_calibrated: 72 },
    "conviction_changed",
  );
  assert(tag === "Δ+12pp", `expected Δ+12pp, got ${tag}`);
});

Deno.test("assessmentSubjectTag returns signed Δpp on negative conviction change", () => {
  const tag = assessmentSubjectTag(
    { ..._baseCurrent, conviction_pct_calibrated: 60 },
    { ..._baseCurrent, conviction_pct_calibrated: 72 },
    "conviction_changed",
  );
  assert(tag === "Δ-12pp", `expected Δ-12pp, got ${tag}`);
});

Deno.test("assessmentSubjectTag returns NEW EVIDENCE on document set hash flip", () => {
  const tag = assessmentSubjectTag(
    { ..._baseCurrent, document_set_hash: "hash-b" },
    { ..._baseCurrent, document_set_hash: "hash-a" },
    "evidence_changed",
  );
  assert(tag === "NEW EVIDENCE", `expected NEW EVIDENCE, got ${tag}`);
});

Deno.test("assessmentSubjectTag returns REFRESH after cooldown with no material change", () => {
  const tag = assessmentSubjectTag(_baseCurrent, _baseCurrent, "cooldown_elapsed_or_unknown_evidence");
  assert(tag === "REFRESH", `expected REFRESH, got ${tag}`);
});

Deno.test("assessmentSubjectTag returns null on suppress-reasons (defensive)", () => {
  // Caller should not invoke the helper when send=false. Returning null
  // prevents producing a bare "[IMMEDIATE]" without context if they do.
  assert(assessmentSubjectTag(_baseCurrent, null, "not_immediate") === null, "not_immediate returns null");
  assert(
    assessmentSubjectTag(_baseCurrent, _baseCurrent, "unchanged_evidence_no_material_change") === null,
    "unchanged_evidence_no_material_change returns null",
  );
  assert(
    assessmentSubjectTag(_baseCurrent, _baseCurrent, "cooldown_no_material_change") === null,
    "cooldown_no_material_change returns null",
  );
});
