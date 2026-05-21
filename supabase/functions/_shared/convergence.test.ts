import { assertEquals } from "https://deno.land/std@0.224.0/assert/mod.ts";
import { classifyGroup, pickWinner } from "./convergence.ts";

// pickWinner determinism: highest score wins, signal_id ASC breaks ties.
// Matches modal_workers/shared/rubric_engine.py _pick_winner so the Python
// convergence_qa reference and the TS reactor stamp agree.
Deno.test("pickWinner: highest score wins outright", () => {
  const winner = pickWinner([
    { signal_id: "s1", scoring_profile: "merger_arb", thesis_direction: "long", score: 22, source_content_hash: "h1" },
    { signal_id: "s2", scoring_profile: "merger_arb", thesis_direction: "long", score: 31, source_content_hash: "h2" },
  ]);
  assertEquals(winner.signal_id, "s2");
});

Deno.test("pickWinner: ties resolve to lowest signal_id (forward order)", () => {
  const winner = pickWinner([
    { signal_id: "s_alpha", scoring_profile: "merger_arb", thesis_direction: "long", score: 30, source_content_hash: "h-alpha" },
    { signal_id: "s_beta", scoring_profile: "merger_arb", thesis_direction: "long", score: 30, source_content_hash: "h-beta" },
  ]);
  assertEquals(winner.signal_id, "s_alpha");
});

Deno.test("pickWinner: ties resolve to lowest signal_id (reverse order)", () => {
  const winner = pickWinner([
    { signal_id: "s_beta", scoring_profile: "merger_arb", thesis_direction: "long", score: 30, source_content_hash: "h-beta" },
    { signal_id: "s_alpha", scoring_profile: "merger_arb", thesis_direction: "long", score: 30, source_content_hash: "h-alpha" },
  ]);
  assertEquals(winner.signal_id, "s_alpha");
});

Deno.test("classifyGroup: tie winner is the deterministic signal_id-min", () => {
  const result = classifyGroup([
    { signal_id: "s_z", scoring_profile: "merger_arb", thesis_direction: "long", score: 28, source_content_hash: "hz" },
    { signal_id: "s_a", scoring_profile: "activist_governance", thesis_direction: "long", score: 28, source_content_hash: "ha" },
  ]);
  // Both scores tie at 28 → orthogonal direction-coherent group, winner_signal_id
  // must be s_a (the lower-sorting signal_id) regardless of input order.
  assertEquals(result.bonus, 5);
  assertEquals(result.type, "orthogonal");
  assertEquals(result.winner_signal_id, "s_a");
});
