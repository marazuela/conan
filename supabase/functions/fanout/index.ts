// Fan-out edge function — runs on alerts.INSERT, candidate_events.INSERT,
// AND v3 convergence_assessments INSERT/UPDATE-into-immediate webhooks.
//
// Email gating revised 2026-04-20 per Pedro's directive (memory: email_alert_gating.md):
// v2 emails fire ONLY after AI review passes (thesis_writer) and the candidate is
// promoted to pre-edge. NOT on raw alerts INSERT.
//
// v3 Stream 1 (2026-05-07) adds entry point D: convergence_assessments rows
// landing with band='immediate' fire the v3 immediate email directly. The v3
// orchestrator runtime IS the AI review — band='immediate' is the post-review
// promotion event, analogous to v2's candidate_events.created.
//
// Four entry points:
//
// A. alerts.INSERT — AUDIT + REALTIME ONLY. No email. The alert row is recorded, the
//    email_body_storage_path is populated so the dashboard can render it server-side,
//    the Realtime `alerts` / `entity:<id>` channels broadcast, and dispatched_at is set.
//    Email dispatch is gated on downstream candidate promotion (entry B).
//
// B. candidate_events.INSERT where event_type='created' — v2 pre-edge promotion email.
//    This is the AI-reviewed, gate-passed, thesis_writer-promoted moment. Candidate lands
//    at state='watch' which is Pedro's "pre-edge" in D-013 terms (pre-edge = any non-
//    killed, non-delivered state after AI review). Email fires here.
//
// C. candidate_events.INSERT where event_type='state_changed' AND payload.to ∈ {killed, delivered}
//    — DISABLED by default per Pedro's 2026-04-20 Q3 answer (email_alert_gating.md:
//    "email only for pre edge"). The code path remains behind a feature flag so the
//    transition template + renderers are preserved for future re-enable. Set
//    `EMAIL_STATE_CHANGE_KILLED_DELIVERED=true` to opt back in.
//
// D. convergence_assessments INSERT or UPDATE-into-immediate (v3 Stream 1).
//    Fires on band='immediate' AND superseded_by IS NULL — the post-orchestrator,
//    post-constitutional, post-calibration band assignment. Renders the v3
//    template (conviction%, ensemble dispersion, thesis_summary, top citations)
//    and dispatches via Resend. Dedup is by (assessment_id, channel, target,
//    day) on alert_deliveries.
//
// Recipients mechanism is shared across B + C + D (notifications_prefs.email_on_immediate).

import { createClient } from "npm:@supabase/supabase-js@2";
import {
  type AssessmentEmailState,
  assessmentSubjectTag,
  deliveryRowFor,
  shouldSendAssessmentImmediateEmail,
} from "./deliveries.ts";
import { formatError } from "../_shared/errors.ts";

interface AlertRow {
  id: string;
  entity_id: string | null;
  signal_id: string;
  signal_fingerprint: string;
  day_utc: string;
}

interface CandidateEventRow {
  id: string;
  candidate_id: string;
  event_type: string;
  payload: Record<string, unknown> | null;
  user_id: string | null;
  created_at: string;
}

interface ConvergenceAssessmentRow {
  id: string;
  asset_id: string;
  trigger_type: string;
  trigger_doc_id: string | null;
  thesis_direction: string | null;
  thesis_summary: string | null;
  conviction_pct: number | null;
  conviction_pct_calibrated: number | null;
  ensemble_dispersion: number | null;
  ensemble_n: number | null;
  band: string | null;
  reference_class: string | null;
  reference_class_base_rate: number | null;
  cited_prose_blocks: unknown;
  evidence_quality: number | null;
  expected_value_bps: number | null;
  market_implied_move: number | null;
  options_iv: number | null;
  superseded_by: string | null;
  document_set_hash: string | null;
  created_at: string;
}

type WebhookPayload =
  | { type: "INSERT"; table: "alerts"; schema: "public"; record: AlertRow; old_record: null }
  | { type: "INSERT"; table: "candidate_events"; schema: "public"; record: CandidateEventRow; old_record: null }
  | { type: "INSERT" | "UPDATE"; table: "convergence_assessments"; schema: "public"; record: ConvergenceAssessmentRow; old_record: ConvergenceAssessmentRow | null };

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SERVICE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const WEBHOOK_SECRET = Deno.env.get("WEBHOOK_SECRET") ?? "";
const RESEND_API_KEY = Deno.env.get("RESEND_API_KEY") ?? "";
const FROM_ADDRESS = Deno.env.get("RESEND_FROM_ADDRESS") ?? "Conan Alerts <alerts@alerts.solutz.com>";
const DASHBOARD_URL = Deno.env.get("DASHBOARD_URL") ?? "https://conan.example.com";
const DEV_RECIPIENTS = (Deno.env.get("FAN_OUT_DEV_RECIPIENTS") ?? "")
  .split(",")
  .map((s) => s.trim())
  .filter(Boolean);

// Feature flag: killed/delivered transition emails. Default OFF per Pedro's 2026-04-20
// directive ("email only for pre edge" — email_alert_gating.md Q3 answer). Code path
// preserved behind the flag so re-enable is a one-env-var change.
const EMAIL_STATE_CHANGE_KILLED_DELIVERED =
  (Deno.env.get("EMAIL_STATE_CHANGE_KILLED_DELIVERED") ?? "false").toLowerCase() === "true";

const sb = createClient(SUPABASE_URL, SERVICE_KEY, {
  auth: { autoRefreshToken: false, persistSession: false },
});

Deno.serve(async (req: Request) => {
  // Constant-time secret compare — see timingSafeEqual below.
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
  // INSERT is the standard path for all 4 entry points. UPDATE is accepted only
  // for convergence_assessments band-flip (a non-immediate row promoted to
  // 'immediate' by a later orchestrator pass — gated on the trigger predicate
  // OLD.band IS DISTINCT FROM 'immediate', so this never fires on re-stamps).
  const isAcceptableConvergenceUpdate = payload.type === "UPDATE" &&
    payload.table === "convergence_assessments";
  if (payload.type !== "INSERT" && !isAcceptableConvergenceUpdate) {
    return new Response(
      JSON.stringify({ skipped: "unsupported event type" }),
      { status: 200 },
    );
  }

  try {
    if (payload.table === "alerts") {
      // AUDIT + REALTIME ONLY path — no email off alerts.INSERT per 2026-04-20 gating.
      const out = await dispatchAlertAuditOnly(payload.record);
      return new Response(JSON.stringify(out), {
        status: 200, headers: { "content-type": "application/json" },
      });
    }
    if (payload.table === "convergence_assessments") {
      // v3 entry point D: orchestrator-produced immediate-band assessment.
      // Trigger predicate already filters to (band='immediate' AND
      // superseded_by IS NULL); defensive checks again here.
      const row = payload.record;
      if (row.band !== "immediate" || row.superseded_by !== null) {
        return new Response(
          JSON.stringify({ skipped: "not_immediate_or_superseded" }),
          { status: 200 },
        );
      }
      const out = await dispatchAssessmentImmediate(row);
      return new Response(JSON.stringify(out), {
        status: 200, headers: { "content-type": "application/json" },
      });
    }
    if (payload.table === "candidate_events") {
      const evt = payload.record;
      const newState = (evt.payload as Record<string, string> | null)?.to;

      // Thesis writer honest-decline rows are operator-visible audit events,
      // not pages. Keep this before every candidate email branch.
      const rd = (evt.payload as Record<string, unknown> | null)?.routine_declined;
      if (rd === true || rd === "true") {
        return new Response(
          JSON.stringify({ skipped: "routine_declined" }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }

      // Email path B: pre-edge promotion (thesis_writer → candidate row created).
      if (evt.event_type === "created" || evt.event_type === "thesis_drafted_by_claude") {
        const out = await dispatchPreEdgePromotion(evt);
        return new Response(JSON.stringify(out), {
          status: 200, headers: { "content-type": "application/json" },
        });
      }

      // Email path C: killed/delivered transition (feature-flagged).
      if (
        EMAIL_STATE_CHANGE_KILLED_DELIVERED &&
        evt.event_type === "state_changed" &&
        (newState === "killed" || newState === "delivered")
      ) {
        const out = await dispatchCandidateEvent(evt);
        return new Response(JSON.stringify(out), {
          status: 200, headers: { "content-type": "application/json" },
        });
      }

      return new Response(JSON.stringify({ skipped: "event_type or target state not emailable" }), {
        status: 200, headers: { "content-type": "application/json" },
      });
    }
    return new Response(JSON.stringify({ skipped: "unsupported table" }), { status: 200 });
  } catch (err) {
    const info = formatError(err);
    return new Response(JSON.stringify({ error: info.message, code: info.code, details: info.details, hint: info.hint }), {
      status: 500, headers: { "content-type": "application/json" },
    });
  }
});

// Pre-render the would-be email body + broadcast to Realtime; do NOT send Resend.
// Emails are gated on downstream candidate promotion (dispatchPreEdgePromotion).
async function dispatchAlertAuditOnly(alert: AlertRow) {
  // --- Load signal + entity + rationale in parallel (for audit body).
  const [sigRes, entityRes] = await Promise.all([
    sb.from("signals").select("*").eq("signal_id", alert.signal_id).limit(1).single(),
    alert.entity_id
      ? sb.from("entities").select("id,name,primary_ticker,primary_mic").eq("id", alert.entity_id).single()
      : Promise.resolve({ data: null, error: null }),
  ]);
  if (sigRes.error) throw sigRes.error;
  const signal = sigRes.data;
  const entity = (entityRes as { data: EntityRow | null }).data ?? null;

  const rationale = entity?.primary_ticker
    ? (await sb.from("candidate_rationales").select("one_liner,thesis,kill_watch,catalyst_date_iso")
        .eq("ticker", entity.primary_ticker).maybeSingle()).data
    : null;

  // --- Render and persist the audit body (dashboard can render from Storage).
  const subject = renderSubject(signal, entity);
  const html = renderHtml(signal, entity, rationale);
  const storagePath = `alerts/${alert.day_utc.slice(0, 4)}/${alert.day_utc.slice(5, 7)}/${alert.id}.html`;
  await sb.storage.from("reports").upload(storagePath, new Blob([html], { type: "text/html" }), {
    upsert: true, contentType: "text/html",
  });
  await sb.from("alerts").update({
    email_subject: subject,
    email_body_storage_path: storagePath,
  }).eq("id", alert.id);

  // --- Realtime broadcast on `alerts` and per-entity channel (dashboard feed).
  const realtime_channels = ["alerts"];
  if (alert.entity_id) realtime_channels.push(`entity:${alert.entity_id}`);
  for (const ch of realtime_channels) {
    try {
      await sb.channel(ch).send({
        type: "broadcast",
        event: "alert",
        payload: { alert_id: alert.id, signal_id: alert.signal_id, subject },
      });
    } catch {
      // Realtime best-effort.
    }
  }

  // --- Close the alert row. `dispatched_to` stays empty; email is not sent here.
  await sb.from("alerts").update({
    dispatched_at: new Date().toISOString(),
    dispatched_to: [],
  }).eq("id", alert.id);

  return {
    processed: true,
    email_recipients: 0,                // gate: no email off alerts.INSERT
    realtime_channels,
    storage_path: storagePath,
    email_gate: "pre-edge-promotion-required",
  };
}

async function resolveRecipients(): Promise<string[]> {
  // notifications_prefs → auth.users join isn't directly queryable via REST; use two hops.
  const { data: prefs, error } = await sb
    .from("notifications_prefs")
    .select("user_id")
    .eq("email_on_immediate", true);
  if (error) throw error;

  const emails: string[] = [];
  if (prefs && prefs.length > 0) {
    // Use the admin client to resolve emails for each user_id.
    const { data: userList, error: uErr } = await sb.auth.admin.listUsers({ perPage: 200 });
    if (uErr) throw uErr;
    const byId = new Map((userList?.users ?? []).map((u) => [u.id, u.email ?? ""]));
    for (const p of prefs) {
      const email = byId.get((p as { user_id: string }).user_id);
      if (email) emails.push(email);
    }
  }

  if (emails.length === 0 && DEV_RECIPIENTS.length > 0) return DEV_RECIPIENTS;
  return emails;
}

// ----------------------------------------------------------------------
// Email rendering — simple for Phase 1; matches spec.md Appendix D stub.
// ----------------------------------------------------------------------

interface EntityRow {
  id: string;
  name: string;
  primary_ticker: string | null;
  primary_mic: string | null;
}

// 2026-04-24: when ticker is NULL (unresolved entity), the subject used to
// render as "?.?" which was the most visible symptom of the caption-as-entity
// bug. Fall back to a truncated entity name instead so operators can see at
// a glance what's being alerted even without a ticker.
function _labelForSubject(entity: EntityRow | null): string {
  const ticker = entity?.primary_ticker;
  const mic = entity?.primary_mic;
  if (ticker) {
    return `${ticker}.${mic ?? "?"}`;
  }
  const name = entity?.name ?? "";
  if (name) {
    return name.length > 40 ? `${name.slice(0, 40)}…` : name;
  }
  return "?.?";
}

function renderSubject(sig: Record<string, unknown>, entity: EntityRow | null): string {
  const band = (sig.band_with_bonus ?? sig.band) as string;
  const label = _labelForSubject(entity);
  return `[IMMEDIATE] ${label} — ${sig.signal_type} — ${band}`;
}

interface Rationale {
  one_liner: string;
  thesis: string;
  kill_watch: string;
  catalyst_date_iso: string | null;
}

function renderHtml(sig: Record<string, unknown>, entity: EntityRow | null, rat: Rationale | null): string {
  const label = _labelForSubject(entity);
  const name = entity?.name ?? "Unknown entity";
  const oneLiner = rat?.one_liner ?? (sig.signal_type as string);
  const band = (sig.band_with_bonus ?? sig.band) as string;
  const score = sig.score_with_bonus ?? sig.score;
  const sourceUrl = (sig.source_url as string) ?? "#";
  const thesisText = rat?.thesis ?? "Thesis pending curation.";
  const killText = rat?.kill_watch ?? "";
  const catalystDate = rat?.catalyst_date_iso ?? "—";
  const convergenceBonus = sig.convergence_bonus ?? 0;
  const baseScore = sig.score ?? 0;
  const dashSignal = `${DASHBOARD_URL}/signals/${sig.signal_id}`;
  return `<!DOCTYPE html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:640px;margin:0 auto;padding:24px;">
  <h1 style="color:#8b0000;margin-bottom:4px;">${escapeHtml(label)} — ${escapeHtml(oneLiner)}</h1>
  <p style="color:#555;margin-top:0;">${escapeHtml(name)} · ${escapeHtml(sig.scoring_profile as string)}</p>
  <table style="width:100%;border-collapse:collapse;margin:16px 0;">
    <tr><td>Band</td><td><strong>${band}</strong> (score ${score} = ${baseScore} + ${convergenceBonus})</td></tr>
    <tr><td>Signal type</td><td>${escapeHtml(sig.signal_type as string)}</td></tr>
    <tr><td>Source</td><td><a href="${escapeHtml(sourceUrl)}">${escapeHtml(sourceUrl)}</a></td></tr>
    <tr><td>Catalyst</td><td>${escapeHtml(catalystDate)}</td></tr>
  </table>
  <h3>Why this is immediate</h3>
  <p>${escapeHtml(thesisText)}</p>
  ${killText ? `<h3>Kill watch</h3><p>${escapeHtml(killText)}</p>` : ""}
  <p style="margin-top:24px;">
    <a href="${escapeHtml(dashSignal)}" style="background:#111;color:#fff;padding:10px 16px;text-decoration:none;">Open in dashboard</a>
  </p>
</body></html>`;
}

function renderText(sig: Record<string, unknown>, entity: EntityRow | null, rat: Rationale | null): string {
  const label = _labelForSubject(entity);
  const oneLiner = rat?.one_liner ?? (sig.signal_type as string);
  const band = (sig.band_with_bonus ?? sig.band) as string;
  const score = sig.score_with_bonus ?? sig.score;
  const baseScore = sig.score ?? 0;
  const bonus = sig.convergence_bonus ?? 0;
  const catalystDate = rat?.catalyst_date_iso ?? "—";
  return [
    `[IMMEDIATE] ${label} — ${sig.signal_type}`,
    "",
    oneLiner,
    "",
    `Band: ${band} (score ${score} = ${baseScore} + ${bonus})`,
    `Source: ${sig.source_url ?? "#"}`,
    `Catalyst: ${catalystDate}`,
    "",
    "Why this is immediate:",
    rat?.thesis ?? "Thesis pending curation.",
    rat?.kill_watch ? `\nKill watch:\n${rat.kill_watch}` : "",
    "",
    `Dashboard: ${DASHBOARD_URL}/signals/${sig.signal_id}`,
  ].join("\n");
}

function escapeHtml(s: string | null | undefined): string {
  if (!s) return "";
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// ======================================================================
// Pre-edge promotion dispatch (new 2026-04-20 gate):
// fires on candidate_events.INSERT where event_type IN ('created','thesis_drafted_by_claude')
// — i.e., thesis_writer just promoted a signal to a pre-edge candidate after AI review.
// This is the only email path for new candidates; raw alerts.INSERT no longer emails.
// ======================================================================

interface ThesisShape {
  situation?: string;
  why_underpriced?: string;
  next_catalyst?: string;
  next_catalyst_date?: string;
  kill_conditions?: string;
  steelman?: string;
  web_research?: Array<{ url?: string; finding?: string; lean?: string; retrieved_at?: string }>;
  structured_kill_conditions?: Array<{ id?: string; description?: string; date_bound?: string }>;
  confidence?: string;
}

async function dispatchPreEdgePromotion(evt: CandidateEventRow) {
  // --- Load candidate + entity.
  const { data: candidate, error: cErr } = await sb
    .from("candidates")
    .select("id,ticker,mic,entity_id,state,scoring_profile,current_score,current_band,next_catalyst_date,dossier_storage_path")
    .eq("id", evt.candidate_id)
    .single();
  if (cErr) throw cErr;
  const cand = candidate as {
    id: string; ticker: string; mic: string | null; entity_id: string | null;
    state: string; scoring_profile: string | null;
    current_score: number | null; current_band: string | null;
    next_catalyst_date: string | null; dossier_storage_path: string | null;
  };

  const entity = cand.entity_id
    ? (await sb.from("entities").select("id,name,primary_ticker,primary_mic").eq("id", cand.entity_id).single()).data as EntityRow | null
    : null;

  // --- Extract thesis from the event payload (thesis_writer stores it there).
  const payload = (evt.payload ?? {}) as Record<string, unknown>;
  const thesis = (payload.thesis ?? {}) as ThesisShape;
  const signal_id = (payload.signal_id as string) ?? null;

  // --- Render email.
  const subject = renderPreEdgeSubject(cand, entity);
  const html = renderPreEdgeHtml(cand, entity, thesis, signal_id);
  const text = renderPreEdgeText(cand, entity, thesis, signal_id);

  // --- Store rendered body.
  const yyyy = evt.created_at.slice(0, 4);
  const mm = evt.created_at.slice(5, 7);
  const storagePath = `promotions/${yyyy}/${mm}/${evt.id}.html`;
  await sb.storage.from("reports").upload(storagePath, new Blob([html], { type: "text/html" }), {
    upsert: true, contentType: "text/html",
  });

  // --- Resolve recipients.
  const recipients = await resolveRecipients();

  const resend_message_ids: string[] = [];
  const sent_to: string[] = [];
  for (const to of recipients) {
    const { data: deliveryRows, error: insErr } = await sb
      .from("alert_deliveries")
      .insert(deliveryRowFor(
        { kind: "candidate_event", candidate_event_id: evt.id, candidate_id: cand.id },
        to,
      ))
      .select("id");
    if (insErr) throw insErr;
    const delivery_id = deliveryRows?.[0]?.id;

    if (!RESEND_API_KEY) {
      await sb.from("alert_deliveries").update({
        status: "failed",
        response_body: { error: "RESEND_API_KEY unset" },
      }).eq("id", delivery_id);
      continue;
    }

    const r = await fetch("https://api.resend.com/emails", {
      method: "POST",
      headers: { "Authorization": `Bearer ${RESEND_API_KEY}`, "Content-Type": "application/json" },
      body: JSON.stringify({ from: FROM_ADDRESS, to: [to], subject, html, text }),
    });
    const body = await r.json().catch(() => ({}));
    if (r.ok) {
      const msgId = (body as { id?: string }).id ?? null;
      await sb.from("alert_deliveries").update({
        status: "sent",
        resend_message_id: msgId,
        response_body: body as Record<string, unknown>,
      }).eq("id", delivery_id);
      if (msgId) resend_message_ids.push(msgId);
      sent_to.push(to);
    } else {
      await sb.from("alert_deliveries").update({
        status: "failed",
        response_body: body as Record<string, unknown>,
      }).eq("id", delivery_id);
    }
  }

  // --- Realtime broadcast.
  const realtime_channels = ["candidates", `candidate:${cand.id}`];
  for (const ch of realtime_channels) {
    try {
      await sb.channel(ch).send({
        type: "broadcast",
        event: "pre_edge_promoted",
        payload: { candidate_id: cand.id, signal_id, subject },
      });
    } catch {
      // Realtime best-effort.
    }
  }

  return {
    processed: true,
    kind: "pre_edge_promotion",
    candidate_id: cand.id,
    email_recipients: sent_to.length,
    realtime_channels,
    resend_message_ids,
    storage_path: storagePath,
  };
}

function renderPreEdgeSubject(
  cand: { ticker: string; mic: string | null; scoring_profile: string | null; current_score: number | null },
  entity: EntityRow | null,
): string {
  const ticker = entity?.primary_ticker ?? cand.ticker ?? "?";
  const mic = entity?.primary_mic ?? cand.mic ?? "?";
  const profile = cand.scoring_profile ?? "—";
  const score = cand.current_score ?? "—";
  return `[PRE-EDGE] ${ticker}.${mic} — ${profile} (score ${score})`;
}

function renderPreEdgeHtml(
  cand: { id: string; ticker: string; mic: string | null; scoring_profile: string | null;
          current_score: number | null; current_band: string | null; next_catalyst_date: string | null },
  entity: EntityRow | null,
  thesis: ThesisShape,
  signal_id: string | null,
): string {
  const ticker = entity?.primary_ticker ?? cand.ticker ?? "?";
  const mic = entity?.primary_mic ?? cand.mic ?? "?";
  const name = entity?.name ?? "Unknown entity";
  const dashCand = `${DASHBOARD_URL}/candidates/${cand.id}`;
  const killRows = (thesis.structured_kill_conditions ?? []).slice(0, 3)
    .map((k) => `<li><strong>${escapeHtml(k.id ?? "")}</strong> — ${escapeHtml(k.description ?? "")}${k.date_bound ? ` <em>(by ${escapeHtml(k.date_bound)})</em>` : ""}</li>`)
    .join("");
  const researchRows = (thesis.web_research ?? []).slice(0, 3)
    .map((r) => `<li><a href="${escapeHtml(r.url ?? "#")}">${escapeHtml(r.url ?? "")}</a> — ${escapeHtml(r.finding ?? "")} <em>[${escapeHtml(r.lean ?? "")}]</em></li>`)
    .join("");
  return `<!DOCTYPE html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:680px;margin:0 auto;padding:24px;">
  <h1 style="color:#1a4a8b;margin-bottom:4px;">${escapeHtml(ticker)}.${escapeHtml(mic)} — ${escapeHtml(name)}</h1>
  <p style="color:#555;margin-top:0;">Pre-edge candidate promoted · ${escapeHtml(cand.scoring_profile ?? "—")} · score ${cand.current_score ?? "—"} ${cand.current_band ? `(${escapeHtml(cand.current_band)})` : ""}</p>

  ${thesis.situation ? `<h3>Situation</h3><p>${escapeHtml(thesis.situation)}</p>` : ""}
  ${thesis.why_underpriced ? `<h3>Why under-priced</h3><p>${escapeHtml(thesis.why_underpriced)}</p>` : ""}
  ${thesis.next_catalyst ? `<h3>Next catalyst</h3><p>${escapeHtml(thesis.next_catalyst)}${thesis.next_catalyst_date ? ` <em>(${escapeHtml(thesis.next_catalyst_date)})</em>` : ""}</p>` : ""}
  ${thesis.steelman ? `<h3>Steelman</h3><p>${escapeHtml(thesis.steelman)}</p>` : ""}
  ${killRows ? `<h3>Kill conditions</h3><ul>${killRows}</ul>` : ""}
  ${researchRows ? `<h3>Web research</h3><ul>${researchRows}</ul>` : ""}

  <p style="margin-top:24px;">
    <a href="${escapeHtml(dashCand)}" style="background:#111;color:#fff;padding:10px 16px;text-decoration:none;">Open dossier</a>
  </p>
  <p style="color:#888;font-size:12px;">Signal ${escapeHtml(signal_id ?? "")}. AI-reviewed via thesis_writer; gate passed. Review and accept/reject in the dashboard.</p>
</body></html>`;
}

function renderPreEdgeText(
  cand: { id: string; ticker: string; mic: string | null; scoring_profile: string | null;
          current_score: number | null; current_band: string | null; next_catalyst_date: string | null },
  entity: EntityRow | null,
  thesis: ThesisShape,
  signal_id: string | null,
): string {
  const ticker = entity?.primary_ticker ?? cand.ticker ?? "?";
  const mic = entity?.primary_mic ?? cand.mic ?? "?";
  const name = entity?.name ?? "Unknown entity";
  const lines: string[] = [
    `[PRE-EDGE] ${ticker}.${mic} — ${name}`,
    `Profile: ${cand.scoring_profile ?? "—"} · score ${cand.current_score ?? "—"} ${cand.current_band ? `(${cand.current_band})` : ""}`,
    "",
  ];
  if (thesis.situation) lines.push(`Situation:`, thesis.situation, "");
  if (thesis.why_underpriced) lines.push(`Why under-priced:`, thesis.why_underpriced, "");
  if (thesis.next_catalyst) lines.push(`Next catalyst: ${thesis.next_catalyst}${thesis.next_catalyst_date ? ` (${thesis.next_catalyst_date})` : ""}`);
  if (thesis.steelman) lines.push(`Steelman:`, thesis.steelman, "");
  if (thesis.structured_kill_conditions?.length) {
    lines.push("Kill conditions:");
    for (const k of thesis.structured_kill_conditions.slice(0, 3)) {
      lines.push(`  ${k.id}: ${k.description}${k.date_bound ? ` (by ${k.date_bound})` : ""}`);
    }
    lines.push("");
  }
  lines.push(`Dossier: ${DASHBOARD_URL}/candidates/${cand.id}`);
  if (signal_id) lines.push(`Signal: ${signal_id}`);
  return lines.join("\n");
}

// ======================================================================
// Candidate state-change dispatch (spec.md §7.5 + Appendix D template).
// ======================================================================

interface CandidateRow {
  id: string;
  ticker: string;
  mic: string | null;
  entity_id: string | null;
  state: string;
  scoring_profile: string | null;
  dossier_storage_path: string | null;
  kill_conditions: Array<Record<string, unknown>> | null;
  next_catalyst_date: string | null;
  last_aging_evaluated_at: string | null;
}

interface KillCondition {
  id: string;
  description?: string;
  observable?: Record<string, unknown>;
  status?: string;
  evidence_url?: string;
  evidence_ts?: string;
}

async function dispatchCandidateEvent(evt: CandidateEventRow) {
  // --- Load candidate + entity in parallel.
  const { data: candidate, error: cErr } = await sb
    .from("candidates")
    .select(
      "id,ticker,mic,entity_id,state,scoring_profile,dossier_storage_path,kill_conditions,next_catalyst_date,last_aging_evaluated_at",
    )
    .eq("id", evt.candidate_id)
    .single();
  if (cErr) throw cErr;
  const cand = candidate as CandidateRow;

  const entity = cand.entity_id
    ? (await sb.from("entities").select("id,name,primary_ticker,primary_mic").eq("id", cand.entity_id).single()).data as EntityRow | null
    : null;

  // --- Resolve the triggered kill_condition (payload.reason carries the kill_id
  //     per spec §7.5 step 4). Fall back to searching kill_conditions for the
  //     first `status='triggered'` entry if reason is absent.
  const payload = (evt.payload ?? {}) as Record<string, unknown>;
  const prevState = (payload.from as string) ?? "unknown";
  const newState = (payload.to as string) ?? cand.state;
  const triggeredKillId = (payload.reason as string | null | undefined) ?? null;
  const killList = (cand.kill_conditions ?? []) as unknown as KillCondition[];
  const triggeredKill = triggeredKillId
    ? killList.find((k) => k.id === triggeredKillId) ?? null
    : killList.find((k) => k.status === "triggered") ?? null;

  const reasonFull = (payload.reasoning as string)
    ?? (payload.kill_reason as string)
    ?? (triggeredKill?.description ?? `state transitioned ${prevState} → ${newState}`);

  // --- Render email bodies.
  const subject = renderStateChangeSubject(cand, entity, newState, triggeredKill);
  const html = renderStateChangeHtml(cand, entity, prevState, newState, reasonFull, triggeredKill);
  const text = renderStateChangeText(cand, entity, prevState, newState, reasonFull, triggeredKill);

  // --- Store rendered body for audit (reports/state-changes/YYYY/MM/<event_id>.html).
  const yyyy = evt.created_at.slice(0, 4);
  const mm = evt.created_at.slice(5, 7);
  const storagePath = `state-changes/${yyyy}/${mm}/${evt.id}.html`;
  await sb.storage.from("reports").upload(storagePath, new Blob([html], { type: "text/html" }), {
    upsert: true, contentType: "text/html",
  });

  // --- Resolve recipients (same pool as Immediate-band alerts for v2).
  const recipients = await resolveRecipients();

  // --- Send via Resend + record alert_deliveries. candidate_event_id is the
  //     audit-parent (CASCADE on delete); alert_id stays NULL on this path.
  const resend_message_ids: string[] = [];
  const sent_to: string[] = [];
  for (const to of recipients) {
    const { data: deliveryRows, error: insErr } = await sb
      .from("alert_deliveries")
      .insert(deliveryRowFor(
        { kind: "candidate_event", candidate_event_id: evt.id, candidate_id: cand.id },
        to,
      ))
      .select("id");
    if (insErr) throw insErr;
    const delivery_id = deliveryRows?.[0]?.id;

    if (!RESEND_API_KEY) {
      await sb.from("alert_deliveries").update({
        status: "failed",
        response_body: { error: "RESEND_API_KEY unset" },
      }).eq("id", delivery_id);
      continue;
    }

    const r = await fetch("https://api.resend.com/emails", {
      method: "POST",
      headers: { "Authorization": `Bearer ${RESEND_API_KEY}`, "Content-Type": "application/json" },
      body: JSON.stringify({ from: FROM_ADDRESS, to: [to], subject, html, text }),
    });
    const body = await r.json().catch(() => ({}));
    if (r.ok) {
      const msgId = (body as { id?: string }).id ?? null;
      await sb.from("alert_deliveries").update({
        status: "sent",
        resend_message_id: msgId,
        response_body: body as Record<string, unknown>,
      }).eq("id", delivery_id);
      if (msgId) resend_message_ids.push(msgId);
      sent_to.push(to);
    } else {
      await sb.from("alert_deliveries").update({
        status: "failed",
        response_body: body as Record<string, unknown>,
      }).eq("id", delivery_id);
    }
  }

  // --- Realtime broadcast on `candidates` and per-candidate channel.
  const realtime_channels = ["candidates", `candidate:${cand.id}`];
  for (const ch of realtime_channels) {
    try {
      await sb.channel(ch).send({
        type: "broadcast",
        event: "state_changed",
        payload: { candidate_id: cand.id, from: prevState, to: newState, subject },
      });
    } catch {
      // Realtime best-effort.
    }
  }

  return {
    processed: true,
    candidate_id: cand.id,
    from: prevState,
    to: newState,
    email_recipients: sent_to.length,
    realtime_channels,
    resend_message_ids,
    storage_path: storagePath,
  };
}

function renderStateChangeSubject(
  cand: CandidateRow,
  entity: EntityRow | null,
  newState: string,
  triggeredKill: KillCondition | null,
): string {
  const ticker = entity?.primary_ticker ?? cand.ticker ?? "?";
  const mic = entity?.primary_mic ?? cand.mic ?? "?";
  const reasonShort = triggeredKill?.description
    ? truncate(triggeredKill.description, 40)
    : (newState === "delivered" ? "catalyst resolved" : "aged out");
  return `[CANDIDATE ${newState.toUpperCase()}] ${ticker}.${mic} — ${reasonShort}`;
}

function renderStateChangeHtml(
  cand: CandidateRow,
  entity: EntityRow | null,
  prevState: string,
  newState: string,
  reasonFull: string,
  triggeredKill: KillCondition | null,
): string {
  const ticker = entity?.primary_ticker ?? cand.ticker ?? "?";
  const mic = entity?.primary_mic ?? cand.mic ?? "?";
  const name = entity?.name ?? "Unknown entity";
  const dashCand = `${DASHBOARD_URL}/candidates/${cand.id}`;
  const barColor = newState === "killed" ? "#8b0000" : "#1a6b3a";
  const killBlock = triggeredKill
    ? `<h3>Triggered kill condition</h3>
       <p><strong>${escapeHtml(triggeredKill.id)}</strong> — ${escapeHtml(triggeredKill.description ?? "")}</p>
       ${triggeredKill.evidence_url ? `<p>Evidence: <a href="${escapeHtml(triggeredKill.evidence_url)}">${escapeHtml(triggeredKill.evidence_url)}</a>${triggeredKill.evidence_ts ? ` <em>(${escapeHtml(triggeredKill.evidence_ts)})</em>` : ""}</p>` : ""}`
    : "";
  const deliveredNote = newState === "delivered"
    ? `<p><em>realized_return is NULL; fill manually in dashboard.</em></p>`
    : "";
  return `<!DOCTYPE html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:640px;margin:0 auto;padding:24px;">
  <h1 style="color:${barColor};margin-bottom:4px;">${escapeHtml(ticker)}.${escapeHtml(mic)} — ${escapeHtml(name)}</h1>
  <p style="color:#555;margin-top:0;">State: <strong>${escapeHtml(prevState)} → ${escapeHtml(newState)}</strong></p>
  <p>${escapeHtml(reasonFull)}</p>
  ${killBlock}
  ${cand.next_catalyst_date ? `<p><small>Catalyst date: ${escapeHtml(cand.next_catalyst_date)}</small></p>` : ""}
  ${cand.last_aging_evaluated_at ? `<p><small>Last aging evaluated: ${escapeHtml(cand.last_aging_evaluated_at)}</small></p>` : ""}
  ${deliveredNote}
  <p style="margin-top:24px;">
    <a href="${escapeHtml(dashCand)}" style="background:#111;color:#fff;padding:10px 16px;text-decoration:none;">Open dossier</a>
  </p>
</body></html>`;
}

function renderStateChangeText(
  cand: CandidateRow,
  entity: EntityRow | null,
  prevState: string,
  newState: string,
  reasonFull: string,
  triggeredKill: KillCondition | null,
): string {
  const ticker = entity?.primary_ticker ?? cand.ticker ?? "?";
  const mic = entity?.primary_mic ?? cand.mic ?? "?";
  const name = entity?.name ?? "Unknown entity";
  const lines = [
    `${ticker}.${mic} — ${name}`,
    `State: ${prevState} → ${newState}`,
    `Reason: ${reasonFull}`,
    "",
  ];
  if (triggeredKill) {
    lines.push(`Triggered kill condition: ${triggeredKill.id} — ${triggeredKill.description ?? ""}`);
    if (triggeredKill.evidence_url) {
      lines.push(`Evidence: ${triggeredKill.evidence_url}`);
      if (triggeredKill.evidence_ts) lines.push(`  (${triggeredKill.evidence_ts})`);
    }
    lines.push("");
  }
  if (cand.next_catalyst_date) lines.push(`Catalyst date: ${cand.next_catalyst_date}`);
  if (cand.last_aging_evaluated_at) lines.push(`Last aging evaluated: ${cand.last_aging_evaluated_at}`);
  if (newState === "delivered") {
    lines.push("");
    lines.push("For delivered candidates: realized_return is NULL; fill manually in dashboard.");
  }
  lines.push("", `Dossier: ${DASHBOARD_URL}/candidates/${cand.id}`);
  return lines.join("\n");
}

function truncate(s: string, n: number): string {
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

// ======================================================================
// v3 Stream 1 — convergence_assessments band='immediate' dispatch.
// Fires on (a) AFTER INSERT WHEN band='immediate', (b) AFTER UPDATE WHEN
// band='immediate' AND OLD.band <> 'immediate'. Renders the v3 template
// (conviction%, ensemble dispersion, thesis_summary, top citations) and
// dispatches to recipients via Resend. Per-day dedupe via the partial
// unique index on (assessment_id, channel, target, date_trunc(day,...)).
// ======================================================================

interface FdaAssetRow {
  id: string;
  ticker: string | null;
  mic: string | null;
  drug_name: string | null;
  generic_name: string | null;
  sponsor_name: string | null;
  indication: string | null;
  indication_normalized: string | null;
  program_status: string | null;
  watch_priority: number | null;
  reference_class_signature: string | null;
  entity_id: string | null;
}

async function dispatchAssessmentImmediate(row: ConvergenceAssessmentRow) {
  // --- Load asset + entity for header context.
  const { data: assetRaw, error: aErr } = await sb
    .from("fda_assets")
    .select(
      "id,ticker,mic,drug_name,generic_name,sponsor_name,indication,indication_normalized,program_status,watch_priority,reference_class_signature,entity_id",
    )
    .eq("id", row.asset_id)
    .maybeSingle();
  if (aErr) throw aErr;
  const asset = (assetRaw ?? null) as FdaAssetRow | null;

  const entity = asset?.entity_id
    ? (await sb.from("entities").select("id,name,primary_ticker,primary_mic").eq("id", asset.entity_id).single()).data as EntityRow | null
    : null;

  // --- Render email bodies + audit storage.
  const subject = renderAssessmentSubject(row, asset, entity);
  const html = renderAssessmentHtml(row, asset, entity);
  const text = renderAssessmentText(row, asset, entity);

  const yyyy = row.created_at.slice(0, 4);
  const mm = row.created_at.slice(5, 7);
  const storagePath = `assessments/${yyyy}/${mm}/${row.id}.html`;
  await sb.storage.from("reports").upload(
    storagePath,
    new Blob([html], { type: "text/html" }),
    { upsert: true, contentType: "text/html" },
  );

  // --- Resolve recipients (same pool as v2 immediate alerts).
  const recipients = await resolveRecipients();

  const resend_message_ids: string[] = [];
  const sent_to: string[] = [];
  let dedupe_skipped = 0;
  let asset_dedupe_skipped = 0;
  for (const to of recipients) {
    const prior = await loadPriorAssessmentEmailState(row.asset_id, to, row.id);
    const currentState = assessmentEmailStateFromRow(row);
    const gate = shouldSendAssessmentImmediateEmail(currentState, prior);
    if (!gate.send) {
      asset_dedupe_skipped += 1;
      continue;
    }

    // Per-recipient subject prefix. The gate.reason already tells us WHY
    // we're sending this email; surface it in the subject so the recipient
    // can distinguish [NEW] / [DIRECTION CHANGE] / [Δ+12pp] / [REFRESH]
    // at a glance instead of seeing the same [IMMEDIATE] · LONG · cross_source
    // line every day.
    const tag = assessmentSubjectTag(currentState, prior, gate.reason);
    const taggedSubject = tag ? `[IMMEDIATE] [${tag}] ${subjectBody(subject)}` : subject;

    // Dedupe: insert (assessment_id, channel, target, day) — partial unique
    // index throws 23505 on a same-day duplicate.
    const { data: deliveryRows, error: insErr } = await sb
      .from("alert_deliveries")
      .insert(deliveryRowFor({ kind: "assessment", assessment_id: row.id }, to))
      .select("id");
    if (insErr) {
      const code = (insErr as { code?: string }).code;
      if (code === "23505") {
        // Same-day duplicate — orchestrator re-stamped this assessment, or a
        // concurrent webhook retry. No-op.
        dedupe_skipped += 1;
        continue;
      }
      throw insErr;
    }
    const delivery_id = deliveryRows?.[0]?.id;

    if (!RESEND_API_KEY) {
      await sb.from("alert_deliveries").update({
        status: "failed",
        response_body: { error: "RESEND_API_KEY unset" },
      }).eq("id", delivery_id);
      continue;
    }

    const r = await fetch("https://api.resend.com/emails", {
      method: "POST",
      headers: { "Authorization": `Bearer ${RESEND_API_KEY}`, "Content-Type": "application/json" },
      body: JSON.stringify({ from: FROM_ADDRESS, to: [to], subject: taggedSubject, html, text }),
    });
    const body = await r.json().catch(() => ({}));
    if (r.ok) {
      const msgId = (body as { id?: string }).id ?? null;
      await sb.from("alert_deliveries").update({
        status: "sent",
        resend_message_id: msgId,
        response_body: body as Record<string, unknown>,
      }).eq("id", delivery_id);
      if (msgId) resend_message_ids.push(msgId);
      sent_to.push(to);
    } else {
      await sb.from("alert_deliveries").update({
        status: "failed",
        response_body: body as Record<string, unknown>,
      }).eq("id", delivery_id);
    }
  }

  // --- Realtime broadcast.
  const realtime_channels = ["assessments", `asset:${row.asset_id}`];
  for (const ch of realtime_channels) {
    try {
      await sb.channel(ch).send({
        type: "broadcast",
        event: "immediate_assessment",
        payload: { assessment_id: row.id, asset_id: row.asset_id, subject },
      });
    } catch {
      // Realtime best-effort.
    }
  }

  return {
    processed: true,
    kind: "convergence_assessment_immediate",
    assessment_id: row.id,
    asset_id: row.asset_id,
    email_recipients: sent_to.length,
    dedupe_skipped,
    asset_dedupe_skipped,
    realtime_channels,
    resend_message_ids,
    storage_path: storagePath,
  };
}

function assessmentEmailStateFromRow(row: ConvergenceAssessmentRow): AssessmentEmailState {
  return {
    asset_id: row.asset_id,
    band: row.band,
    document_set_hash: row.document_set_hash,
    thesis_direction: row.thesis_direction,
    conviction_pct: row.conviction_pct,
    conviction_pct_calibrated: row.conviction_pct_calibrated,
    created_at: row.created_at,
  };
}

async function loadPriorAssessmentEmailState(
  assetId: string,
  target: string,
  excludeAssessmentId: string,
): Promise<AssessmentEmailState | null> {
  // Query same-asset assessments first. Scanning the recipient's latest N
  // deliveries globally can miss the last email for a quiet asset when the
  // recipient has received many unrelated asset emails since then.
  const { data: assessmentRows, error: assessmentErr } = await sb
    .from("convergence_assessments")
    .select(
      "id,asset_id,band,document_set_hash,thesis_direction,conviction_pct,conviction_pct_calibrated,created_at",
    )
    .eq("asset_id", assetId)
    .neq("id", excludeAssessmentId)
    .order("created_at", { ascending: false })
    .limit(50);
  if (assessmentErr) throw assessmentErr;

  const assessmentIds = (assessmentRows ?? [])
    .map((r: { id: string | null }) => r.id)
    .filter((id: string | null): id is string => Boolean(id));
  if (assessmentIds.length === 0) return null;

  const { data: deliveryRows, error: deliveryErr } = await sb
    .from("alert_deliveries")
    .select("assessment_id,created_at")
    .eq("channel", "email")
    .eq("target", target)
    .in("status", ["queued", "sent"])
    .in("assessment_id", assessmentIds)
    .order("created_at", { ascending: false });
  if (deliveryErr) throw deliveryErr;

  const deliveredAtByAssessmentId = new Map<string, string>();
  for (const row of deliveryRows ?? []) {
    const assessmentId = (row as { assessment_id: string | null }).assessment_id;
    const deliveredAt = (row as { created_at: string | null }).created_at;
    if (assessmentId && deliveredAt && !deliveredAtByAssessmentId.has(assessmentId)) {
      deliveredAtByAssessmentId.set(assessmentId, deliveredAt);
    }
  }

  const prior = (assessmentRows ?? []).find((row: { id: string | null }) =>
    Boolean(row.id && deliveredAtByAssessmentId.has(row.id))
  );
  if (!prior) return null;

  const priorWithDeliveryTime = {
    ...(prior as Record<string, unknown>),
    created_at: deliveredAtByAssessmentId.get((prior as { id: string }).id) ?? prior.created_at,
  };
  delete priorWithDeliveryTime.id;
  return priorWithDeliveryTime as unknown as AssessmentEmailState;
}

function _assetLabel(asset: FdaAssetRow | null, entity: EntityRow | null): string {
  const ticker = asset?.ticker ?? entity?.primary_ticker ?? null;
  const mic = asset?.mic ?? entity?.primary_mic ?? null;
  if (ticker) return `${ticker}${mic ? `.${mic}` : ""}`;
  const name = entity?.name ?? asset?.sponsor_name ?? asset?.drug_name ?? "";
  if (name) return name.length > 40 ? `${name.slice(0, 40)}…` : name;
  return "?";
}

function _formatConviction(row: ConvergenceAssessmentRow): string {
  // Prefer calibrated; fall back to raw conviction_pct.
  const v = row.conviction_pct_calibrated ?? row.conviction_pct;
  if (v === null || v === undefined) return "—";
  const dispersion = row.ensemble_dispersion;
  if (dispersion !== null && dispersion !== undefined) {
    const lo = Math.max(0, Math.round(v - dispersion));
    const hi = Math.min(100, Math.round(v + dispersion));
    return `${Math.round(v)}% [${lo}–${hi}]`;
  }
  return `${Math.round(v)}%`;
}

function renderAssessmentSubject(
  row: ConvergenceAssessmentRow,
  asset: FdaAssetRow | null,
  entity: EntityRow | null,
): string {
  const label = _assetLabel(asset, entity);
  const conviction = _formatConviction(row);
  const direction = row.thesis_direction
    ? row.thesis_direction.toUpperCase()
    : "—";
  return `[IMMEDIATE] ${label} · ${direction} ${conviction} · ${row.trigger_type}`;
}

// Strip the leading "[IMMEDIATE] " prefix so callers can splice in a
// per-recipient semantic tag and reassemble: "[IMMEDIATE] [NEW] <body>".
// Defensive: if the prefix isn't there for any reason, return as-is.
function subjectBody(subject: string): string {
  const PREFIX = "[IMMEDIATE] ";
  return subject.startsWith(PREFIX) ? subject.slice(PREFIX.length) : subject;
}

interface CitedBlock {
  citation_ref?: number | string;
  text?: string;
  document_id?: string;
  span_start?: number;
  span_end?: number;
  snippet?: string;
}

function _topCitations(row: ConvergenceAssessmentRow, k: number): CitedBlock[] {
  const blocks = row.cited_prose_blocks;
  if (!Array.isArray(blocks)) return [];
  return (blocks as CitedBlock[]).slice(0, k);
}

function renderAssessmentHtml(
  row: ConvergenceAssessmentRow,
  asset: FdaAssetRow | null,
  entity: EntityRow | null,
): string {
  const label = _assetLabel(asset, entity);
  const name = entity?.name ?? asset?.sponsor_name ?? "Unknown sponsor";
  const assetName = asset?.drug_name ?? asset?.generic_name ?? "—";
  const indication = asset?.indication_normalized ?? asset?.indication ?? "—";
  const programStatus = asset?.program_status ?? "—";
  const conviction = _formatConviction(row);
  const direction = row.thesis_direction ?? "—";
  const dispersion = row.ensemble_dispersion;
  const ensembleN = row.ensemble_n ?? null;
  const refClass = row.reference_class ?? asset?.reference_class_signature ?? "—";
  const refRate = row.reference_class_base_rate;
  const refRatePct = refRate !== null && refRate !== undefined
    ? `${Math.round(Number(refRate) * 100)}%`
    : "—";
  const evQuality = row.evidence_quality;
  const ev = row.expected_value_bps;
  const evPct = ev !== null && ev !== undefined ? (Number(ev) / 100).toFixed(2) : "—";
  const dashUrl = `${DASHBOARD_URL}/fda/${row.id}`;
  const thesis = row.thesis_summary ?? "(thesis summary unavailable)";

  const cites = _topCitations(row, 5)
    .map((c) => {
      const ref = c.citation_ref !== undefined ? `[${escapeHtml(String(c.citation_ref))}]` : "";
      const snippet = c.snippet ?? c.text ?? "";
      return `<li>${ref} ${escapeHtml(truncate(snippet, 240))}</li>`;
    })
    .join("");

  return `<!DOCTYPE html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:680px;margin:0 auto;padding:24px;">
  <h1 style="color:#1a4a8b;margin-bottom:4px;">${escapeHtml(label)} — ${escapeHtml(assetName)}</h1>
  <p style="color:#555;margin-top:0;">
    ${escapeHtml(name)} · ${escapeHtml(indication)} · program: ${escapeHtml(programStatus)}
  </p>

  <table style="width:100%;border-collapse:collapse;margin:16px 0;">
    <tr><td>Conviction</td><td><strong>${escapeHtml(conviction)}</strong> · ${escapeHtml(direction)}${ensembleN ? ` · ensemble n=${ensembleN}` : ""}${dispersion !== null && dispersion !== undefined ? ` · σ=${Number(dispersion).toFixed(1)}` : ""}</td></tr>
    <tr><td>Reference class</td><td>${escapeHtml(refClass)} · base rate ${escapeHtml(refRatePct)}</td></tr>
    <tr><td>Evidence quality</td><td>${evQuality !== null && evQuality !== undefined ? Number(evQuality).toFixed(2) : "—"}</td></tr>
    <tr><td>Expected value</td><td>${escapeHtml(evPct)}%${row.market_implied_move !== null ? ` · IM ${Number(row.market_implied_move).toFixed(1)}%` : ""}${row.options_iv !== null ? ` · IV ${Number(row.options_iv).toFixed(1)}%` : ""}</td></tr>
    <tr><td>Trigger</td><td>${escapeHtml(row.trigger_type)}</td></tr>
  </table>

  <h3>Thesis</h3>
  <p>${escapeHtml(thesis)}</p>

  ${cites ? `<h3>Cited evidence</h3><ul>${cites}</ul>` : ""}

  <p style="margin-top:24px;">
    <a href="${escapeHtml(dashUrl)}" style="background:#111;color:#fff;padding:10px 16px;text-decoration:none;">Open in dashboard</a>
  </p>
  <p style="color:#888;font-size:12px;">Assessment ${escapeHtml(row.id)}. v3 orchestrator · constitutional pass · isotonic-calibrated.</p>
</body></html>`;
}

function renderAssessmentText(
  row: ConvergenceAssessmentRow,
  asset: FdaAssetRow | null,
  entity: EntityRow | null,
): string {
  const label = _assetLabel(asset, entity);
  const name = entity?.name ?? asset?.sponsor_name ?? "Unknown sponsor";
  const assetName = asset?.drug_name ?? asset?.generic_name ?? "—";
  const indication = asset?.indication_normalized ?? asset?.indication ?? "—";
  const conviction = _formatConviction(row);
  const direction = row.thesis_direction ?? "—";
  const refClass = row.reference_class ?? asset?.reference_class_signature ?? "—";
  const lines: string[] = [
    `[IMMEDIATE] ${label} — ${assetName}`,
    `${name} · ${indication} · program: ${asset?.program_status ?? "—"}`,
    "",
    `Conviction: ${conviction} · ${direction}`,
    `Reference class: ${refClass}`,
    `Trigger: ${row.trigger_type}`,
    "",
    "Thesis:",
    row.thesis_summary ?? "(thesis summary unavailable)",
    "",
  ];
  const cites = _topCitations(row, 5);
  if (cites.length > 0) {
    lines.push("Cited evidence:");
    for (const c of cites) {
      const ref = c.citation_ref !== undefined ? `[${c.citation_ref}] ` : "";
      const snippet = c.snippet ?? c.text ?? "";
      lines.push(`  ${ref}${truncate(snippet, 240)}`);
    }
    lines.push("");
  }
  lines.push(`Dashboard: ${DASHBOARD_URL}/fda/${row.id}`);
  return lines.join("\n");
}

// Constant-time string compare — see reactor/index.ts for the same pattern.
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
