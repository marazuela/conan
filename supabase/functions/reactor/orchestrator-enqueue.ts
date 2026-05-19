// v3 Stream 1 — pure helper for the orchestrator_runs row shape.
//
// Extracted from reactor/index.ts so the C1 contract (the row produced by
// the reactor for the v3 orchestrator_drain_queue Modal function to consume)
// can be unit-tested without booting the supabase client / env.
//
// Keep this file purely declarative — no Deno.env, no createClient, no fetch.

export interface EnqueueArgs {
  asset_id: string;
  trigger_type: "new_doc" | "cross_source" | "operator_refresh" | "market_move";
  trigger_doc_id: string | null;
}

export interface OrchestratorRunInsert {
  asset_id: string;
  trigger_type: EnqueueArgs["trigger_type"];
  trigger_doc_id: string | null;
  status: "pending";
}

export function buildOrchestratorRunInsert(args: EnqueueArgs): OrchestratorRunInsert {
  return {
    asset_id: args.asset_id,
    trigger_type: args.trigger_type,
    trigger_doc_id: args.trigger_doc_id,
    status: "pending",
  };
}
