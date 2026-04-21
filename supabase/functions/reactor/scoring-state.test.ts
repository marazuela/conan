import {
  isProvisionalHeuristic,
  scoringMeta,
  scoringProvenance,
  shouldProcessUpdate,
} from "./scoring-state.ts";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

Deno.test("scoringProvenance reads persisted dimension provenance", () => {
  assert(
    scoringProvenance({ score: 30, dimensions: { approval_probability: 3, _provenance: "heuristic" } }) ===
      "heuristic",
    "expected heuristic provenance",
  );
  assert(
    scoringProvenance({ score: 30, dimensions: { approval_probability: 3 } }) === null,
    "missing provenance should return null",
  );
});

Deno.test("scoringMeta reads scoring_meta from extensions", () => {
  const meta = scoringMeta({
    score: 30,
    extensions: {
      scoring_meta: {
        provenance: "heuristic",
        requires_resolution: true,
      },
    },
  });
  assert(meta.requires_resolution === true, "expected requires_resolution=true");
});

Deno.test("isProvisionalHeuristic requires both heuristic provenance and resolution flag", () => {
  assert(
    isProvisionalHeuristic({
      score: 30,
      dimensions: { approval_probability: 3, _provenance: "heuristic" },
      extensions: { scoring_meta: { requires_resolution: true } },
    }) === true,
    "heuristic rows with unresolved defaults should be provisional",
  );
  assert(
    isProvisionalHeuristic({
      score: 30,
      dimensions: { approval_probability: 3, _provenance: "heuristic" },
      extensions: { scoring_meta: { requires_resolution: false } },
    }) === false,
    "resolved heuristic rows should not stay provisional",
  );
});

Deno.test("shouldProcessUpdate still accepts score-null to score-filled transition", () => {
  assert(
    shouldProcessUpdate(
      { score: 30, dimensions: { approval_probability: 3, _provenance: "heuristic" } },
      { score: null, dimensions: {} },
    ) === true,
    "NULL->non-NULL score transition must still re-enter reactor",
  );
});

Deno.test("shouldProcessUpdate accepts heuristic to ai_resolved transition", () => {
  assert(
    shouldProcessUpdate(
      {
        score: 37,
        dimensions: { approval_probability: 5, _provenance: "ai_resolved" },
        extensions: { scoring_meta: { requires_resolution: false } },
      },
      {
        score: 30,
        dimensions: { approval_probability: 3, _provenance: "heuristic" },
        extensions: { scoring_meta: { requires_resolution: true } },
      },
    ) === true,
    "resolved rows must re-enter convergence processing",
  );
});

Deno.test("shouldProcessUpdate ignores reactor self-writes", () => {
  assert(
    shouldProcessUpdate(
      {
        score: 30,
        dimensions: { approval_probability: 3, _provenance: "heuristic" },
        extensions: { scoring_meta: { requires_resolution: true } },
      },
      {
        score: 30,
        dimensions: { approval_probability: 3, _provenance: "heuristic" },
        extensions: { scoring_meta: { requires_resolution: true } },
      },
    ) === false,
    "unchanged heuristic rows should not loop reactor on its own stamping updates",
  );
});
