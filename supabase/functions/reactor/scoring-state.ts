export interface ScoringStateRow {
  score: number | null | undefined;
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

export function isProvisionalHeuristic(
  row: ScoringStateRow | null | undefined,
): boolean {
  return (
    scoringProvenance(row) === "heuristic" &&
    scoringMeta(row).requires_resolution === true
  );
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
  return oldProv !== "ai_resolved" && newProv === "ai_resolved";
}
