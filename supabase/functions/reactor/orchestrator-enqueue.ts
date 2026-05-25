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
  // WI-2: BC convergence pre-gate fields. When the pre-gate is active
  // (internal_config.bc_pregate_enabled='true') and the composite score is
  // below threshold, status is 'declined' and routine_declined=true;
  // otherwise status is 'pending'. The score + inputs are persisted on every
  // binary_catalyst run for forensic audit, declined or not.
  bc_pregate_score?: number | null;
  bc_pregate_inputs?: Record<string, unknown> | null;
  routine_declined?: boolean;
  decline_reasons?: string[] | null;
  status_override?: "declined";
}

export interface OrchestratorRunInsert {
  asset_id: string;
  trigger_type: EnqueueArgs["trigger_type"];
  trigger_doc_id: string | null;
  status: "pending" | "declined";
  document_set_hash: string | null;
  bc_pregate_score?: number | null;
  bc_pregate_inputs?: Record<string, unknown> | null;
  routine_declined?: boolean;
  decline_reasons?: string[] | null;
}

export function buildOrchestratorRunInsert(args: EnqueueArgs): OrchestratorRunInsert {
  const status: "pending" | "declined" = args.status_override ?? "pending";
  const insert: OrchestratorRunInsert = {
    asset_id: args.asset_id,
    trigger_type: args.trigger_type,
    trigger_doc_id: args.trigger_doc_id,
    status,
    document_set_hash: args.document_set_hash ?? null,
  };
  // Only include pre-gate fields when the caller actually scored something —
  // keeps non-binary_catalyst rows clean of NULL noise.
  if (args.bc_pregate_score !== undefined) {
    insert.bc_pregate_score = args.bc_pregate_score;
  }
  if (args.bc_pregate_inputs !== undefined) {
    insert.bc_pregate_inputs = args.bc_pregate_inputs;
  }
  if (args.routine_declined !== undefined) {
    insert.routine_declined = args.routine_declined;
  }
  if (args.decline_reasons !== undefined) {
    insert.decline_reasons = args.decline_reasons;
  }
  return insert;
}

// Bypass set: enqueues with these trigger_types skip the content-dedup check.
// Manual/operator initiated runs and replay backtests intentionally proceed
// even when document_set_hash is unchanged. Routine system refresh paths are
// content-deduped so unchanged evidence does not burn another assessment.
//
// Mirrored in the partial unique index orchestrator_runs_pending_content_dedup_idx
// (see migration 20260527000010_v3_content_dedup_document_set_hash.sql).
export const CONTENT_DEDUP_BYPASS_TRIGGERS: ReadonlySet<string> = new Set([
  "manual",
  "operator_refresh",
  "backtest",
]);
