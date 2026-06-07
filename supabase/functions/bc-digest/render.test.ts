// =============================================================================
// bc-digest / render.test.ts — Phase 3 §8.1 render + gating tests (Deno).
//
// Run:  deno test --no-check functions/bc-digest/render.test.ts
//
// FAKE data only — NO live Resend / DB / network. The load-bearing assertions:
//   - BAND-ONLY golden: the rendered HTML AND text contain NO implied-move column,
//     NO implied-move cell, and NOT the string "implied move unavailable" (the
//     inverse of the merged-away draft's golden — Pedro's band-only v1, §1.3).
//   - p_crl is NEVER rendered (no p_crl value in the body; the DigestRow type has
//     no p_crl field — structural §2.1).
// =============================================================================

import {
  assert,
  assertEquals,
  assertStringIncludes,
} from "https://deno.land/std@0.224.0/assert/mod.ts";
import {
  type DigestRow,
  isFlagged,
  renderDigestHtml,
  renderDigestText,
  renderSubject,
  type Synthesis,
} from "./render.ts";

const TODAY = "2026-06-03";
const OPTS = { flagMinConfidence: 0.6 };

// ---- fixtures -------------------------------------------------------------
function synthesis(overrides: Partial<Synthesis> = {}): Synthesis {
  return {
    headline:
      "Three insiders bought $2.1M open-market 41 days before PDUFA",
    what_changed:
      "Two directors and the CFO bought $2.1M open-market over 14 days and an 8-K manufacturing-buildout filing hit, 41 days before PDUFA.",
    risk_vs_market: {
      model_risk_band: "elevated",
      model_percentile: 78,
      // band-only v1: options dormant — these MUST NOT render in v1
      options_implied_move_pct: null,
      implied_move_horizon: "unavailable",
      stance: "indeterminate_no_options",
      gap_bps: null,
      rationale: null,
    },
    drivers: [
      {
        stream: "insider",
        direction: "bullish",
        magnitude: "notable",
        summary: "cluster: 2 directors + CFO, $2.1M/14d, no 10b5-1",
      },
      {
        stream: "news",
        direction: "bullish",
        magnitude: "minor",
        summary: "8-K manufacturing scale-up (primary tier)",
      },
    ],
    bullets_up: ["insider cluster buying into the PDUFA window", "manufacturing buildout"],
    bullets_down: ["model sits in the elevated band (78th pct)"],
    risks: ["manufacturing 8-K corroborated by SEC filing but launch outcome unproven"],
    watch_items: ["8-K cadence into PDUFA — a CRL-risk or financing 8-K would flip the read"],
    recommended_action: "investigate",
    confidence: 0.66,
    provenance: { streams_available: { insider: true, options: false, news: true } },
    ...overrides,
  };
}

function row(overrides: Partial<DigestRow> = {}): DigestRow {
  return {
    application_number: "761333",
    risk_band: "elevated",
    oof_percentile_rank: 78,
    appl_type: "BLA",
    pdufa_date: "2026-07-14",
    days_to_pdufa: 41,
    tier: "watchlist",
    materialized_at: TODAY + "T00:00:00Z",
    ticker: "PRTX",
    synthesis: synthesis(),
    trigger_reasons: ["insider_cluster", "news_8k"],
    fired_at: TODAY + "T14:05:00Z",
    ...overrides,
  };
}

// ===========================================================================
// Flag gate (§8.1)
// ===========================================================================
Deno.test("flag gate: investigate + conf 0.66 => flagged", () => {
  assert(isFlagged(row({ synthesis: synthesis({ recommended_action: "investigate", confidence: 0.66 }) }), 0.6));
});

Deno.test("flag gate: investigate + conf 0.55 => NOT flagged (watch-only)", () => {
  assertEquals(isFlagged(row({ synthesis: synthesis({ recommended_action: "investigate", confidence: 0.55 }) }), 0.6), false);
});

Deno.test("flag gate: monitor => NOT flagged", () => {
  assertEquals(isFlagged(row({ synthesis: synthesis({ recommended_action: "monitor", confidence: 0.9 }) }), 0.6), false);
});

Deno.test("flag gate: exit + conf 0.7 => flagged", () => {
  assert(isFlagged(row({ synthesis: synthesis({ recommended_action: "exit", confidence: 0.7 }) }), 0.6));
});

Deno.test("flag gate: no synthesis => NOT flagged", () => {
  assertEquals(isFlagged(row({ synthesis: null }), 0.6), false);
});

// ===========================================================================
// BAND-ONLY golden (load-bearing) — NO implied-move column / cell / string (§8.1)
// ===========================================================================
Deno.test("band-only: HTML has NO implied-move column and NO 'implied move unavailable' string", () => {
  const rows = [row()]; // options dormant (streams_available.options=false, implied_move null)
  const html = renderDigestHtml(rows, TODAY, OPTS);
  const lower = html.toLowerCase();

  // no implied-move column header, no per-cell unavailable string, no tier-note
  assert(!lower.includes("implied move"), "HTML must not render an implied-move column/header");
  assert(!lower.includes("implied-move"), "HTML must not render an implied-move column/header");
  assert(!lower.includes("implied move unavailable"), "HTML must not render the per-cell unavailable string (v1.1 only)");
  assert(!lower.includes("options unavailable on this tier"), "HTML must not render the global options tier-note (v1.1 only)");
  // the numeric implied-move pct value must never appear either
  assert(!lower.includes("±14%"), "HTML must not render an implied-move number");

  // the band badge stands alone as the risk read
  assertStringIncludes(html, "elevated");
  assertStringIncludes(html, "78th pct");
  // the streams footnote is the ONLY options mention
  assertStringIncludes(html, "options ✗");
});

Deno.test("band-only: TEXT has NO implied-move column and NO unavailable string", () => {
  const rows = [row()];
  const text = renderDigestText(rows, TODAY, OPTS).toLowerCase();
  assert(!text.includes("implied move"), "text must not render an implied-move column");
  assert(!text.includes("implied move unavailable"), "text must not render the per-cell unavailable string");
  assert(!text.includes("options unavailable on this tier"), "text must not render the tier-note");
  assertStringIncludes(text, "elevated · 78th pct");
  assertStringIncludes(text, "options ✗");
});

// the watchlist table header columns are exactly the band-only set (no Implied move)
Deno.test("band-only: watchlist table header has no 'Implied move' column", () => {
  const html = renderDigestHtml([row()], TODAY, OPTS);
  assert(!html.toLowerCase().includes(">implied move<"), "table header must not include an Implied move column");
  assertStringIncludes(html, ">Band · Rank<");
  assertStringIncludes(html, ">PDUFA<");
});

// ===========================================================================
// p_crl NEVER rendered (§8.1)
// ===========================================================================
Deno.test("p_crl never rendered: a p_crl value placed on the row is not in the body", () => {
  // Even if a caller mistakenly attaches p_crl (the type forbids it, but JS is loose),
  // the renderer reads only band/percentile, so the value never reaches the output.
  const sneaky = { ...row(), p_crl: 0.123456789 } as unknown as DigestRow;
  const html = renderDigestHtml([sneaky], TODAY, OPTS);
  const text = renderDigestText([sneaky], TODAY, OPTS);
  assert(!html.includes("0.123456789"), "p_crl value must not appear in HTML");
  assert(!text.includes("0.123456789"), "p_crl value must not appear in text");
  assert(!html.toLowerCase().includes("p_crl"), "the token p_crl must not appear in HTML");
  assert(!text.toLowerCase().includes("p_crl"), "the token p_crl must not appear in text");
});

// ===========================================================================
// Field map — the §1.6 worked example (§8.1)
// ===========================================================================
Deno.test("field map: flagged card renders band badge, what_changed, watch, drivers, streams", () => {
  const html = renderDigestHtml([row()], TODAY, OPTS);
  assertStringIncludes(html, "PRTX"); // ticker label
  assertStringIncludes(html, "INVESTIGATE"); // action chip
  assertStringIncludes(html, "conf 0.66");
  assertStringIncludes(html, "elevated"); // band
  assertStringIncludes(html, "78th pct"); // percentile
  assertStringIncludes(html, "What changed:");
  assertStringIncludes(html, "Watch:");
  assertStringIncludes(html, "8-K cadence into PDUFA"); // watch item
  assertStringIncludes(html, "insider"); // a driver stream
  assertStringIncludes(html, "streams: insider ✓ · options ✗ · news ✓");
});

Deno.test("field map: watchlist row carries band·rank, PDUFA days, changed-today, flag", () => {
  const html = renderDigestHtml([row()], TODAY, OPTS);
  assertStringIncludes(html, "41d"); // days_to_pdufa
  assertStringIncludes(html, "🔴 investigate"); // flag cell
  assertStringIncludes(html, "BLA-761333"); // appno label in the table
});

// ===========================================================================
// Empty digest (§8.1) — 0 flagged + send_when_empty handled by index; render still shows watchlist
// ===========================================================================
Deno.test("empty: 0 flagged renders 'nothing flagged' header + the watchlist", () => {
  const watchOnly = row({ synthesis: synthesis({ recommended_action: "monitor", confidence: 0.9 }) });
  const html = renderDigestHtml([watchOnly], TODAY, OPTS);
  assertStringIncludes(html, "Nothing crossed the attention threshold today.");
  assertStringIncludes(html, "WATCHLIST (1)");
});

Deno.test("empty: a quiet name (no synthesis fired today) shows '—' in changed-today", () => {
  const quiet = row({ synthesis: null });
  const html = renderDigestHtml([quiet], TODAY, OPTS);
  assertStringIncludes(html, "WATCHLIST (1)");
  // changed-today is em-dash for a quiet row
  assertStringIncludes(html, "—");
});

// ===========================================================================
// Subject line (§8.1, §1.5)
// ===========================================================================
Deno.test("subject: N flagged · M watched", () => {
  const rows = [row(), row({ application_number: "216789", ticker: "AXSM", synthesis: synthesis({ recommended_action: "monitor", confidence: 0.4 }) })];
  assertEquals(renderSubject(rows, TODAY, OPTS), "[BC-FDA] 2026-06-03 — 1 flagged · 2 watched");
});

Deno.test("subject: 0 flagged => 'nothing flagged'", () => {
  const rows = [row({ synthesis: synthesis({ recommended_action: "monitor", confidence: 0.4 }) })];
  assertEquals(renderSubject(rows, TODAY, OPTS), "[BC-FDA] 2026-06-03 — nothing flagged · 1 watched");
});

// ===========================================================================
// Sort: PDUFA proximity + today's delta, NOT the band (plan W3)
// ===========================================================================
Deno.test("sort: fired-today rows precede quiet rows; nearest PDUFA first within group", () => {
  const farFired = row({ application_number: "A", ticker: "AAA", days_to_pdufa: 90, oof_percentile_rank: 95 });
  const nearQuiet = row({ application_number: "B", ticker: "BBB", days_to_pdufa: 5, oof_percentile_rank: 10, synthesis: null });
  const nearFired = row({ application_number: "C", ticker: "CCC", days_to_pdufa: 10, oof_percentile_rank: 20 });
  const html = renderDigestHtml([farFired, nearQuiet, nearFired], TODAY, OPTS);
  // In the watchlist body, the two fired rows (CCC@10d before AAA@90d) come before the quiet BBB.
  const idxC = html.indexOf("CCC");
  const idxA = html.indexOf("AAA");
  const idxB = html.indexOf("BBB");
  assert(idxC < idxA, "nearer-PDUFA fired row (CCC@10d) sorts before farther fired row (AAA@90d)");
  assert(idxA < idxB, "fired rows sort before the quiet row (BBB) — band (95th) is NOT the sort key");
});
