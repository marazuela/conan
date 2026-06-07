// =============================================================================
// bc-digest / render.ts — the PURE deterministic digest renderer (Phase 3 §1).
//
// renderDigestHtml(rows, today, opts) / renderDigestText(rows, today, opts):
//   pure functions of the bc_digest_rows() row list (§2.1). NO LLM, NO DB, NO
//   network — string-building only. The §1.2 synthesis-field -> layout map and
//   the band-only v1 rules (§1.3) live here.
//
// BAND-ONLY v1 (Pedro 2026-06-03): there is NO market-implied-move column and NO
// per-cell "implied move unavailable" string and NO global options tier-note.
// The band (risk_band + oof_percentile_rank) is the only risk number. The
// synthesis contract still CARRIES risk_vs_market.options_implied_move_pct
// (dormant/null), so v1.1 lights up a column with near-zero render change — but
// this renderer ignores those fields entirely in v1.
//
// INVARIANT (load-bearing): p_crl is NEVER rendered. The DigestRow type below has
// no p_crl field (bc_digest_rows omits it structurally, §2.1), so it cannot reach
// this renderer; the render tests additionally assert no p_crl token appears.
//
// escapeHtml is ported verbatim from supabase/functions/fanout/index.ts:403
// (the function is NOT shared — strangle-don't-entangle; we copy the ~8 lines).
// =============================================================================

// ---- Synthesis contract subset the digest renders (Phase 2 §1.1; §1.2 map) ----
// Only the fields the digest reads are typed; unknown fields are ignored.
export interface RiskVsMarket {
  model_risk_band?: string | null;
  model_percentile?: number | null;
  // v1.1 ONLY — carried dormant in v1, NOT rendered:
  options_implied_move_pct?: number | null;
  implied_move_horizon?: string | null;
  stance?: string | null;
  gap_bps?: number | null;
  rationale?: string | null;
}

export interface Driver {
  stream?: string | null;
  direction?: string | null;
  magnitude?: string | null;
  summary?: string | null;
  evidence_ref?: unknown; // retained for Phase-4 deep-links; not rendered in v1
}

export interface StreamsAvailable {
  insider?: boolean | null;
  options?: boolean | null;
  news?: boolean | null;
}

export interface Synthesis {
  headline?: string | null;
  what_changed?: string | null;
  risk_vs_market?: RiskVsMarket | null;
  drivers?: Driver[] | null;
  bullets_up?: string[] | null;
  bullets_down?: string[] | null;
  risks?: string[] | null;
  watch_items?: string[] | null;
  recommended_action?: string | null; // monitor | investigate | exit
  confidence?: number | null;
  provenance?: { streams_available?: StreamsAvailable | null } | null;
}

// ---- The row the renderer consumes (the bc_digest_rows() shape — NO p_crl) ----
export interface DigestRow {
  application_number: string;
  risk_band: string | null;
  oof_percentile_rank: number | null;
  appl_type: string | null;
  pdufa_date: string | null;
  days_to_pdufa: number | null;
  tier: string | null;
  materialized_at: string | null;
  ticker: string | null;
  synthesis: Synthesis | null;
  trigger_reasons: string[] | null;
  fired_at: string | null;
  // NOTE: deliberately no `p_crl` field — the invariant is structural.
}

export interface RenderOptions {
  // confidence floor for investigate/exit to flag a name (bc_config
  // l4.digest_flag_min_confidence, default 0.6). Injected so tests pin it.
  flagMinConfidence?: number;
  // matview-staleness note threshold in days (§9 risk 3); default 8.
  staleMatviewDays?: number;
}

const DEFAULT_FLAG_MIN_CONFIDENCE = 0.6;
const DEFAULT_STALE_MATVIEW_DAYS = 8;

// ---------------------------------------------------------------------------
// escapeHtml — ported verbatim from fanout/index.ts:403 (copy, not import).
// ---------------------------------------------------------------------------
export function escapeHtml(s: string | null | undefined): string {
  if (!s) return "";
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// ---------------------------------------------------------------------------
// Flag gate (pure, deterministic — no LLM, no threshold re-derivation). §1.1/§2.2
//   flagged iff today's synthesis exists AND recommended_action ∈ {investigate,
//   exit} AND confidence >= flagMinConfidence.
// ---------------------------------------------------------------------------
export function isFlagged(row: DigestRow, flagMinConfidence: number): boolean {
  const syn = row.synthesis;
  if (!syn) return false;
  const action = syn.recommended_action;
  if (action !== "investigate" && action !== "exit") return false;
  const conf = Number(syn.confidence);
  if (!Number.isFinite(conf)) return false;
  return conf >= flagMinConfidence;
}

// ---------------------------------------------------------------------------
// Sort: PDUFA proximity + today's delta, NOT the band (the band is degenerate on
// live names — never the sort key; plan W3 + risk register). A row whose
// synthesis fired today (a "delta") sorts ahead of a quiet row; within each
// group, nearest PDUFA first.
// ---------------------------------------------------------------------------
export function sortRows(rows: DigestRow[]): DigestRow[] {
  const daysOf = (r: DigestRow): number =>
    r.days_to_pdufa === null || r.days_to_pdufa === undefined
      ? Number.POSITIVE_INFINITY
      : r.days_to_pdufa;
  const firedOf = (r: DigestRow): number => (r.synthesis ? 0 : 1); // fired-today first
  return [...rows].sort((a, b) => {
    if (firedOf(a) !== firedOf(b)) return firedOf(a) - firedOf(b);
    return daysOf(a) - daysOf(b);
  });
}

// ---------------------------------------------------------------------------
// Small formatters
// ---------------------------------------------------------------------------
function ordinal(n: number): string {
  const v = Math.round(n);
  const s = ["th", "st", "nd", "rd"];
  const m = v % 100;
  return v + (s[(m - 20) % 10] || s[m] || s[0]);
}

function bandBadge(row: DigestRow): string {
  // band + percentile, standing alone — the v1 risk read (§1.2). NEVER p_crl.
  const band = row.risk_band ?? "—";
  if (row.oof_percentile_rank === null || row.oof_percentile_rank === undefined) {
    return band;
  }
  return `${band} · ${ordinal(row.oof_percentile_rank)} pct`;
}

function nameLabel(row: DigestRow): string {
  if (row.ticker) return row.ticker;
  return row.application_number;
}

function appnoLabel(row: DigestRow): string {
  const t = row.appl_type ? row.appl_type : "";
  return t ? `${t}-${row.application_number}` : row.application_number;
}

function streamsFootnote(syn: Synthesis | null, days: number | null): string {
  const sa = syn?.provenance?.streams_available ?? {};
  const mark = (b: boolean | null | undefined) => (b ? "✓" : "✗");
  const parts = [
    `insider ${mark(sa.insider)}`,
    `options ${mark(sa.options)}`,
    `news ${mark(sa.news)}`,
  ];
  if (days !== null && days !== undefined) parts.push(`PDUFA in ${days}d`);
  return `streams: ${parts.join(" · ")}`;
}

function truncate(s: string, n: number): string {
  if (s.length <= n) return s;
  return s.slice(0, n - 1) + "…";
}

function isStaleMatview(rows: DigestRow[], today: string, staleDays: number): boolean {
  // The oldest materialized_at across rows; if > staleDays old, note it (§9 risk 3).
  let oldest: number | null = null;
  for (const r of rows) {
    if (!r.materialized_at) continue;
    const t = Date.parse(r.materialized_at);
    if (Number.isNaN(t)) continue;
    if (oldest === null || t < oldest) oldest = t;
  }
  if (oldest === null) return false;
  const todayMs = Date.parse(today + "T00:00:00Z");
  if (Number.isNaN(todayMs)) return false;
  const ageDays = (todayMs - oldest) / 86_400_000;
  return ageDays > staleDays;
}

// ---------------------------------------------------------------------------
// Subject (§1.5)
// ---------------------------------------------------------------------------
export function renderSubject(rows: DigestRow[], today: string, opts?: RenderOptions): string {
  const floor = opts?.flagMinConfidence ?? DEFAULT_FLAG_MIN_CONFIDENCE;
  const nFlagged = rows.filter((r) => isFlagged(r, floor)).length;
  const nWatch = rows.length;
  const flaggedPhrase = nFlagged === 0 ? "nothing flagged" : `${nFlagged} flagged`;
  return `[BC-FDA] ${today} — ${flaggedPhrase} · ${nWatch} watched`;
}

// ---------------------------------------------------------------------------
// HTML render (§1.2 card map + §1.4 table; band-only v1 — NO implied-move column)
// ---------------------------------------------------------------------------
export function renderDigestHtml(rows: DigestRow[], today: string, opts?: RenderOptions): string {
  const floor = opts?.flagMinConfidence ?? DEFAULT_FLAG_MIN_CONFIDENCE;
  const staleDays = opts?.staleMatviewDays ?? DEFAULT_STALE_MATVIEW_DAYS;
  const sorted = sortRows(rows);
  const flagged = sorted.filter((r) => isFlagged(r, floor));
  const nWatch = sorted.length;

  const cards = flagged.map((r) => renderCardHtml(r)).join("\n");

  const tableRows = sorted
    .map((r) => {
      const flaggedHere = isFlagged(r, floor);
      const syn = r.synthesis;
      const changed = syn?.headline ? truncate(syn.headline, 36) : "—";
      const flagCell = flaggedHere
        ? `🔴 ${escapeHtml(syn?.recommended_action ?? "")}`
        : "";
      // No "Implied move" column in v1 (band-only §1.4). Columns: Name | Band·Rank | PDUFA | Changed today | Flag
      return `<tr>
  <td style="padding:6px 10px;"><strong>${escapeHtml(nameLabel(r))}</strong><br><span style="color:#888;font-size:12px;">${escapeHtml(appnoLabel(r))}</span></td>
  <td style="padding:6px 10px;">${escapeHtml(bandBadge(r))}</td>
  <td style="padding:6px 10px;">${r.days_to_pdufa !== null && r.days_to_pdufa !== undefined ? escapeHtml(String(r.days_to_pdufa) + "d") : "—"}</td>
  <td style="padding:6px 10px;">${escapeHtml(changed)}</td>
  <td style="padding:6px 10px;">${flagCell}</td>
</tr>`;
    })
    .join("\n");

  const flaggedHeader =
    flagged.length === 0
      ? `<p style="color:#555;">Nothing crossed the attention threshold today.</p>`
      : `<h2 style="color:#8b0000;">🔴 FLAGGED (${flagged.length})</h2>\n${cards}`;

  const staleNote = isStaleMatview(sorted, today, staleDays)
    ? `<p style="color:#b8860b;font-size:12px;">Note: model scores are stale (matview last refreshed before the ${staleDays}-day freshness window).</p>`
    : "";

  return `<!DOCTYPE html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:680px;margin:0 auto;padding:24px;">
  <h1 style="margin-bottom:4px;">BC-FDA daily monitor — ${escapeHtml(today)}</h1>
  ${staleNote}
  <hr style="border:none;border-top:1px solid #ddd;margin:16px 0;">
  ${flaggedHeader}
  <hr style="border:none;border-top:1px solid #ddd;margin:16px 0;">
  <h2>WATCHLIST (${nWatch})</h2>
  <table style="width:100%;border-collapse:collapse;font-size:14px;">
    <thead><tr style="text-align:left;border-bottom:1px solid #ccc;">
      <th style="padding:6px 10px;">Name</th>
      <th style="padding:6px 10px;">Band · Rank</th>
      <th style="padding:6px 10px;">PDUFA</th>
      <th style="padding:6px 10px;">Changed today</th>
      <th style="padding:6px 10px;">Flag</th>
    </tr></thead>
    <tbody>
${tableRows}
    </tbody>
  </table>
  <p style="color:#888;font-size:12px;margin-top:24px;">Model band is a ranking input, not a calibrated probability.</p>
</body></html>`;
}

function renderCardHtml(r: DigestRow): string {
  const syn = r.synthesis as Synthesis; // flagged rows always have a synthesis
  const action = (syn.recommended_action ?? "").toUpperCase();
  const conf = Number(syn.confidence);
  const confStr = Number.isFinite(conf) ? conf.toFixed(2) : "—";
  const title = `${escapeHtml(nameLabel(r))} (${escapeHtml(appnoLabel(r))}) — ${escapeHtml(action)} · conf ${escapeHtml(confStr)}`;

  const headline = syn.headline ? `<p style="font-weight:600;margin:4px 0;">${escapeHtml(syn.headline)}</p>` : "";
  const band = `<p style="margin:4px 0;">Risk: <strong>${escapeHtml(bandBadge(r))}</strong></p>`;
  const whatChanged = syn.what_changed
    ? `<p style="margin:4px 0;"><em>What changed:</em> ${escapeHtml(syn.what_changed)}</p>`
    : "";

  const drivers = (syn.drivers ?? []).slice(0, 4);
  const driversHtml = drivers.length
    ? `<p style="margin:8px 0 2px;"><em>Drivers:</em></p><ul style="margin:0 0 8px;">${drivers
        .map(
          (d) =>
            `<li>${escapeHtml(d.stream ?? "")} · ${escapeHtml(d.direction ?? "")} · ${escapeHtml(
              d.magnitude ?? "",
            )} — ${escapeHtml(d.summary ?? "")}</li>`,
        )
        .join("")}</ul>`
    : "";

  const up = (syn.bullets_up ?? []).filter(Boolean);
  const down = (syn.bullets_down ?? []).filter(Boolean);
  const forAgainst =
    up.length || down.length
      ? `<table style="width:100%;"><tr style="vertical-align:top;">
  <td style="width:50%;"><strong>For</strong><ul style="margin:4px 0;">${up.map((b) => `<li>${escapeHtml(b)}</li>`).join("")}</ul></td>
  <td style="width:50%;"><strong>Against</strong><ul style="margin:4px 0;">${down.map((b) => `<li>${escapeHtml(b)}</li>`).join("")}</ul></td>
</tr></table>`
      : "";

  const risks = (syn.risks ?? []).filter(Boolean);
  const risksHtml = risks.length
    ? `<p style="margin:8px 0 2px;"><em>Risks:</em></p><ul style="margin:0 0 8px;">${risks
        .map((x) => `<li>${escapeHtml(x)}</li>`)
        .join("")}</ul>`
    : "";

  const watch = (syn.watch_items ?? []).filter(Boolean).slice(0, 2);
  const watchHtml = watch.length
    ? `<p style="margin:8px 0;color:#1a4a8b;"><strong>Watch:</strong> ${watch.map((w) => `▸ ${escapeHtml(w)}`).join(" ")}</p>`
    : "";

  const footnote = `<p style="color:#888;font-size:12px;margin-top:8px;">${escapeHtml(
    streamsFootnote(syn, r.days_to_pdufa),
  )}</p>`;

  return `<div style="border:1px solid #eee;border-radius:8px;padding:16px;margin:12px 0;">
  <h3 style="margin:0 0 8px;">${title}</h3>
  ${headline}
  ${band}
  ${whatChanged}
  ${driversHtml}
  ${forAgainst}
  ${risksHtml}
  ${watchHtml}
  ${footnote}
</div>`;
}

// ---------------------------------------------------------------------------
// Plain-text render (mirrors HTML; band-only v1 — NO implied-move column)
// ---------------------------------------------------------------------------
export function renderDigestText(rows: DigestRow[], today: string, opts?: RenderOptions): string {
  const floor = opts?.flagMinConfidence ?? DEFAULT_FLAG_MIN_CONFIDENCE;
  const staleDays = opts?.staleMatviewDays ?? DEFAULT_STALE_MATVIEW_DAYS;
  const sorted = sortRows(rows);
  const flagged = sorted.filter((r) => isFlagged(r, floor));
  const nWatch = sorted.length;
  const lines: string[] = [];

  lines.push(`BC-FDA daily monitor — ${today}`);
  if (isStaleMatview(sorted, today, staleDays)) {
    lines.push(`Note: model scores are stale (matview last refreshed before the ${staleDays}-day window).`);
  }
  lines.push("");
  lines.push("──────────────────────────────────────────────────────────────────────");
  if (flagged.length === 0) {
    lines.push("Nothing crossed the attention threshold today.");
  } else {
    lines.push(`🔴 FLAGGED (${flagged.length})`);
    lines.push("──────────────────────────────────────────────────────────────────────");
    for (const r of flagged) {
      lines.push(...renderCardText(r));
      lines.push("");
    }
  }
  lines.push("──────────────────────────────────────────────────────────────────────");
  lines.push(`WATCHLIST (${nWatch})        band · rank        PDUFA   changed today        flag`);
  lines.push("──────────────────────────────────────────────────────────────────────");
  for (const r of sorted) {
    const flaggedHere = isFlagged(r, floor);
    const syn = r.synthesis;
    const changed = syn?.headline ? truncate(syn.headline, 24) : "—";
    const flagCell = flaggedHere ? `🔴 ${syn?.recommended_action ?? ""}` : "";
    const days = r.days_to_pdufa !== null && r.days_to_pdufa !== undefined ? `${r.days_to_pdufa}d` : "—";
    lines.push(
      `${nameLabel(r).padEnd(6)} ${appnoLabel(r).padEnd(14)} ${bandBadge(r).padEnd(18)} ${days.padEnd(6)}  ${changed.padEnd(20)} ${flagCell}`,
    );
  }
  lines.push("──────────────────────────────────────────────────────────────────────");
  lines.push("Model band is a ranking input, not a calibrated probability.");
  return lines.join("\n");
}

function renderCardText(r: DigestRow): string[] {
  const syn = r.synthesis as Synthesis;
  const action = (syn.recommended_action ?? "").toUpperCase();
  const conf = Number(syn.confidence);
  const confStr = Number.isFinite(conf) ? conf.toFixed(2) : "—";
  const out: string[] = [];
  out.push(`${nameLabel(r)} (${appnoLabel(r)}) — ${action} · conf ${confStr}`);
  out.push(`  Risk: ${bandBadge(r)}`);
  if (syn.what_changed) out.push(`  What changed: ${syn.what_changed}`);
  const drivers = (syn.drivers ?? []).slice(0, 4);
  if (drivers.length) {
    out.push("  Drivers:");
    for (const d of drivers) {
      out.push(`    • ${d.stream ?? ""} · ${d.direction ?? ""} · ${d.magnitude ?? ""} — ${d.summary ?? ""}`);
    }
  }
  const up = (syn.bullets_up ?? []).filter(Boolean);
  if (up.length) out.push(`  For:    ${up.join("; ")}`);
  const down = (syn.bullets_down ?? []).filter(Boolean);
  if (down.length) out.push(`  Against: ${down.join("; ")}`);
  const risks = (syn.risks ?? []).filter(Boolean);
  if (risks.length) out.push(`  Risks:  ${risks.join("; ")}`);
  const watch = (syn.watch_items ?? []).filter(Boolean).slice(0, 2);
  if (watch.length) out.push(`  Watch:  ${watch.map((w) => `▸ ${w}`).join(" ")}`);
  out.push(`  ${streamsFootnote(syn, r.days_to_pdufa)}`);
  return out;
}
