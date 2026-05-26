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

export const ASSESSMENT_EMAIL_COOLDOWN_HOURS = 24;
export const ASSESSMENT_EMAIL_MATERIAL_CONVICTION_DELTA = 5;

export interface AssessmentEmailState {
  asset_id: string;
  band?: string | null;
  gate_status?: string | null;
  alert_gate_status?: string | null;
  alert_gate_reasons?: string[] | null;
  constitutional_pass?: boolean | null;
  document_set_hash?: string | null;
  thesis_direction?: string | null;
  conviction_pct?: number | null;
  conviction_pct_calibrated?: number | null;
  ensemble_dispersion?: number | null;
  evidence_quality?: number | null;
  expected_value_bps?: number | null;
  target_type?: string | null;
  label_rule?: string | null;
  created_at?: string | null;
}

export interface AssessmentEmailGateDecision {
  send: boolean;
  reason: string;
}

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

export function assessmentConvictionValue(row: AssessmentEmailState): number | null {
  const value = row.conviction_pct_calibrated ?? row.conviction_pct;
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/**
 * Compose a short, human-parseable subject-line tag describing why this
 * email is being sent. Pairs with `shouldSendAssessmentImmediateEmail` —
 * the same `reason` produced by the gate drives the tag, so the recipient
 * can tell at a glance whether an email is a NEW signal, a direction flip,
 * a conviction delta, or just a scheduled refresh.
 *
 * Examples (the caller prepends/appends to `[IMMEDIATE]` as desired):
 *   NEW                — first time we are emailing this asset to this recipient
 *   DIRECTION CHANGE   — thesis_direction flipped
 *   Δ+12pp / Δ−8pp     — conviction delta vs prior email
 *   NEW EVIDENCE       — document_set_hash changed
 *   REFRESH            — cooldown elapsed, no material change
 *
 * Returns null when the gate decision was `send=false` (caller should not
 * have called this at all — defensive return so we never produce a bare
 * "[IMMEDIATE]" tag without context).
 */
export function assessmentSubjectTag(
  current: AssessmentEmailState,
  prior: AssessmentEmailState | null,
  reason: string,
): string | null {
  if (reason === "first_assessment_for_recipient_asset" || reason === "different_asset") {
    return "NEW";
  }
  if (reason === "direction_changed") {
    return "DIRECTION CHANGE";
  }
  if (reason === "conviction_changed") {
    const currentV = assessmentConvictionValue(current);
    const priorV = prior ? assessmentConvictionValue(prior) : null;
    if (currentV !== null && priorV !== null) {
      const delta = Math.round(currentV - priorV);
      const sign = delta >= 0 ? "+" : "";
      return `Δ${sign}${delta}pp`;
    }
    return "CONVICTION CHANGE";
  }
  if (reason === "evidence_changed") {
    return "NEW EVIDENCE";
  }
  if (reason === "cooldown_elapsed_or_unknown_evidence") {
    return "REFRESH";
  }
  // `not_immediate`, `unchanged_evidence_no_material_change`, `cooldown_no_material_change`
  // all imply send=false — caller should not have invoked the tag helper.
  return null;
}

export function shouldSendAssessmentImmediateEmail(
  current: AssessmentEmailState,
  prior: AssessmentEmailState | null,
  now: Date = new Date(),
): AssessmentEmailGateDecision {
  if (current.band !== "immediate") {
    return { send: false, reason: "not_immediate" };
  }
  if (current.gate_status !== undefined && current.gate_status !== null && current.gate_status !== "pass") {
    return { send: false, reason: "gate_status_not_pass" };
  }
  if (current.alert_gate_status !== undefined && current.alert_gate_status !== null && current.alert_gate_status !== "pass") {
    return {
      send: false,
      reason: current.alert_gate_reasons?.[0] ?? "alert_gate_suppressed",
    };
  }
  if (current.constitutional_pass !== undefined && current.constitutional_pass !== null && current.constitutional_pass !== true) {
    return { send: false, reason: "constitutional_not_passed" };
  }
  if (current.target_type !== undefined && !current.target_type) {
    return { send: false, reason: "missing_prediction_target" };
  }
  if (current.label_rule !== undefined && !current.label_rule) {
    return { send: false, reason: "missing_label_rule" };
  }
  if (
    current.evidence_quality !== undefined &&
    current.evidence_quality !== null &&
    current.evidence_quality < 0.45
  ) {
    return { send: false, reason: "low_evidence_quality" };
  }
  if (
    current.ensemble_dispersion !== undefined &&
    current.ensemble_dispersion !== null &&
    current.ensemble_dispersion > 15
  ) {
    return { send: false, reason: "high_ensemble_dispersion" };
  }
  if (
    current.expected_value_bps !== undefined &&
    (current.expected_value_bps === null || current.expected_value_bps <= 0)
  ) {
    return { send: false, reason: "non_positive_expected_value" };
  }
  if (!prior) {
    return { send: true, reason: "first_assessment_for_recipient_asset" };
  }
  if (prior.asset_id !== current.asset_id) {
    return { send: true, reason: "different_asset" };
  }

  const currentDirection = current.thesis_direction ?? null;
  const priorDirection = prior.thesis_direction ?? null;
  if (currentDirection && priorDirection && currentDirection !== priorDirection) {
    return { send: true, reason: "direction_changed" };
  }

  const currentConviction = assessmentConvictionValue(current);
  const priorConviction = assessmentConvictionValue(prior);
  if (
    currentConviction !== null &&
    priorConviction !== null &&
    Math.abs(currentConviction - priorConviction) >= ASSESSMENT_EMAIL_MATERIAL_CONVICTION_DELTA
  ) {
    return { send: true, reason: "conviction_changed" };
  }

  const currentHash = current.document_set_hash ?? null;
  const priorHash = prior.document_set_hash ?? null;
  if (currentHash && priorHash) {
    if (currentHash !== priorHash) {
      return { send: true, reason: "evidence_changed" };
    }
    return { send: false, reason: "unchanged_evidence_no_material_change" };
  }

  const priorCreatedAt = prior.created_at ? Date.parse(prior.created_at) : NaN;
  if (Number.isFinite(priorCreatedAt)) {
    const ageMs = now.getTime() - priorCreatedAt;
    const cooldownMs = ASSESSMENT_EMAIL_COOLDOWN_HOURS * 60 * 60 * 1000;
    if (ageMs >= 0 && ageMs < cooldownMs) {
      return { send: false, reason: "cooldown_no_material_change" };
    }
  }

  return { send: true, reason: "cooldown_elapsed_or_unknown_evidence" };
}
