export interface ScoringStateRow {
  score: number | null | undefined;
  score_with_bonus?: number | null;
  band_with_bonus?: string | null;
  dimensions?: Record<string, unknown> | null;
  extensions?: Record<string, unknown> | null;
}

type ScoringMeta = {
  requires_resolution?: boolean;
};

function asObject(
  value: unknown,
): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

/**
 * Inverse of Python's dimensions_with_provenance. The reactor reads
 * signals.dimensions (envelope shape written by signal_resolver) and POSTs it
 * to the rubric_apply_caps Modal endpoint. Without flattening, apply_auto_caps
 * sees {dim: {value: int}} — litigation raises TypeError on `dict < int`,
 * merger_arb silently evaluates False. Mirrors
 * modal_workers/shared/rubric_engine.py::flatten_persisted_dimensions.
 */
export function flattenPersistedDimensions(
  dims: Record<string, unknown> | null | undefined,
): Record<string, number> {
  const out: Record<string, number> = {};
  for (const [k, v] of Object.entries(dims ?? {})) {
    if (k.startsWith("_")) continue;
    if (typeof v === "boolean") continue;
    if (typeof v === "number" && Number.isFinite(v)) {
      out[k] = Math.trunc(v);
      continue;
    }
    if (v && typeof v === "object" && !Array.isArray(v)) {
      const inner = (v as Record<string, unknown>)["value"];
      if (typeof inner === "boolean") continue;
      if (typeof inner === "number" && Number.isFinite(inner)) {
        out[k] = Math.trunc(inner);
      }
    }
  }
  return out;
}

export function scoringProvenance(
  row: ScoringStateRow | null | undefined,
): string | null {
  const dims = asObject(row?.dimensions);
  const provenance = dims?.["_provenance"];
  return typeof provenance === "string" ? provenance : null;
}

export function scoringMeta(
  row: ScoringStateRow | null | undefined,
): ScoringMeta {
  const extensions = asObject(row?.extensions);
  const meta = asObject(extensions?.["scoring_meta"]);
  return (meta ?? {}) as ScoringMeta;
}

export interface ProvisionalClassification {
  provisional: boolean;
  /**
   * True when the row declares `_provenance='heuristic'` on dimensions but
   * `extensions.scoring_meta` is missing. Without the sidecar, the reactor
   * cannot trust `requires_resolution`, so the row is treated as provisional
   * and routed to signal_resolver — AND an operator_flag is inserted because
   * this is a scanner/writer bug, not a normal payload shape.
   */
  malformed: boolean;
}

export function classifyProvisionalHeuristic(
  row: ScoringStateRow | null | undefined,
): ProvisionalClassification {
  if (scoringProvenance(row) !== "heuristic") {
    return { provisional: false, malformed: false };
  }
  const extensions = asObject(row?.extensions);
  const metaObj = asObject(extensions?.["scoring_meta"]);
  if (metaObj === null) {
    return { provisional: true, malformed: true };
  }
  return {
    provisional: metaObj["requires_resolution"] === true,
    malformed: false,
  };
}

export function isProvisionalHeuristic(
  row: ScoringStateRow | null | undefined,
): boolean {
  return classifyProvisionalHeuristic(row).provisional;
}

export function shouldProcessUpdate(
  nextRow: ScoringStateRow,
  previousRow: ScoringStateRow | null | undefined,
): boolean {
  const becameScored =
    (previousRow?.score ?? null) === null &&
    (nextRow?.score ?? null) !== null;
  if (becameScored) return true;

  const oldNeedsResolution = isProvisionalHeuristic(previousRow);
  const newNeedsResolution = isProvisionalHeuristic(nextRow);
  if (oldNeedsResolution && !newNeedsResolution) return true;

  const oldProv = scoringProvenance(previousRow);
  const newProv = scoringProvenance(nextRow);

  if (oldProv !== "ai_resolved" && newProv === "ai_resolved") return true;

  // ai_resolved → ai_resolved: the trigger WHEN clause fires on any
  // `dimensions->>'_provenance'` distinct-from change. A resolver re-write
  // (dim refinement) keeps provenance="ai_resolved" but Postgres still
  // fires the webhook. Re-enter ONLY if convergence never finished stamping
  // this row — score_with_bonus + band_with_bonus still NULL. Otherwise
  // this is a no-op refinement and full convergence would storm rubric_apply_caps.
  if (oldProv === "ai_resolved" && newProv === "ai_resolved") {
    const alreadyStamped =
      previousRow?.score_with_bonus != null &&
      previousRow?.band_with_bonus != null;
    return !alreadyStamped;
  }

  return false;
}
