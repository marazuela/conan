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
  document_set_hash?: string | null;
}

export interface OrchestratorRunInsert {
  asset_id: string;
  trigger_type: EnqueueArgs["trigger_type"];
  trigger_doc_id: string | null;
  status: "pending";
  document_set_hash: string | null;
}

export function buildOrchestratorRunInsert(args: EnqueueArgs): OrchestratorRunInsert {
  return {
    asset_id: args.asset_id,
    trigger_type: args.trigger_type,
    trigger_doc_id: args.trigger_doc_id,
    status: "pending",
    document_set_hash: args.document_set_hash ?? null,
  };
}

// Bypass set: enqueues with these trigger_types skip the content-dedup check.
// Operator-initiated (manual / operator_refresh) and system-initiated outside
// the doc bus (tier2_escalation, catalyst_proximity, aging_recheck, scheduled,
// backtest) intentionally proceed even when document_set_hash is unchanged.
//
// Mirrored in the partial unique index orchestrator_runs_pending_content_dedup_idx
// (see migration 20260527000010_v3_content_dedup_document_set_hash.sql).
export const CONTENT_DEDUP_BYPASS_TRIGGERS: ReadonlySet<string> = new Set([
  "manual",
  "operator_refresh",
  "tier2_escalation",
  "catalyst_proximity",
  "aging_recheck",
  "scheduled",
  "backtest",
]);
