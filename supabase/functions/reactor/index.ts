// Reactor edge function — runs on signals.INSERT, signals.UPDATE,
// AND v3 asset_documents.INSERT webhooks.
//
// v3 Stream 1 added a top-level dispatch on payload.table:
//
//   • table='signals'  — legacy v2 path. The function still receives every
//     signal write so non-FDA verticals (activist_governance, takeover_candidate,
//     litigation, merger_arb) keep their full convergence flow. Inside
//     processSignal we short-circuit FDA scoring profiles (binary_catalyst,
//     fda_event) into the v3 orchestrator queue instead of running the legacy
//     classifyGroup / bonus stamping / clearDisplacedWinners pipeline.
//
//   • table='asset_documents' — v3 path. New material primary links written
//     by the Sonnet asset_linker (orchestrator_app::asset_linker_run) fire
//     this trigger via call_reactor_assetdoc(). Reactor enqueues an
//     orchestrator_runs row with trigger_type='new_doc' (or 'cross_source'
//     when a sibling primary doc already exists for the same asset within
//     24h) and returns. The Modal orchestrator_drain_queue function picks
//     up pending rows on its 5-min poll.
//
// Legacy v2 flow on signals (spec.md §6.1) — unchanged for non-FDA profiles:
//   1. Parse the Supabase DB-webhook envelope; pick record = new signal row.
//   2. Resolve convergence_key: prefer issuer_figi, else walk entity_identifiers fallback chain.
//   3. Query the 14d window (30d if any in-group signal is litigation).
//   4. Classify the group (contradiction / same_direction / orthogonal / single).
//   5. Pick winner (highest-scoring signal). Compute score_with_bonus + raw band_with_bonus.
//   6. RPC Modal rubric-apply-caps to get the final band (with auto-caps re-applied to the
//      bonus-adjusted score), preserving Python parity per spec §7.1.
//   7. UPDATE the winner row. Cross-UPDATE the prior winner (if any and displaced) to clear
//      its convergence fields.
//   8. If winner's band_with_bonus='immediate':
//        a. INSERT alerts ON CONFLICT DO NOTHING (same-day fingerprint dup dedup).
//        b. INSERT thesis_jobs ON CONFLICT (signal_id) DO NOTHING — enqueues the Claude
//           thesis-writer Modal function (spec §6.1 step 8, §7.4).
//      Alert and thesis-job inserts are independent: fan-out fires regardless of thesis
//      state so the 5-min email SLA is never gated on drafting.
//   9. On any throw, INSERT failed_reactor_events and return 500 so Supabase retries.

import { createClient } from "npm:@supabase/supabase-js@2";
import {
  classifyBand,
  classifyGroup,
  type GroupSignal,
  signalFingerprint,
} from "../_shared/convergence.ts";
import {
  classifyProvisionalHeuristic,
  flattenPersistedDimensions,
  shouldProcessUpdate,
} from "./scoring-state.ts";
import {
  shouldClearDisplacedWinner,
  shouldUseLitigationWindow,
} from "./convergence-window.ts";
import {
  buildOrchestratorRunInsert,
  type EnqueueArgs,
} from "./orchestrator-enqueue.ts";
import { fetchWithRetry } from "./fetch-retry.ts";

type Direction = "long" | "short" | "neutral" | null | undefined;
type Band = "immediate" | "watchlist" | "archive" | "discard";

// `score` and `band` are nullable per migration 20260421000000 — unscored signals
// land in the DB with both NULL until signal_resolver fills dims. The convergence
// path below assumes they are non-null and is gated by a short-circuit that
// routes NULL-score signals into the signal_resolver queue before this interface
// is consumed by classifyGroup.
interface SignalRow extends Omit<GroupSignal, "score"> {
  entity_id: string | null;
  issuer_figi: string | null;
  raw_payload: Record<string, unknown>;
  dimensions: Record<string, unknown>;
  score: number | null;
  band: Band | null;
  thesis_direction: Direction;
  convergence_bonus?: number | null;
  band_with_bonus?: Band | null;
  score_with_bonus?: number | null;
  auto_caps_triggered?: string[] | null;
}

interface AssetDocumentRow {
  id: string;
  asset_id: string;
  document_id: string;
  link_type: "primary" | "mentions" | "pipeline_context" | "safety_signal" | "literature";
  is_material: boolean;
  extraction_method: string;
  extraction_confidence: number | null;
  created_at: string;
}

// Webhook envelope is shared across signals + asset_documents. The
// `record` shape is decided by `table`; we narrow it inside the dispatcher.
interface SignalsWebhookPayload {
  type: "INSERT" | "UPDATE" | "DELETE";
  table: "signals";
  schema: string;
  record: SignalRow;
  old_record: SignalRow | null;
}

interface AssetDocumentsWebhookPayload {
  type: "INSERT" | "UPDATE" | "DELETE";
  table: "asset_documents";
  schema: string;
  record: AssetDocumentRow;
  old_record: null;
}

interface OtherWebhookPayload {
  type: "INSERT" | "UPDATE" | "DELETE";
  table: string;
  schema: string;
  record: Record<string, unknown>;
  old_record: Record<string, unknown> | null;
}

type WebhookPayload =
  | SignalsWebhookPayload
  | AssetDocumentsWebhookPayload
  | OtherWebhookPayload;

// FDA scoring profiles route into the v3 orchestrator queue instead of the
// legacy convergence path. The v2 reactor still classifies + stamps the row,
// so these signals remain queryable in the legacy schema; we just don't run
// classifyGroup / bonus stamping / alert + thesis_job inserts for them — the
// orchestrator owns all of that for FDA assets.
const FDA_PROFILES_ROUTED_TO_ORCHESTRATOR = new Set([
  "binary_catalyst",
  "fda_event",
]);

// Cross-source coalesce window for asset_documents → orchestrator_runs.
// If a sibling primary doc was linked to the same asset in the last 24h,
// we tag the new run as 'cross_source' (Tier 1 trigger per spec) instead
// of 'new_doc' (Tier 2 trigger).
const CROSS_SOURCE_WINDOW_HOURS = 24;

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SERVICE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const WEBHOOK_SECRET = Deno.env.get("WEBHOOK_SECRET") ?? "";
// F-202: fail-fast on missing env var. Previous fallback to
// "https://marazuela--rubric-apply-caps.modal.run" silently routed traffic
// to a personal Modal namespace if the secret was unset/typo'd.
function requireEnv(name: string, hint: string): string {
  const v = Deno.env.get(name);
  if (!v) throw new Error(`${name} env var is required; ${hint}`);
  return v;
}
const RUBRIC_MODAL_URL: string = requireEnv(
  "RUBRIC_APPLY_CAPS_URL",
  "set via Supabase Dashboard → Edge Functions → Secrets",
);

const sb = createClient(SUPABASE_URL, SERVICE_KEY, {
  auth: { autoRefreshToken: false, persistSession: false },
});

Deno.serve(async (req: Request) => {
  // Webhook secret check (if configured). Set via Supabase Dashboard → Edge Functions → Secrets.
  // Constant-time comparison so an attacker can't byte-by-byte the secret via timing.
  if (WEBHOOK_SECRET) {
    const got = req.headers.get("x-supabase-webhook-secret") ?? "";
    if (!timingSafeEqual(got, WEBHOOK_SECRET)) {
      return new Response("unauthorized", { status: 401 });
    }
  }

  let payload: WebhookPayload;
  try {
    payload = await req.json();
  } catch {
    return new Response("invalid json", { status: 400 });
  }

  // Dispatch on payload.table — signals (legacy v2 + FDA-profile short-circuit)
  // vs asset_documents (v3 orchestrator enqueue). Anything else: 200 skipped.
  if (payload.table === "asset_documents") {
    if (payload.type !== "INSERT") {
      return new Response(
        JSON.stringify({
          skipped: true,
          reason: "asset_documents non-INSERT ignored",
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    }
    const link = (payload as AssetDocumentsWebhookPayload).record;
    try {
      const result = await processAssetDocument(link);
      return new Response(JSON.stringify(result), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      await sb.from("failed_reactor_events").insert({
        signal_id: null,
        payload: payload as unknown as Record<string, unknown>,
        error_message: `[asset_documents] ${message}`,
      });
      return new Response(JSON.stringify({ error: message }), {
        status: 500,
        headers: { "content-type": "application/json" },
      });
    }
  }

  if (payload.table !== "signals") {
    return new Response(
      JSON.stringify({ skipped: true, reason: "unsupported table" }),
      {
        status: 200,
        headers: { "content-type": "application/json" },
      },
    );
  }
  // INSERT is the normal scanner→reactor path. UPDATE is accepted only when a
  // signal_resolver skill has filled in dims and transitioned score NULL→non-NULL;
  // in that case we re-run convergence. All other UPDATE events are ignored so
  // the reactor isn't re-entered by its own stamping writes (which also UPDATE
  // convergence_* columns).
  const signalsPayload = payload as SignalsWebhookPayload;
  if (signalsPayload.type === "UPDATE") {
    if (!shouldProcessUpdate(signalsPayload.record, signalsPayload.old_record)) {
      return new Response(
        JSON.stringify({
          skipped: true,
          reason: "UPDATE without scoring-resolution transition",
        }),
        {
          status: 200,
          headers: { "content-type": "application/json" },
        },
      );
    }
  } else if (signalsPayload.type !== "INSERT") {
    return new Response(
      JSON.stringify({ skipped: true, reason: "unsupported webhook type" }),
      {
        status: 200,
        headers: { "content-type": "application/json" },
      },
    );
  }

  const sig = signalsPayload.record;

  try {
    const result = await processSignal(sig);
    return new Response(JSON.stringify(result), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  } catch (err) {
    // DLQ the event so Supabase's webhook retry + our own inspection both work.
    const message = err instanceof Error ? err.message : String(err);
    await sb.from("failed_reactor_events").insert({
      signal_id: sig?.signal_id ?? null,
      payload: payload as unknown as Record<string, unknown>,
      error_message: message,
    });
    return new Response(JSON.stringify({ error: message }), {
      status: 500,
      headers: { "content-type": "application/json" },
    });
  }
});

// Every scoring profile routes unscored signals to the signal_resolver queue.
// Previously this only covered the three profiles whose dim_estimator returns
// None unconditionally (activist_governance, merger_arb, litigation). But
// short_positioning / binary_catalyst / takeover_candidate can also return
// None when required payload keys (position_pct, days_until_pdufa, patterns_hit)
// are missing; those signals would otherwise hit the "unscored_signal_in_scored_profile"
// branch and be silently dropped. Enqueue them instead so the resolver skill
// (or a human operator) can fill dims.
const UNSCORED_RESOLVER_PROFILES = new Set([
  "activist_governance",
  "merger_arb",
  "litigation",
  "short_positioning",
  "binary_catalyst",
  "takeover_candidate",
]);

async function processSignal(sig: SignalRow) {
  // --- v3 FDA short-circuit.
  // FDA-profile signals (binary_catalyst, fda_event) skip the legacy
  // convergence/bonus/alert pipeline. Their orchestration runs through the
  // v3 orchestrator queue: ingestion adapters write to documents +
  // asset_documents, the asset_documents trigger fires reactor again on
  // that table and enqueues an orchestrator_runs row. This `signals` event
  // is recorded in the legacy schema for non-FDA observability + audit
  // (the 4 operational scanners co-write to signals as a side-effect of
  // their existing scanner_base implementation). We acknowledge it here
  // and return without convergence stamping.
  if (
    sig.scoring_profile &&
    FDA_PROFILES_ROUTED_TO_ORCHESTRATOR.has(sig.scoring_profile)
  ) {
    return {
      processed: true,
      skipped: "fda_profile_routed_to_orchestrator",
      signal_id: sig.signal_id,
      scoring_profile: sig.scoring_profile,
    };
  }

  // --- Unscored signal short-circuit.
  // Signals from dim_estimator-unsupported profiles arrive with score=NULL.
  // They can't run convergence (no score to weigh) so we enqueue them onto the
  // signal_resolver queue and return. When the skill fills dims and UPDATEs
  // the row (score NULL→non-NULL), reactor is re-invoked via the UPDATE webhook
  // path and continues through the normal convergence flow below.
  if ((sig.score ?? null) === null) {
    if (
      sig.scoring_profile && UNSCORED_RESOLVER_PROFILES.has(sig.scoring_profile)
    ) {
      const enqueued = await enqueueNeedsScoring(sig.signal_id);
      return {
        processed: true,
        needs_scoring_enqueued: enqueued,
        signal_id: sig.signal_id,
        scoring_profile: sig.scoring_profile,
      };
    }
    // Unknown scoring_profile — shouldn't happen (DB CHECK would reject), but
    // log and skip rather than crash, so a bad row can't DLQ reactor.
    return {
      processed: false,
      reason: "unscored_signal_unknown_profile",
      signal_id: sig.signal_id,
      scoring_profile: sig.scoring_profile,
    };
  }

  // --- Provisional heuristic short-circuit.
  // Heuristic rows now persist a full numeric dims map plus provenance +
  // scoring_meta. If the estimator had to neutral-fill unsupported dims, the row
  // stays provisional and should be resolved before convergence / alerts /
  // thesis drafting treat it as final.
  //
  // A "malformed" heuristic row — _provenance='heuristic' with NO scoring_meta
  // sidecar — is a scanner bug: we can't tell supported vs defaulted dims, so
  // the row is routed to signal_resolver just like any other provisional row,
  // AND we insert an operator_flag so the writing scanner gets fixed.
  const provisional = classifyProvisionalHeuristic(sig);
  if (provisional.provisional) {
    const enqueued = await enqueueNeedsScoring(sig.signal_id);
    if (provisional.malformed) {
      await flagHeuristicMissingScoringMeta(sig);
    }
    return {
      processed: true,
      needs_scoring_enqueued: enqueued,
      signal_id: sig.signal_id,
      scoring_profile: sig.scoring_profile,
      provisional_scoring: true,
      malformed_scoring_meta: provisional.malformed,
    };
  }

  // --- Step 1-2: resolve convergence_key.
  const convergence_key = await resolveConvergenceKey(sig);

  // --- Step 3: query the window. Use 30d if any current signal in the full
  // 30d candidate group is litigation; otherwise use the standard 14d window.
  let windowSignals = await fetchWindow(convergence_key, 14);
  const firstPassProfiles = [
    sig.scoring_profile,
    ...windowSignals.map((s) => s.scoring_profile),
  ];
  const hasExtendedLitigation = firstPassProfiles.includes("litigation") ||
    await hasLitigationInWindow(convergence_key, 30);
  if (shouldUseLitigationWindow(firstPassProfiles, hasExtendedLitigation)) {
    windowSignals = await fetchWindow(convergence_key, 30);
  }

  // The webhook fires AFTER INSERT, so sig is already in the window query results.
  // But the `source_content_hash`-based dedup in classifyGroup handles any edge case.
  // Filter out unscored siblings in the window — they're waiting on signal_resolver
  // and have no score to weigh in the group.
  const group: GroupSignal[] = windowSignals
    .filter((r) => r.score !== null)
    .map(signalRowToGroupSignal);
  if (!group.some((g) => g.signal_id === sig.signal_id)) {
    group.push(signalRowToGroupSignal(sig)); // defensive: include the INSERTed row
  }

  const verdict = classifyGroup(group);

  // --- Step 4-5: resolve the winner and its raw score_with_bonus.
  const winnerGroup = verdict.unique_signals.find((s) =>
    s.signal_id === verdict.winner_signal_id
  );
  if (!winnerGroup) {
    // Edge case: empty group (shouldn't happen post-INSERT). Stamp the INSERTed row with zeros.
    // Non-null on score/band guaranteed by the unscored short-circuit at the top of processSignal.
    await stampRow(sig.signal_id, convergence_key, 0, sig.score!, sig.band!);
    return {
      processed: true,
      convergence_key,
      convergence_bonus: 0,
      winner_signal_id: sig.signal_id,
    };
  }

  const winnerRow = await fetchSignalFull(winnerGroup.signal_id);
  if (!winnerRow) {
    throw new Error(`winner signal ${winnerGroup.signal_id} disappeared`);
  }
  if (winnerRow.score === null) {
    // Defensive: the unscored filter above should have excluded this row from the group.
    throw new Error(`winner signal ${winnerGroup.signal_id} has NULL score`);
  }

  const scoreWithBonus = round2(winnerRow.score + verdict.bonus);
  const rawBandWithBonus: Band = classifyBand(scoreWithBonus);

  // --- Step 6: RPC Modal to re-apply auto-caps on the bonus-adjusted band.
  const capped = await rubricApplyCaps(winnerRow, rawBandWithBonus);
  const finalBand = capped.band;
  // The winner's auto_caps_triggered column already holds the pre-convergence caps; we
  // merge in any new caps that fired on the bonus-adjusted band. Order-insensitive per spec §10.4.
  const mergedCaps = Array.from(
    new Set([
      ...(winnerRow.auto_caps_triggered ?? []),
      ...capped.auto_caps_triggered,
    ]),
  );

  // --- Step 7: UPDATE the winner. Carry through demotion_reason from the post-bonus
  // cap-apply (curator-readable surface; auto_caps_triggered remains the rule_id contract).
  await stampRow(
    winnerRow.signal_id,
    convergence_key,
    verdict.bonus,
    scoreWithBonus,
    finalBand,
    mergedCaps,
    capped.demotion_reason ?? null,
  );

  // --- Cross-update: any prior row in the group that has non-null convergence_bonus and is
  // NOT the new winner gets its convergence fields cleared. This matches v1's
  // "bonus lives only on the winner" behavior.
  const cross_updates = await clearDisplacedWinners(
    convergence_key,
    winnerRow.signal_id,
    windowSignals,
  );

  // --- Step 8: on Immediate band, insert alert AND enqueue thesis_job in parallel.
  //   8a. alerts INSERT (ON CONFLICT DO NOTHING — same-day fingerprint dup).
  //   8b. thesis_jobs INSERT (ON CONFLICT DO NOTHING on signal_id — one draft per signal).
  // The two inserts are independent: fan-out fires regardless of thesis_writer status, so
  // the 5-min email SLA is never gated on drafting (spec §6.1 step 8).
  let alert_inserted = false;
  let thesis_job_enqueued = false;
  if (finalBand === "immediate") {
    const [alertRes, jobRes] = await Promise.all([
      insertAlert(winnerRow),
      enqueueThesisJob(winnerRow.signal_id),
    ]);
    alert_inserted = alertRes;
    thesis_job_enqueued = jobRes;
  }

  return {
    processed: true,
    convergence_key,
    convergence_bonus: verdict.bonus,
    convergence_type: verdict.type,
    score_with_bonus: scoreWithBonus,
    band_with_bonus: finalBand,
    winner_signal_id: winnerRow.signal_id,
    cross_updates,
    alert_inserted,
    thesis_job_enqueued,
  };
}

// ----------------------------------------------------------------------
// v3 — asset_documents → orchestrator_runs enqueue path.
// ----------------------------------------------------------------------

async function processAssetDocument(link: AssetDocumentRow) {
  // The trigger predicate already restricts us to (link_type='primary' AND
  // is_material=true) — defensive checks here so a misfired webhook can't
  // enqueue garbage.
  if (link.link_type !== "primary" || link.is_material !== true) {
    return {
      processed: false,
      skipped: "non_primary_or_immaterial",
      asset_id: link.asset_id,
      document_id: link.document_id,
    };
  }

  // Decide trigger_type. cross_source = ≥1 prior primary link on the same
  // asset within the last 24h (this new doc would be the second source);
  // otherwise new_doc.
  const since = new Date(
    Date.now() - CROSS_SOURCE_WINDOW_HOURS * 3600 * 1000,
  ).toISOString();
  const { data: priorLinks, error: priorErr } = await sb
    .from("asset_documents")
    .select("id")
    .eq("asset_id", link.asset_id)
    .eq("link_type", "primary")
    .eq("is_material", true)
    .neq("id", link.id)
    .gte("created_at", since)
    .limit(1);
  if (priorErr) throw priorErr;
  const triggerType: "cross_source" | "new_doc" =
    (priorLinks?.length ?? 0) > 0 ? "cross_source" : "new_doc";

  // PR-2 content-aware dedup: compute md5 over the asset's current material
  // primary document_id set and compare against the last non-superseded
  // convergence_assessments row. If unchanged, skip the enqueue — running
  // synthesis again can't produce new information.
  const docSetHash = await computeDocSetHash(link.asset_id);
  if (docSetHash !== null && !CONTENT_DEDUP_BYPASS_TRIGGERS.has(triggerType)) {
    const { data: lastAssessment, error: lastErr } = await sb
      .from("convergence_assessments")
      .select("document_set_hash")
      .eq("asset_id", link.asset_id)
      .is("superseded_at", null)
      .order("created_at", { ascending: false })
      .limit(1)
      .maybeSingle();
    if (lastErr) throw lastErr;
    if (lastAssessment?.document_set_hash === docSetHash) {
      return {
        processed: true,
        asset_id: link.asset_id,
        document_id: link.document_id,
        trigger_type: triggerType,
        enqueued: false,
        orchestrator_run_id: null,
        coalesce_dedupe: true,
        dedupe_reason: "doc_set_unchanged",
      };
    }
  }

  const enqueue = await enqueueOrchestratorRun({
    asset_id: link.asset_id,
    trigger_type: triggerType,
    trigger_doc_id: link.document_id,
  });
  return {
    processed: true,
    asset_id: link.asset_id,
    document_id: link.document_id,
    trigger_type: triggerType,
    enqueued: enqueue.enqueued,
    orchestrator_run_id: enqueue.run_id,
    coalesce_dedupe: enqueue.coalesce_dedupe,
  };
}

// Compute md5 over the asset's material primary asset_documents.document_id set,
// sorted to make the hash order-invariant. Returns null when the asset has zero
// material primary docs (which shouldn't happen in this code path — defensive).
//
// Reused by orchestrator_runs.document_set_hash and (eventually) by Stage 10
// persistence into convergence_assessments.document_set_hash.
async function computeDocSetHash(asset_id: string): Promise<string | null> {
  const { data, error } = await sb
    .from("asset_documents")
    .select("document_id")
    .eq("asset_id", asset_id)
    .eq("link_type", "primary")
    .eq("is_material", true);
  if (error) throw error;
  if (!data || data.length === 0) return null;
  const sorted = data
    .map((r: { document_id: string }) => r.document_id)
    .sort();
  const enc = new TextEncoder().encode(sorted.join(","));
  const buf = await crypto.subtle.digest("MD5", enc).catch(async () => {
    // Some Deno runtimes don't expose MD5 — fall back to SHA-256 truncated.
    return await crypto.subtle.digest("SHA-256", enc);
  });
  return Array.from(new Uint8Array(buf))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("")
    .slice(0, 32);
}

async function enqueueOrchestratorRun(
  args: EnqueueArgs,
): Promise<{ enqueued: boolean; run_id: string | null; coalesce_dedupe: boolean }> {
  // The partial unique index orchestrator_runs_pending_dedup_idx (status='pending')
  // makes this an idempotent ON CONFLICT DO NOTHING — a follow-up doc on the
  // same (asset, type, doc) won't double-enqueue while a row is still pending.
  // Once the drainer flips status, a new row can be enqueued for the next doc.
  const row = buildOrchestratorRunInsert(args);
  const { data, error } = await sb
    .from("orchestrator_runs")
    .insert(row)
    .select("id");
  if (error) {
    const code = (error as { code?: string }).code;
    if (code === "23505") {
      // Unique-violation = pending row already exists for this dedupe key.
      return { enqueued: false, run_id: null, coalesce_dedupe: true };
    }
    throw error;
  }
  return {
    enqueued: (data?.length ?? 0) > 0,
    run_id: data?.[0]?.id ?? null,
    coalesce_dedupe: false,
  };
}

async function enqueueThesisJob(signal_id: string): Promise<boolean> {
  const { data, error } = await sb
    .from("thesis_jobs")
    .insert({ signal_id })
    .select("id");
  if (error) {
    // 23505 = unique_violation on signal_id → job already queued/promoted/dlq'd. No-op.
    const code = (error as { code?: string }).code;
    if (code === "23505") return false;
    throw error;
  }
  return (data?.length ?? 0) > 0;
}

// signal_resolver queue — unscored signals in activist_governance / merger_arb /
// litigation land here on INSERT. The skill drains them, fills dims, and either
// promotes through scoring_complete_below_immediate (terminal) or transitions
// the same row to drafting → promoted when the resolved band is immediate.
async function enqueueNeedsScoring(signal_id: string): Promise<boolean> {
  const { data, error } = await sb
    .from("thesis_jobs")
    .insert({ signal_id, status: "needs_scoring" })
    .select("id");
  if (error) {
    const code = (error as { code?: string }).code;
    if (code === "23505") return false;
    throw error;
  }
  return (data?.length ?? 0) > 0;
}

// Insert an operator_flag when a heuristic-stamped row is missing its
// scoring_meta sidecar. This is a scanner writer bug — we still route the
// row to signal_resolver so it can be fixed, but we also surface it so
// operators can chase down the offending scanner. Per-signal dedup via
// the partial unique index on (source, kind, signal_id) WHERE resolved_at IS NULL.
// Never throws — flag-write failures cannot break reactor processing.
async function flagHeuristicMissingScoringMeta(sig: SignalRow): Promise<void> {
  try {
    const { data: existing } = await sb
      .from("operator_flags")
      .select("id")
      .eq("source", "reactor")
      .eq("kind", "heuristic_missing_scoring_meta")
      .eq("signal_id", sig.signal_id)
      .is("resolved_at", null)
      .limit(1);
    if (existing && existing.length > 0) return;
    await sb.from("operator_flags").insert({
      severity: "critical",
      source: "reactor",
      kind: "heuristic_missing_scoring_meta",
      title:
        `Heuristic provenance without scoring_meta on signal ${sig.signal_id}`,
      signal_id: sig.signal_id,
      evidence: {
        signal_id: sig.signal_id,
        scoring_profile: sig.scoring_profile,
      },
    });
  } catch (err) {
    console.error("flagHeuristicMissingScoringMeta failed:", err);
  }
}

// ----------------------------------------------------------------------
// Helpers
// ----------------------------------------------------------------------

async function resolveConvergenceKey(sig: SignalRow): Promise<string> {
  if (sig.issuer_figi) return `figi:${sig.issuer_figi}`;

  if (sig.entity_id) {
    // Walk entity_identifiers in priority order (lower = higher priority).
    // Skip `name_normalized` here — see the disambiguation fallback below.
    // Future: when scanner-caches/litigation/party_resolution_cache.json grows
    // beyond its litigation seed, an additional name → canonical (cik/figi)
    // resolution step can sit between this query and the entity:<uuid> fallback.
    const { data, error } = await sb
      .from("entity_identifiers")
      .select("id_type,id_value")
      .eq("entity_id", sig.entity_id)
      .neq("id_type", "name_normalized")
      .order("priority", { ascending: true })
      .limit(1);
    if (error) throw error;
    if (data && data.length > 0) {
      return `${data[0].id_type}:${data[0].id_value}`;
    }
    // Entity is identified but only via fuzzy name. Use the entity uuid as the
    // convergence key — guarantees no false merge across two distinct entities
    // that happen to share a normalized name (the "AMERICAN" / "NATIONAL"
    // common-name collision risk).
    return `entity:${sig.entity_id}`;
  }

  return `unidentified:${sig.signal_id}`;
}

async function fetchWindow(
  convergence_key: string,
  days: 14 | 30,
): Promise<SignalRow[]> {
  const since = new Date(Date.now() - days * 24 * 3600 * 1000).toISOString();
  // We have two orthogonal lookup paths: (a) rows already stamped with this
  // convergence_key, (b) rows we might retroactively group by figi. For simplicity,
  // lookup by convergence_key first; the reactor stamps every signal, so after the
  // first invocation this is the authoritative path. On a cold DB the first row in a
  // group will not have convergence_key populated; we also fetch by issuer_figi in
  // that case (see fetchWindowByFigi).
  const { data, error } = await sb
    .from("signals")
    .select(
      "signal_id,entity_id,issuer_figi,scoring_profile,thesis_direction,score,band,dimensions,auto_caps_triggered,raw_payload,source_content_hash,convergence_bonus,band_with_bonus,score_with_bonus",
    )
    .eq("convergence_key", convergence_key)
    .gte("scan_date", since);
  if (error) throw error;
  const rows = (data ?? []) as SignalRow[];

  if (convergence_key.startsWith("figi:")) {
    const figi = convergence_key.slice("figi:".length);
    const { data: d2, error: e2 } = await sb
      .from("signals")
      .select(
        "signal_id,entity_id,issuer_figi,scoring_profile,thesis_direction,score,band,dimensions,auto_caps_triggered,raw_payload,source_content_hash,convergence_bonus,band_with_bonus,score_with_bonus",
      )
      .eq("issuer_figi", figi)
      .is("convergence_key", null)
      .gte("scan_date", since);
    if (e2) throw e2;
    for (const row of (d2 ?? []) as SignalRow[]) {
      if (!rows.find((r) => r.signal_id === row.signal_id)) rows.push(row);
    }
  }
  return rows;
}

async function hasLitigationInWindow(
  convergence_key: string,
  days: 14 | 30,
): Promise<boolean> {
  const since = new Date(Date.now() - days * 24 * 3600 * 1000).toISOString();
  const { data, error } = await sb
    .from("signals")
    .select("signal_id")
    .eq("convergence_key", convergence_key)
    .eq("scoring_profile", "litigation")
    .gte("scan_date", since)
    .limit(1);
  if (error) throw error;
  if ((data?.length ?? 0) > 0) return true;

  if (convergence_key.startsWith("figi:")) {
    const figi = convergence_key.slice("figi:".length);
    const { data: d2, error: e2 } = await sb
      .from("signals")
      .select("signal_id")
      .eq("issuer_figi", figi)
      .is("convergence_key", null)
      .eq("scoring_profile", "litigation")
      .gte("scan_date", since)
      .limit(1);
    if (e2) throw e2;
    return (d2?.length ?? 0) > 0;
  }
  return false;
}

function signalRowToGroupSignal(r: SignalRow): GroupSignal {
  return {
    signal_id: r.signal_id,
    scoring_profile: r.scoring_profile,
    thesis_direction: r.thesis_direction ?? null,
    // Caller filters out unscored rows before calling this. Keeping the null
    // guard here as belt-and-suspenders — Number(null)=0 would quietly produce
    // bogus convergence group scores.
    score: r.score === null ? 0 : Number(r.score),
    source_content_hash: r.source_content_hash,
  };
}

interface FullSignal extends SignalRow {
  auto_caps_triggered: string[];
  source_content_hash: string;
}

async function fetchSignalFull(signal_id: string): Promise<FullSignal | null> {
  const { data, error } = await sb
    .from("signals")
    .select("*")
    .eq("signal_id", signal_id)
    .limit(1);
  if (error) throw error;
  return (data && data[0]) ? (data[0] as FullSignal) : null;
}

async function rubricApplyCaps(
  row: FullSignal,
  band: Band,
): Promise<
  { band: Band; auto_caps_triggered: string[]; demotion_reason: string | null }
> {
  // signals.dimensions is persisted as the provenance envelope
  // ({dim: {value, provenance}, _provenance}); apply_auto_caps wants flat ints.
  // Without this flatten, litigation raises TypeError on `dict < int` and
  // merger_arb silently evaluates False against envelope dicts.
  const body = {
    signal: { raw_data: row.raw_payload ?? {} },
    dimensions: flattenPersistedDimensions(
      row.dimensions as Record<string, unknown> | null | undefined,
    ),
    profile: row.scoring_profile,
    band,
  };
  const r = await fetchWithRetry(RUBRIC_MODAL_URL, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    throw new Error(`rubric_apply_caps ${r.status}: ${await r.text()}`);
  }
  const j = (await r.json()) as {
    band: Band;
    auto_caps_triggered: string[];
    demotion_reason?: string | null;
  };
  return {
    band: j.band,
    auto_caps_triggered: j.auto_caps_triggered,
    demotion_reason: j.demotion_reason ?? null,
  };
}

async function stampRow(
  signal_id: string,
  convergence_key: string,
  convergence_bonus: 0 | 5 | 10,
  score_with_bonus: number,
  band_with_bonus: Band,
  auto_caps_triggered?: string[],
  demotion_reason?: string | null,
) {
  const patch: Record<string, unknown> = {
    convergence_key,
    convergence_bonus,
    score_with_bonus,
    band_with_bonus,
    convergence_evaluated_at: new Date().toISOString(),
  };
  if (auto_caps_triggered) patch.auto_caps_triggered = auto_caps_triggered;
  // Always set demotion_reason when caller passes it (including explicit null,
  // which clears stale narratives if a cap stops firing on re-evaluation).
  if (demotion_reason !== undefined) patch.demotion_reason = demotion_reason;
  const { error } = await sb.from("signals").update(patch).eq(
    "signal_id",
    signal_id,
  );
  if (error) throw error;
}

async function clearDisplacedWinners(
  convergence_key: string,
  winner_id: string,
  windowSignals: SignalRow[],
): Promise<string[]> {
  const toClear = windowSignals.filter(
    (s) => shouldClearDisplacedWinner(s, winner_id),
  );
  const ids: string[] = [];
  for (const s of toClear) {
    const { error } = await sb
      .from("signals")
      .update({
        convergence_bonus: 0,
        score_with_bonus: null,
        band_with_bonus: null,
      })
      .eq("signal_id", s.signal_id)
      .eq("convergence_key", convergence_key);
    if (error) throw error;
    ids.push(s.signal_id);
  }
  return ids;
}

async function insertAlert(row: FullSignal): Promise<boolean> {
  const fp = await signalFingerprint(
    row.source_content_hash,
    row.scoring_profile,
  );
  const { data, error } = await sb
    .from("alerts")
    .insert({
      entity_id: row.entity_id,
      signal_id: row.signal_id,
      signal_fingerprint: fp,
    })
    .select("id");
  if (error) {
    // 23505 = unique_violation → same-day dup, which is the intended no-op.
    const code = (error as { code?: string }).code;
    if (code === "23505") return false;
    throw error;
  }
  return (data?.length ?? 0) > 0;
}

function round2(n: number): number {
  return Math.round(n * 100) / 100;
}

// Constant-time string compare. Always iterates over the longer input so the
// total time is independent of both length and per-byte mismatch position.
function timingSafeEqual(a: string, b: string): boolean {
  const aBytes = new TextEncoder().encode(a);
  const bBytes = new TextEncoder().encode(b);
  const len = Math.max(aBytes.length, bBytes.length);
  let diff = aBytes.length ^ bBytes.length;
  for (let i = 0; i < len; i++) {
    const ax = i < aBytes.length ? aBytes[i] : 0;
    const bx = i < bBytes.length ? bBytes[i] : 0;
    diff |= ax ^ bx;
  }
  return diff === 0;
}
