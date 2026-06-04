// WI-2 — Binary-catalyst convergence pre-gate scorer.
//
// Ported from v2_skills/detect-binary-catalyst-convergence. The gate sits in
// processAssetDocument() between the content-dedup check and the
// enqueueOrchestratorRun() call so we don't burn an orchestrator slot on an
// asset that lacks the structural quality signals v2 uses to dispatch.
//
// V1 scoring (max composite = 10):
//   Breakthrough designation                    +6
//   First-time sponsor (no prior P3 NDA / BLA)  +4
//   Class precedent (stubbed to 0)
//
// V2 scoring (max composite = 15) — active once `fda_class_precedent_base_rates`
// is populated by `bc_class_precedent_refresher.py`:
//   Class precedent term = approval_rate (∈[0..1]) * 5
// At that point operator should bump `internal_config.bc_pregate_threshold`
// from 6 to 9. Threshold lives in config so the cutover is a single SQL flip.
//
// Pure-data helpers in this file have no Deno imports — keep it that way so
// they can be unit-tested via Deno's test runner without env wiring.

export interface BcPregateInputs {
  breakthrough_designation: boolean;
  // FDA Priority Review on the in-flight submission. Sourced from 8-K
  // extracted_facts (openFDA drugsfda has no application_number for a pending
  // pre-PDUFA app), hydrated onto fda_assets by enrich_fda_asset_designations.py.
  priority_review: boolean;
  first_time_sponsor: boolean;
  // approval_rate from fda_class_precedent_base_rates, ∈[0..1]. 0 = no row in
  // the refresher table OR n_total<threshold; the gate scores the term as 0.
  class_precedent: number;
  enrichment_state: "ready" | "stub" | "unavailable";
}

export interface BcPregateScore {
  score: number;
  inputs: BcPregateInputs;
  reasons: string[];   // populated on decline (or shadow-decline)
  passed: boolean;     // score >= threshold AND enrichment_state='ready'
}

// V1 weights — keep in lockstep with v2_skills/detect-binary-catalyst-convergence.
export const BC_PREGATE_WEIGHTS = {
  breakthrough_designation: 6,
  first_time_sponsor: 4,
  priority_review: 3,
  class_precedent_per_unit: 5, // multiplier for the (currently-stubbed) class precedent input
} as const;

export const BC_PREGATE_MAX_SCORE_V1 = 13; // BT+6 + sponsor+4 + priority+3; class_precedent stubbed to 0
export const BC_PREGATE_MAX_SCORE_V2 = 18; // adds class_precedent * 5 (refresher-populated)
export const BC_PREGATE_DEFAULT_THRESHOLD = 4;

/**
 * Canonicalize a mechanism-of-action or indication string for lookup against
 * `fda_class_precedent_base_rates`. Mirrors `normalize_class_field()` in
 * `modal_workers/scripts/bc_class_precedent_refresher.py` byte-for-byte so
 * the reactor reads rows the refresher wrote.
 *
 * Steps: trim, lowercase, collapse interior whitespace.
 */
export function normalizeClassField(value: unknown): string {
  if (typeof value !== "string") return "";
  return value.trim().toLowerCase().split(/\s+/).filter(Boolean).join(" ");
}

/**
 * Clamp a raw `approval_rate` row value into the [0..1] range expected by
 * `inputsFromRawPayload.class_precedent`. NULL / non-finite → 0 so the gate
 * treats sparse/missing classes as "no precedent" (the safe default).
 */
export function classPrecedentFromApprovalRate(rate: unknown): number {
  if (typeof rate !== "number" || !Number.isFinite(rate)) return 0;
  if (rate < 0) return 0;
  if (rate > 1) return 1;
  return rate;
}

/**
 * Convert an FDA-family signal raw_payload into pre-gate inputs. Kept pure so
 * the reactor can unit-test payload interpretation separately from Supabase I/O.
 */
export function inputsFromRawPayload(rawPayload: Record<string, unknown>): BcPregateInputs {
  const classPrecedent = rawPayload["class_precedent"];
  return {
    breakthrough_designation: rawPayload["breakthrough_designation"] === true,
    priority_review: rawPayload["priority_review"] === true,
    first_time_sponsor: rawPayload["first_time_sponsor"] === true,
    class_precedent: typeof classPrecedent === "number" && Number.isFinite(classPrecedent)
      ? classPrecedent
      : 0,
    enrichment_state: "ready",
  };
}

/**
 * Pure scoring function. Input shape mirrors what evaluateBcPreGate() collects
 * from fda_pdufa_pipeline + fda_assets at gate time. No I/O.
 */
export function scoreBcPregate(
  inputs: BcPregateInputs,
  threshold: number = BC_PREGATE_DEFAULT_THRESHOLD,
): BcPregateScore {
  const reasons: string[] = [];

  // FAIL-OPEN on missing data. The gate exists to skip OBVIOUSLY low-quality
  // catalysts (established sponsor, no designation), which we can only judge for
  // an enriched asset. An un-enriched ("stub") or missing ("unavailable") asset
  // is something we CANNOT judge, so we pass it rather than decline — a false
  // decline misses a real FDA catalyst (high cost), a false pass burns one extra
  // orchestrator run (cents). Once enrich_fda_asset_designations.py hydrates the
  // asset it gets scored normally on the next dispatch.
  if (inputs.enrichment_state === "stub") {
    return {
      score: 0,
      inputs,
      reasons: ["enrichment_pending_fail_open"],
      passed: true,
    };
  }
  if (inputs.enrichment_state === "unavailable") {
    return {
      score: 0,
      inputs,
      reasons: ["enrichment_unavailable_fail_open"],
      passed: true,
    };
  }

  let score = 0;
  if (inputs.breakthrough_designation) {
    score += BC_PREGATE_WEIGHTS.breakthrough_designation;
  } else {
    reasons.push("no_breakthrough_designation");
  }
  if (inputs.first_time_sponsor) {
    score += BC_PREGATE_WEIGHTS.first_time_sponsor;
  } else {
    reasons.push("sponsor_has_prior_p3");
  }
  if (inputs.priority_review) {
    score += BC_PREGATE_WEIGHTS.priority_review;
  } else {
    reasons.push("no_priority_review");
  }
  // class_precedent is a numeric input in [0..1] (or null/0 in v1 stub). When
  // the refresher table lands and class_precedent > 0, weight by the per-unit
  // multiplier. v1 stub always emits 0 so this term is 0.
  if (inputs.class_precedent > 0) {
    score += inputs.class_precedent * BC_PREGATE_WEIGHTS.class_precedent_per_unit;
  } else {
    reasons.push("class_precedent_unknown");
  }

  const passed = score >= threshold;
  return {
    score,
    inputs,
    reasons: passed ? [] : reasons,
    passed,
  };
}

/**
 * Helper: parses a string config value into a boolean. Treats 'true' / '1' /
 * 'yes' (case-insensitive) as true; everything else as false. Matches the
 * convention used by internal_config flag readers elsewhere in the project.
 */
export function configFlagBool(v: string | null | undefined): boolean {
  if (v === null || v === undefined) return false;
  return /^(true|1|yes)$/i.test(v.trim());
}

/**
 * Helper: parses a string config value into a positive integer threshold,
 * falling back to BC_PREGATE_DEFAULT_THRESHOLD on parse failure.
 */
export function configThreshold(v: string | null | undefined): number {
  if (v === null || v === undefined) return BC_PREGATE_DEFAULT_THRESHOLD;
  const trimmed = v.trim();
  if (trimmed === "") return BC_PREGATE_DEFAULT_THRESHOLD;
  const n = Number(trimmed);
  if (!Number.isFinite(n) || n < 0) return BC_PREGATE_DEFAULT_THRESHOLD;
  return n;
}
