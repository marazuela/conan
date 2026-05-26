// WI-2 — Tests for the binary-catalyst convergence pre-gate scorer.
//
// Run from repo root:
//   deno test supabase/functions/reactor/bc-pregate.test.ts --no-check

import {
  BC_PREGATE_DEFAULT_THRESHOLD,
  BC_PREGATE_MAX_SCORE_V1,
  BC_PREGATE_MAX_SCORE_V2,
  BC_PREGATE_WEIGHTS,
  classPrecedentFromApprovalRate,
  configFlagBool,
  configThreshold,
  inputsFromRawPayload,
  normalizeClassField,
  scoreBcPregate,
  type BcPregateInputs,
} from "./bc-pregate.ts";
import {
  buildOrchestratorRunInsert,
} from "./orchestrator-enqueue.ts";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

function inputs(over: Partial<BcPregateInputs> = {}): BcPregateInputs {
  return {
    breakthrough_designation: false,
    first_time_sponsor: false,
    class_precedent: 0,
    enrichment_state: "ready",
    ...over,
  };
}

// ---------------------------------------------------------------------------
// Case 1 — all three signals fire at boundary → pass
// ---------------------------------------------------------------------------

Deno.test("all-fire scores at v1 max and passes", () => {
  const result = scoreBcPregate(inputs({
    breakthrough_designation: true,
    first_time_sponsor: true,
    class_precedent: 0, // stubbed in v1
  }));
  // v1 max = BT(+6) + sponsor(+4) + class_precedent(0) = 10. When the
  // class_precedent refresher table lands and emits class_precedent=1, the
  // composite will hit 15 — at which point we lift threshold to 9.
  assert(result.score === BC_PREGATE_MAX_SCORE_V1, `expected max=10, got ${result.score}`);
  assert(result.passed === true, "all-three-fire must pass");
  assert(result.reasons.length === 0, "passed case carries no reasons");
});

// ---------------------------------------------------------------------------
// Case 2 — two of three fire at boundary → still passes (threshold 6)
// ---------------------------------------------------------------------------

Deno.test("breakthrough alone scores 6, passes at boundary", () => {
  const result = scoreBcPregate(inputs({
    breakthrough_designation: true,
  }));
  assert(result.score === BC_PREGATE_WEIGHTS.breakthrough_designation,
    `breakthrough alone = ${BC_PREGATE_WEIGHTS.breakthrough_designation}`);
  assert(result.passed === true, "boundary score must pass (>=)");
});

Deno.test("breakthrough + first-time sponsor = 10, comfortable pass", () => {
  const result = scoreBcPregate(inputs({
    breakthrough_designation: true,
    first_time_sponsor: true,
  }));
  assert(result.score === 10, `expected 10, got ${result.score}`);
  assert(result.passed === true, "two-fires must pass");
});

// ---------------------------------------------------------------------------
// Case 3 — one fire below threshold → decline
// ---------------------------------------------------------------------------

Deno.test("first-time sponsor alone scores 4, declines (below threshold)", () => {
  const result = scoreBcPregate(inputs({
    first_time_sponsor: true,
  }));
  assert(result.score === BC_PREGATE_WEIGHTS.first_time_sponsor,
    `sponsor alone = ${BC_PREGATE_WEIGHTS.first_time_sponsor}`);
  assert(result.passed === false, "score 4 < threshold 6 must decline");
  // Reasons should call out which signals failed to fire so operators can
  // distinguish "scored low because of A" from "scored low because of B".
  assert(result.reasons.includes("no_breakthrough_designation"),
    "decline reasons must include no_breakthrough_designation");
  assert(result.reasons.includes("class_precedent_unknown"),
    "decline reasons must include class_precedent_unknown (v1 stub)");
});

Deno.test("zero-fire scores 0, declines with all reasons", () => {
  const result = scoreBcPregate(inputs());
  assert(result.score === 0, "zero inputs scores 0");
  assert(result.passed === false, "must decline");
  assert(result.reasons.includes("no_breakthrough_designation"));
  assert(result.reasons.includes("sponsor_has_prior_p3"));
  assert(result.reasons.includes("class_precedent_unknown"));
});

// ---------------------------------------------------------------------------
// Case 4 — enrichment_pending stub → auto-decline with specific reason
// ---------------------------------------------------------------------------

Deno.test("enrichment_state='stub' auto-declines with enrichment_pending reason", () => {
  const result = scoreBcPregate(inputs({
    breakthrough_designation: true,
    first_time_sponsor: true,
    enrichment_state: "stub",
  }));
  assert(result.passed === false, "stub asset must decline regardless of designation flags");
  assert(result.score === 0, "stub asset short-circuits before scoring");
  assert(result.reasons.length === 1 && result.reasons[0] === "enrichment_pending",
    "stub asset emits exactly enrichment_pending reason — re-dispatch path picks up the row when enrichment completes");
});

Deno.test("enrichment_state='unavailable' declines with enrichment_unavailable", () => {
  const result = scoreBcPregate(inputs({
    breakthrough_designation: true,
    enrichment_state: "unavailable",
  }));
  assert(result.passed === false);
  assert(result.reasons[0] === "enrichment_unavailable");
});

// ---------------------------------------------------------------------------
// Case 5 — class_precedent post-refresher (future state)
// ---------------------------------------------------------------------------

Deno.test("class_precedent=1 lifts score to v2 max=15", () => {
  // With the refresher table populated, class_precedent ∈[0..1] is the
  // approval_rate from fda_class_precedent_base_rates. The per-unit
  // multiplier (5) lifts the max composite to 15.
  const result = scoreBcPregate(inputs({
    breakthrough_designation: true,
    first_time_sponsor: true,
    class_precedent: 1.0,
  }));
  assert(result.score === BC_PREGATE_MAX_SCORE_V2,
    `BT(6) + sponsor(4) + class(1*5)=15, got ${result.score}`);
  assert(result.passed === true);
});

Deno.test("class_precedent fractional values weight proportionally", () => {
  const result = scoreBcPregate(inputs({ class_precedent: 0.4 }));
  // 0 + 0 + 0.4*5 = 2 (still below threshold)
  assert(result.score === 2, `expected 2, got ${result.score}`);
  assert(result.passed === false);
});

// ---------------------------------------------------------------------------
// Payload interpretation — reactor must score the triggering document's signal
// payload, not the latest entity-wide FDA signal.
// ---------------------------------------------------------------------------

Deno.test("inputsFromRawPayload reads designation and sponsor flags only from payload", () => {
  const parsed = inputsFromRawPayload({
    breakthrough_designation: true,
    first_time_sponsor: true,
    class_precedent: 0.4,
  });
  assert(parsed.breakthrough_designation === true);
  assert(parsed.first_time_sponsor === true);
  assert(parsed.class_precedent === 0.4);
  assert(parsed.enrichment_state === "ready");
});

Deno.test("inputsFromRawPayload defaults absent/invalid payload fields conservatively", () => {
  const parsed = inputsFromRawPayload({
    breakthrough_designation: "true",
    first_time_sponsor: 1,
    class_precedent: "1",
  });
  assert(parsed.breakthrough_designation === false);
  assert(parsed.first_time_sponsor === false);
  assert(parsed.class_precedent === 0);
});

// ---------------------------------------------------------------------------
// Case 6 — custom threshold (post-refresher tuning)
// ---------------------------------------------------------------------------

Deno.test("custom threshold 9 separates two-fires (10) from one-fire (6)", () => {
  const tightSetup = { breakthrough_designation: true, first_time_sponsor: true };
  assert(scoreBcPregate(inputs(tightSetup), 9).passed === true, "10 >= 9 passes");
  assert(scoreBcPregate(inputs({ breakthrough_designation: true }), 9).passed === false,
    "6 < 9 declines under stricter threshold");
});

// ---------------------------------------------------------------------------
// V2 max constant + class_precedent term math
// ---------------------------------------------------------------------------

Deno.test("BC_PREGATE_MAX_SCORE_V2 equals V1 max plus full class_precedent contribution", () => {
  // The V2 max is the v1 max (10) plus the per-unit weight (5) for
  // class_precedent=1.0. Locks the contract that refresher integration
  // tops out at exactly this number — any future weight tweaks need to
  // update both constants.
  assert(
    BC_PREGATE_MAX_SCORE_V2 ===
      BC_PREGATE_MAX_SCORE_V1 + BC_PREGATE_WEIGHTS.class_precedent_per_unit,
    `V2 max should equal V1 + class weight; got ${BC_PREGATE_MAX_SCORE_V2}`,
  );
});

// ---------------------------------------------------------------------------
// normalizeClassField — byte-for-byte mirror of the Python refresher
// ---------------------------------------------------------------------------

Deno.test("normalizeClassField lowercases and collapses whitespace", () => {
  assert(normalizeClassField("  GLP-1 Agonist  ") === "glp-1 agonist",
    "trim + lowercase");
  assert(normalizeClassField("anti-VEGF\tmAb") === "anti-vegf mab",
    "tab → space");
  assert(normalizeClassField("JAK   inhibitor") === "jak inhibitor",
    "collapse interior whitespace");
});

Deno.test("normalizeClassField returns empty for null/undefined/non-string", () => {
  assert(normalizeClassField(null) === "");
  assert(normalizeClassField(undefined) === "");
  assert(normalizeClassField(123) === "");
  assert(normalizeClassField("") === "");
  assert(normalizeClassField("   ") === "");
});

// ---------------------------------------------------------------------------
// classPrecedentFromApprovalRate — clamps DB values into the scorer range
// ---------------------------------------------------------------------------

Deno.test("classPrecedentFromApprovalRate passes valid [0..1] values through", () => {
  assert(classPrecedentFromApprovalRate(0) === 0);
  assert(classPrecedentFromApprovalRate(0.6) === 0.6);
  assert(classPrecedentFromApprovalRate(1) === 1);
});

Deno.test("classPrecedentFromApprovalRate clamps out-of-range values", () => {
  assert(classPrecedentFromApprovalRate(-0.5) === 0, "negative → 0");
  assert(classPrecedentFromApprovalRate(1.5) === 1, "above 1 → 1");
});

Deno.test("classPrecedentFromApprovalRate returns 0 for null/NaN/non-numeric", () => {
  assert(classPrecedentFromApprovalRate(null) === 0);
  assert(classPrecedentFromApprovalRate(undefined) === 0);
  assert(classPrecedentFromApprovalRate(NaN) === 0);
  assert(classPrecedentFromApprovalRate("0.5") === 0, "string → 0 (no coercion)");
});

// ---------------------------------------------------------------------------
// configFlagBool / configThreshold helpers
// ---------------------------------------------------------------------------

Deno.test("configFlagBool accepts true/1/yes case-insensitive", () => {
  for (const v of ["true", "TRUE", "True", "1", "yes", "YES"]) {
    assert(configFlagBool(v) === true, `${JSON.stringify(v)} should be true`);
  }
  for (const v of ["false", "0", "no", "", null, undefined, " disabled "]) {
    assert(configFlagBool(v) === false, `${JSON.stringify(v)} should be false`);
  }
});

Deno.test("configThreshold falls back to default on parse failure", () => {
  assert(configThreshold("6") === 6);
  assert(configThreshold("9.5") === 9.5);
  assert(configThreshold("not-a-number") === BC_PREGATE_DEFAULT_THRESHOLD);
  assert(configThreshold("") === BC_PREGATE_DEFAULT_THRESHOLD);
  assert(configThreshold(null) === BC_PREGATE_DEFAULT_THRESHOLD);
  assert(configThreshold("-3") === BC_PREGATE_DEFAULT_THRESHOLD,
    "negative thresholds fall back to default");
});

// ---------------------------------------------------------------------------
// EnqueueArgs contract: pre-gate fields plumb through buildOrchestratorRunInsert
// ---------------------------------------------------------------------------

Deno.test("legacy 4-arg buildOrchestratorRunInsert still produces the 5-tuple", () => {
  const row = buildOrchestratorRunInsert({
    asset_id: "00000000-0000-0000-0000-000000000001",
    trigger_type: "new_doc",
    trigger_doc_id: "00000000-0000-0000-0000-000000000002",
    document_set_hash: "abc123",
  });
  // No pre-gate args means no pre-gate keys leak in — preserves the existing
  // orchestrator-enqueue.test.ts contract.
  assert(!("bc_pregate_score" in row), "no bc_pregate_score on legacy path");
  assert(!("routine_declined" in row), "no routine_declined on legacy path");
  assert(row.status === "pending");
});

Deno.test("pre-gate fields plumb through to the orchestrator_runs insert row", () => {
  const row = buildOrchestratorRunInsert({
    asset_id: "00000000-0000-0000-0000-000000000001",
    trigger_type: "new_doc",
    trigger_doc_id: "00000000-0000-0000-0000-000000000002",
    document_set_hash: "abc123",
    bc_pregate_score: 10,
    bc_pregate_inputs: { breakthrough_designation: true },
    routine_declined: false,
  });
  assert(row.bc_pregate_score === 10);
  assert(row.routine_declined === false);
  assert(row.status === "pending", "passing score still produces pending status");
});

Deno.test("declined override sets status='declined' for the audit row", () => {
  const row = buildOrchestratorRunInsert({
    asset_id: "00000000-0000-0000-0000-000000000001",
    trigger_type: "new_doc",
    trigger_doc_id: "00000000-0000-0000-0000-000000000002",
    document_set_hash: "abc123",
    bc_pregate_score: 4,
    bc_pregate_inputs: { first_time_sponsor: true },
    routine_declined: true,
    decline_reasons: ["no_breakthrough_designation", "class_precedent_unknown"],
    status_override: "declined",
  });
  assert(row.status === "declined", "status_override flips to declined");
  assert(row.routine_declined === true);
  assert(row.decline_reasons?.[0] === "no_breakthrough_designation");
});
