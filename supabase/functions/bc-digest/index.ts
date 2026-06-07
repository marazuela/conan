// =============================================================================
// bc-digest — standalone Supabase edge function (Phase 3 §2–§3).
//
// THE daily product surface. A PURE deterministic renderer of today's
// bc_thesis_updates.synthesis + bc_candidates band/rank, emailed once a day.
//
// Transport (§3, RN-1): a STANDALONE edge fn fired by a pg_cron net.http_post
// tick (the bc-digest-daily job in db/migrations/...bc_digest_and_outcomes.sql),
// calling Resend DIRECTLY (its own ~8-line fetch, reusing the runtime's existing
// RESEND_API_KEY). It does NOT reuse fanout and does NOT add a DB trigger on
// bc_thesis_updates — strangle-don't-entangle. resolveRecipients + escapeHtml are
// COPIED from fanout (the fn is not shared), not imported.
//
// Idempotency (§3.3): one bc_digest_sends row per (digest_date, target), inserted
// BEFORE the Resend POST under a UNIQUE; a 23505 = already sent today => skip, no
// POST. A non-2xx Resend => status='failed' + the run closes 'partial' (fail-loud;
// the next day's digest re-includes the name). ?force=1 (service-role) bypasses.
//
// Fail-loud (§4): the fn opens a bc_pipeline_runs row first thing and closes it in
// a finally — even a thrown render error stamps a 'failed' row (send-or-throw +
// liveness). Status tokens are the live CHECK set {succeeded, partial, failed}.
//
// INVARIANT: p_crl is never read here — bc_digest_rows() structurally omits it.
//
// Auth (§3.4): deployed with --no-verify-jwt; the fn gates on an x-service-key header
// == the shared compute_secret it reads from internal_config (what the pg_cron tick
// carries) — or a BC_DIGEST_TRIGGER_KEY env override. Only the pg_cron tick (or an
// operator holding the secret) can invoke it. The DB client uses the service role.
// =============================================================================

import { createClient, type SupabaseClient } from "npm:@supabase/supabase-js@2";
import {
  type DigestRow,
  isFlagged,
  renderDigestHtml,
  renderDigestText,
  renderSubject,
} from "./render.ts";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SERVICE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
// Inbound trigger gate (conan port): the pg_cron tick authenticates with the shared
// internal_config.compute_secret in the x-service-key header (conan crons carry
// compute_secret, not the service role). By DEFAULT the fn reads that secret from the
// DB at request time — auto-aligned with the cron, nothing to provision and no secret
// duplicated into a function env. An optional BC_DIGEST_TRIGGER_KEY env overrides the
// DB read (operator break-glass / unit tests). The DB client uses SERVICE_KEY.
const TRIGGER_KEY_OVERRIDE = Deno.env.get("BC_DIGEST_TRIGGER_KEY") ?? "";
const RESEND_API_KEY = Deno.env.get("RESEND_API_KEY") ?? "";
// BC-distinct from-address; defaults to the same verified Resend domain fanout uses.
const FROM_ADDRESS =
  Deno.env.get("BC_DIGEST_FROM_ADDRESS") ??
  Deno.env.get("RESEND_FROM_ADDRESS") ??
  "Conan Alerts <alerts@alerts.solutz.com>";
const DEV_RECIPIENTS = (Deno.env.get("BC_DIGEST_DEV_RECIPIENTS") ?? "")
  .split(",")
  .map((s) => s.trim())
  .filter(Boolean);

const PIPELINE_NAME = "bc_daily_digest";
const DEFAULT_FLAG_MIN_CONFIDENCE = 0.6;
const DEFAULT_SEND_WHEN_EMPTY = true;

// ---------------------------------------------------------------------------
// config — read l4.digest_* from bc_config (jsonb scalar); inline defaults on miss.
// ---------------------------------------------------------------------------
async function readConfigScalar(sb: SupabaseClient, key: string): Promise<unknown> {
  const { data, error } = await sb.from("bc_config").select("value").eq("key", key).limit(1);
  if (error) throw error;
  if (!data || data.length === 0) return undefined; // missing => caller's default
  return (data[0] as { value: unknown }).value;
}

function asNumber(v: unknown, dflt: number): number {
  const n = Number(v);
  return Number.isFinite(n) ? n : dflt;
}
function asBool(v: unknown, dflt: boolean): boolean {
  if (typeof v === "boolean") return v;
  if (typeof v === "number") return v !== 0;
  if (typeof v === "string") {
    const s = v.trim().toLowerCase();
    if (["true", "1", "yes"].includes(s)) return true;
    if (["false", "0", "no", ""].includes(s)) return false;
  }
  return dflt;
}

// ---------------------------------------------------------------------------
// resolveRecipients — bc-OWNED recipient resolution (decoupled from v3).
//   Precedence — so v3 users are NEVER emailed unless explicitly opted in:
//     (a) bc_config.l4.digest_recipient_email (JSON array) if non-empty; else
//     (b) BC_DIGEST_DEV_RECIPIENTS env (warm-up / you-only); else
//     (c) notifications_prefs.email_on_immediate -> auth.admin.listUsers two-hop
//         (the v3 pool — an explicit future opt-in, reached only when neither
//         (a) nor (b) is configured).
//   When (a) or (b) supplies recipients, notifications_prefs is NOT queried.
// ---------------------------------------------------------------------------
async function resolveRecipients(sb: SupabaseClient): Promise<string[]> {
  // (a) explicit bc allowlist
  const allowRaw = await readConfigScalar(sb, "l4.digest_recipient_email");
  const allow = Array.isArray(allowRaw)
    ? allowRaw.map((s) => String(s).trim()).filter(Boolean)
    : [];
  if (allow.length > 0) return allow;

  // (b) dev / you-only override (warm-up)
  if (DEV_RECIPIENTS.length > 0) return DEV_RECIPIENTS;

  // (c) v3 opt-in pool — only reached when neither (a) nor (b) is configured
  const { data: prefs, error } = await sb
    .from("notifications_prefs")
    .select("user_id")
    .eq("email_on_immediate", true);
  if (error) throw error;

  const emails: string[] = [];
  if (prefs && prefs.length > 0) {
    const { data: userList, error: uErr } = await sb.auth.admin.listUsers({ perPage: 200 });
    if (uErr) throw uErr;
    const byId = new Map((userList?.users ?? []).map((u) => [u.id, u.email ?? ""]));
    for (const p of prefs) {
      const email = byId.get((p as { user_id: string }).user_id);
      if (email) emails.push(email);
    }
  }
  return emails;
}

// ---------------------------------------------------------------------------
// bc_pipeline_runs open/close (the liveness primitive). Status tokens are the
// live CHECK set; an invalid token never reaches the wire.
// ---------------------------------------------------------------------------
const CLOSE_STATUSES = new Set(["succeeded", "partial", "failed"]);

async function openRun(sb: SupabaseClient, snapshotDate: string): Promise<string | null> {
  const { data, error } = await sb
    .from("bc_pipeline_runs")
    .insert({
      pipeline_name: PIPELINE_NAME,
      status: "running",
      snapshot_date: snapshotDate,
      started_at: new Date().toISOString(),
    })
    .select("id");
  if (error) throw error;
  return data && data.length ? (data[0] as { id: string }).id : null;
}

async function closeRun(
  sb: SupabaseClient,
  runId: string | null,
  args: {
    status: string;
    n_processed: number;
    n_failed: number;
    log: Record<string, unknown>;
    reason?: string | null;
  },
): Promise<void> {
  if (!CLOSE_STATUSES.has(args.status)) {
    throw new Error(`closeRun: invalid status ${args.status} (CHECK {succeeded,partial,failed})`);
  }
  if (!runId) return; // opened-failed: nothing to close, never raise inside finally
  await sb
    .from("bc_pipeline_runs")
    .update({
      status: args.status,
      finished_at: new Date().toISOString(),
      n_processed: args.n_processed,
      n_failed: args.n_failed,
      cost_usd: 0, // no LLM on the digest path
      log: args.log,
      reason: args.reason ?? null,
    })
    .eq("id", runId);
}

// ---------------------------------------------------------------------------
// Send loop — per-recipient idempotency (insert-then-send) + direct Resend POST.
//   Returns {emailed, failed} counts. Mirrors fanout's 23505 dedup + response
//   handling, but against the BC-owned bc_digest_sends table.
// ---------------------------------------------------------------------------
async function sendDigest(
  sb: SupabaseClient,
  recipients: string[],
  args: {
    digestDate: string;
    subject: string;
    html: string;
    text: string;
    flaggedAppNumbers: string[];
    nWatch: number;
    force: boolean;
    fetchImpl?: typeof fetch;
  },
): Promise<{ emailed: number; failed: number; skipped: number }> {
  const doFetch = args.fetchImpl ?? fetch;
  let emailed = 0;
  let failed = 0;
  let skipped = 0;

  for (const to of recipients) {
    let sendRowId: string | null = null;

    if (!args.force) {
      // Insert-then-send: optimistic 'sent' row under UNIQUE(digest_date,target).
      const { data: insRows, error: insErr } = await sb
        .from("bc_digest_sends")
        .insert({
          digest_date: args.digestDate,
          target: to,
          flagged_app_numbers: args.flaggedAppNumbers,
          n_watch: args.nWatch,
          status: "sent",
        })
        .select("id");
      if (insErr) {
        const code = (insErr as { code?: string }).code;
        if (code === "23505") {
          // Already sent today — idempotent re-run; skip, no POST.
          skipped += 1;
          continue;
        }
        throw insErr;
      }
      sendRowId = insRows && insRows.length ? (insRows[0] as { id: string }).id : null;
    }

    if (!RESEND_API_KEY) {
      if (sendRowId) {
        await sb
          .from("bc_digest_sends")
          .update({ status: "failed", response_body: { error: "RESEND_API_KEY unset" } })
          .eq("id", sendRowId);
      }
      failed += 1;
      continue;
    }

    const r = await doFetch("https://api.resend.com/emails", {
      method: "POST",
      headers: { "Authorization": `Bearer ${RESEND_API_KEY}`, "Content-Type": "application/json" },
      body: JSON.stringify({ from: FROM_ADDRESS, to: [to], subject: args.subject, html: args.html, text: args.text }),
    });
    const body = await r.json().catch(() => ({}));
    if (r.ok) {
      const msgId = (body as { id?: string }).id ?? null;
      if (sendRowId) {
        await sb
          .from("bc_digest_sends")
          .update({ status: "sent", resend_message_id: msgId, response_body: body as Record<string, unknown> })
          .eq("id", sendRowId);
      }
      emailed += 1;
    } else {
      if (sendRowId) {
        await sb
          .from("bc_digest_sends")
          .update({ status: "failed", response_body: body as Record<string, unknown> })
          .eq("id", sendRowId);
      }
      failed += 1;
    }
  }
  return { emailed, failed, skipped };
}

// ---------------------------------------------------------------------------
// The run (testable: takes an injected sb client + fetch + today).
// ---------------------------------------------------------------------------
export async function runDigest(
  sb: SupabaseClient,
  opts: { today?: string; force?: boolean; fetchImpl?: typeof fetch } = {},
): Promise<Record<string, unknown>> {
  const today = opts.today ?? new Date().toISOString().slice(0, 10);
  const force = opts.force ?? false;

  let runId: string | null = null;
  let status = "succeeded";
  let reason: string | null = null;
  const log: Record<string, unknown> = {};

  try {
    runId = await openRun(sb, today);

    const flagMinConfidence = asNumber(
      await readConfigScalar(sb, "l4.digest_flag_min_confidence"),
      DEFAULT_FLAG_MIN_CONFIDENCE,
    );
    const sendWhenEmpty = asBool(
      await readConfigScalar(sb, "l4.digest_send_when_empty"),
      DEFAULT_SEND_WHEN_EMPTY,
    );

    // The read query — one round trip via the SECURITY DEFINER reader (omits p_crl).
    const { data: rowData, error: rowErr } = await sb.rpc("bc_digest_rows", { p_day: today });
    if (rowErr) throw rowErr;
    const rows = (rowData ?? []) as DigestRow[];

    const opt = { flagMinConfidence };
    const flagged = rows.filter((r) => isFlagged(r, flagMinConfidence));
    const flaggedAppNumbers = flagged.map((r) => r.application_number);
    log.n_watch = rows.length;
    log.n_flagged = flagged.length;

    // Nothing flagged + send-when-empty=false => succeed without sending.
    if (flagged.length === 0 && !sendWhenEmpty) {
      log.emailed = 0;
      log.note = "0 flagged and l4.digest_send_when_empty=false; no send";
      await closeRun(sb, runId, { status: "succeeded", n_processed: rows.length, n_failed: 0, log });
      return { ok: true, ...log };
    }

    const subject = renderSubject(rows, today, opt);
    const html = renderDigestHtml(rows, today, opt);
    const text = renderDigestText(rows, today, opt);

    const recipients = await resolveRecipients(sb);
    log.n_recipients = recipients.length;

    const { emailed, failed, skipped } = await sendDigest(sb, recipients, {
      digestDate: today,
      subject,
      html,
      text,
      flaggedAppNumbers,
      nWatch: rows.length,
      force,
      fetchImpl: opts.fetchImpl,
    });
    log.emailed = emailed;
    log.failed = failed;
    log.skipped = skipped;

    // A failed Resend send => partial (fail-loud; visible, retried next day).
    status = failed > 0 ? "partial" : "succeeded";
    await closeRun(sb, runId, {
      status,
      n_processed: emailed + failed,
      n_failed: failed,
      log,
      reason: failed > 0 ? `${failed} recipient send(s) failed` : null,
    });
    return { ok: true, status, ...log };
  } catch (err) {
    reason = err instanceof Error ? `${err.name}: ${err.message}`.slice(0, 200) : String(err).slice(0, 200);
    log.error = reason;
    // send-or-throw: close the row 'failed' even on a render/read crash.
    await closeRun(sb, runId, { status: "failed", n_processed: 0, n_failed: 0, log, reason });
    throw err;
  }
}

// The expected trigger secret: an explicit env override (tests / break-glass) else
// the shared compute_secret read live from internal_config (what the pg_cron tick
// carries). Returns "" if neither is available -> all callers are rejected.
async function expectedTriggerSecret(sb: SupabaseClient): Promise<string> {
  if (TRIGGER_KEY_OVERRIDE) return TRIGGER_KEY_OVERRIDE;
  const { data } = await sb
    .from("internal_config")
    .select("value")
    .eq("key", "compute_secret")
    .limit(1);
  return data && data.length ? String((data[0] as { value: unknown }).value) : "";
}

// ---------------------------------------------------------------------------
// HTTP entrypoint — shared-secret gate (deployed with --no-verify-jwt).
//
// Exported so tests can drive it without binding a port; the listener only starts
// when this module is the program entrypoint (import.meta.main), so importing it
// from a test (to exercise runDigest) does NOT spin up a server.
// ---------------------------------------------------------------------------
export async function handleRequest(req: Request): Promise<Response> {
  const svcHeader = req.headers.get("x-service-key");
  if (!svcHeader) return new Response("unauthorized", { status: 401 }); // fast reject, no DB

  const sb = createClient(SUPABASE_URL, SERVICE_KEY, {
    auth: { autoRefreshToken: false, persistSession: false },
  });

  const expected = await expectedTriggerSecret(sb);
  if (!expected || svcHeader !== expected) {
    return new Response("unauthorized", { status: 401 });
  }

  const url = new URL(req.url);
  const force = url.searchParams.get("force") === "1";

  try {
    const out = await runDigest(sb, { force });
    return new Response(JSON.stringify(out), {
      status: 200,
      headers: { "content-type": "application/json", "cache-control": "no-store" },
    });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    return new Response(JSON.stringify({ ok: false, error: msg }), {
      status: 500,
      headers: { "content-type": "application/json" },
    });
  }
}

if (import.meta.main) {
  Deno.serve(handleRequest);
}
