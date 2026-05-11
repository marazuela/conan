import {
  classifyProvisionalHeuristic,
  flattenPersistedDimensions,
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

Deno.test("classifyProvisionalHeuristic flags heuristic provenance without scoring_meta as malformed", () => {
  const result = classifyProvisionalHeuristic({
    score: 30,
    dimensions: { approval_probability: 3, _provenance: "heuristic" },
    extensions: {}, // scoring_meta deliberately absent
  });
  assert(result.provisional === true, "missing scoring_meta must still route to resolver");
  assert(result.malformed === true, "missing scoring_meta must be flagged as malformed");
});

Deno.test("classifyProvisionalHeuristic does NOT flag malformed when scoring_meta exists", () => {
  const result = classifyProvisionalHeuristic({
    score: 30,
    dimensions: { approval_probability: 3, _provenance: "heuristic" },
    extensions: { scoring_meta: { requires_resolution: true } },
  });
  assert(result.provisional === true, "should still be provisional");
  assert(result.malformed === false, "well-formed scoring_meta should not trip malformed");
});

Deno.test("classifyProvisionalHeuristic returns malformed=false on non-heuristic rows", () => {
  const result = classifyProvisionalHeuristic({
    score: 37,
    dimensions: { approval_probability: 5, _provenance: "ai_resolved" },
    extensions: { scoring_meta: { requires_resolution: false } },
  });
  assert(result.provisional === false, "ai_resolved rows are not provisional");
  assert(result.malformed === false, "ai_resolved rows can't be malformed-heuristic");
});

Deno.test("classifyProvisionalHeuristic treats extensions=null as malformed when heuristic", () => {
  const result = classifyProvisionalHeuristic({
    score: 30,
    dimensions: { approval_probability: 3, _provenance: "heuristic" },
    extensions: null,
  });
  assert(result.provisional === true, "should route to resolver");
  assert(result.malformed === true, "null extensions with heuristic provenance is malformed");
});

Deno.test("shouldProcessUpdate fires on score-value change after initial scoring", () => {
  assert(
    shouldProcessUpdate(
      {
        score: 38,
        dimensions: { approval_probability: 5, _provenance: "ai_resolved" },
        extensions: { scoring_meta: { requires_resolution: false } },
      },
      {
        score: 22,
        dimensions: { approval_probability: 3, _provenance: "ai_resolved" },
        extensions: { scoring_meta: { requires_resolution: false } },
      },
    ) === true,
    "score-value change must re-enter convergence even when provenance is unchanged",
  );
});

Deno.test("flattenPersistedDimensions strips _provenance key", () => {
  const out = flattenPersistedDimensions({ _provenance: "ai_resolved" });
  assert(Object.keys(out).length === 0, "expected empty dict");
});

Deno.test("flattenPersistedDimensions passes flat ints through", () => {
  const out = flattenPersistedDimensions({ spread_size: 5, liquidity: 3 });
  assert(out.spread_size === 5 && out.liquidity === 3, "flat ints unchanged");
});

Deno.test("flattenPersistedDimensions extracts value from envelope", () => {
  const out = flattenPersistedDimensions({
    party_resolution_confidence: { value: 2, provenance: "ai_resolved" },
    financial_materiality: { value: 5, provenance: "ai_resolved" },
    _provenance: "ai_resolved",
  });
  assert(
    out.party_resolution_confidence === 2 && out.financial_materiality === 5,
    "envelope values should be extracted",
  );
  assert(!("_provenance" in out), "_provenance key should not leak through");
});

Deno.test("flattenPersistedDimensions handles mixed envelope and flat", () => {
  const out = flattenPersistedDimensions({
    party_resolution_confidence: { value: 1, provenance: "ai_resolved" },
    legacy_flat_dim: 4,
    _provenance: "mixed",
  });
  assert(out.party_resolution_confidence === 1, "envelope extracted");
  assert(out.legacy_flat_dim === 4, "flat int preserved");
});

Deno.test("flattenPersistedDimensions truncates floats to int", () => {
  const out = flattenPersistedDimensions({
    a: 3.7,
    b: { value: 3.2, provenance: "x" },
  });
  assert(out.a === 3 && out.b === 3, "floats must truncate");
});

Deno.test("flattenPersistedDimensions drops bools", () => {
  const out = flattenPersistedDimensions({ foo: true, bar: false, baz: { value: true } });
  assert(Object.keys(out).length === 0, "bools must drop");
});

Deno.test("flattenPersistedDimensions handles empty/null input", () => {
  assert(Object.keys(flattenPersistedDimensions({})).length === 0, "empty");
  assert(Object.keys(flattenPersistedDimensions(null)).length === 0, "null");
  assert(Object.keys(flattenPersistedDimensions(undefined)).length === 0, "undefined");
});

Deno.test("flattenPersistedDimensions drops envelope entries without a value key", () => {
  const out = flattenPersistedDimensions({ foo: { provenance: "x" } });
  assert(Object.keys(out).length === 0, "missing value key drops");
});

Deno.test("flattenPersistedDimensions is idempotent", () => {
  const once = flattenPersistedDimensions({
    foo: { value: 2, provenance: "ai_resolved" },
    _provenance: "ai_resolved",
  });
  const twice = flattenPersistedDimensions(once);
  assert(once.foo === 2 && twice.foo === 2, "idempotent extraction");
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

Deno.test("shouldProcessUpdate skips ai_resolved -> ai_resolved when already stamped", () => {
  const row = {
    score: 37,
    score_with_bonus: 42,
    band_with_bonus: "immediate",
    dimensions: { approval_probability: 5, _provenance: "ai_resolved" },
    extensions: { scoring_meta: { requires_resolution: false } },
  };
  assert(
    shouldProcessUpdate(row, row) === false,
    "already-stamped ai_resolved rows must not storm rubric_apply_caps",
  );
});

Deno.test("shouldProcessUpdate re-enters ai_resolved -> ai_resolved when convergence never stamped", () => {
  const prev = {
    score: 37,
    score_with_bonus: null,
    band_with_bonus: null,
    dimensions: { approval_probability: 5, _provenance: "ai_resolved" },
    extensions: { scoring_meta: { requires_resolution: false } },
  };
  const next = {
    score: 37,
    score_with_bonus: null,
    band_with_bonus: null,
    dimensions: { approval_probability: 4, _provenance: "ai_resolved" },
    extensions: { scoring_meta: { requires_resolution: false } },
  };
  assert(
    shouldProcessUpdate(next, prev) === true,
    "resolver refinement on an un-stamped row must finish convergence",
  );
});

Deno.test("shouldProcessUpdate re-enters heuristic -> ai_resolved even when already stamped", () => {
  // score_with_bonus shouldn't exist pre-resolution, but if it does (analyst
  // backfill, stale data), the heuristic->ai_resolved branch still takes
  // precedence — the persisted rubric output must reflect the resolved dims.
  const prev = {
    score: 30,
    score_with_bonus: 30,
    band_with_bonus: "watchlist",
    dimensions: { approval_probability: 3, _provenance: "heuristic" },
    extensions: { scoring_meta: { requires_resolution: false } },
  };
  const next = {
    score: 37,
    score_with_bonus: null,
    band_with_bonus: null,
    dimensions: { approval_probability: 5, _provenance: "ai_resolved" },
    extensions: { scoring_meta: { requires_resolution: false } },
  };
  assert(
    shouldProcessUpdate(next, prev) === true,
    "heuristic->ai_resolved transition always re-enters, regardless of stamp state",
  );
});
