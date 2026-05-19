// v3 Stream 1 — orchestrator_runs enqueue contract (C1).
//
// The reactor produces orchestrator_runs rows when an FDA-asset primary
// document lands. This test pins the row-shape contract that the v3
// orchestrator_drain_queue Modal function consumes. If this drifts, the
// drainer breaks; lock it here.

import { buildOrchestratorRunInsert } from "./orchestrator-enqueue.ts";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

Deno.test("buildOrchestratorRunInsert produces the canonical pending-row shape", () => {
  const row = buildOrchestratorRunInsert({
    asset_id: "00000000-0000-0000-0000-000000000001",
    trigger_type: "new_doc",
    trigger_doc_id: "00000000-0000-0000-0000-000000000002",
  });
  assert(row.asset_id === "00000000-0000-0000-0000-000000000001", "asset_id passes through");
  assert(row.trigger_type === "new_doc", "trigger_type passes through");
  assert(row.trigger_doc_id === "00000000-0000-0000-0000-000000000002", "trigger_doc_id passes through");
  assert(row.status === "pending", "status is always pending");
  // Row must contain exactly these 4 keys — the dedup index expects this exact
  // (asset_id, trigger_type, trigger_doc_id) trio plus status='pending' for the
  // partial-unique predicate. Extra keys would still insert OK, but adding any
  // is a contract change that should fail this test loudly.
  const keys = Object.keys(row).sort();
  assert(
    JSON.stringify(keys) === JSON.stringify(["asset_id", "status", "trigger_doc_id", "trigger_type"]),
    `row keys must be exactly the 4-tuple, got ${keys.join(",")}`,
  );
});

Deno.test("cross_source trigger_type is preserved (Tier 1 escalation marker)", () => {
  const row = buildOrchestratorRunInsert({
    asset_id: "11111111-1111-1111-1111-111111111111",
    trigger_type: "cross_source",
    trigger_doc_id: "22222222-2222-2222-2222-222222222222",
  });
  assert(row.trigger_type === "cross_source", "cross_source flows through");
});

Deno.test("trigger_doc_id null is allowed (operator_refresh path)", () => {
  const row = buildOrchestratorRunInsert({
    asset_id: "33333333-3333-3333-3333-333333333333",
    trigger_type: "operator_refresh",
    trigger_doc_id: null,
  });
  assert(row.trigger_doc_id === null, "null trigger_doc_id flows through (operator path)");
  assert(row.status === "pending", "status still pending on null-doc path");
});

Deno.test("market_move trigger_type is accepted (Tier 1 hot-path)", () => {
  const row = buildOrchestratorRunInsert({
    asset_id: "44444444-4444-4444-4444-444444444444",
    trigger_type: "market_move",
    trigger_doc_id: null,
  });
  assert(row.trigger_type === "market_move", "market_move flows through");
});
