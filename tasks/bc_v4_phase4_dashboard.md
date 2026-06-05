# BC-FDA Light v4 — Phase 4 detail plan: **dashboard drill-down** (LIST + DETAIL)

> Component owner doc. Scope = Phase 4 of `~/.claude/plans/plan-the-high-level-peppy-shell.md`:
> a read-only operator **drill-down** for the BC universe — a candidate **LIST** page and a per-name
> **DETAIL** page in the existing Next.js operator dashboard (`marazuela/conan-dashboard`).
>
> **This phase is the LOWEST priority and explicitly LAGS.** The product is the **daily digest email**
> (Phase 2/3). The dashboard adds nothing the digest doesn't already deliver — it is a convenience
> surface for an operator who wants to click from a digest line into the underlying streams. Ship it
> only after the pipeline is producing real `bc_*` rows; until then these pages render empty states.
> Nothing downstream depends on this phase. **Do not build this before Phases 0–3 are landing data.**
>
> **Hard constraints honored throughout:**
> - **Never display `p_crl`** (or any calibrated CRL probability). The risk view is a **band**
>   (`low|moderate|elevated|high`) + an optional cohort **percentile**. `p_crl` exists on the rows but
>   is forbidden in the UI (Light-v4 reframe, `v4_redesign_direction`). Loaders must **not even SELECT**
>   `p_crl`/`raw_p_uncalibrated`/`ci_low`/`ci_high` so the value can't leak into the client payload.
> - **No raw HTML.** Synthesis/news text renders via `react-markdown` (+`remark-gfm`) — already a dep,
>   already used safely at `app/(user)/decisions/[id]/page.tsx`. No `dangerouslySetInnerHTML`.
> - **Authenticated-only.** Reuse the existing anon-key + JWT server client; never a service-role client.
> - **Reuse, don't reinvent.** Clone the `/operator/flags` (list) and `/operator/runs/[id]` (detail)
>   patterns and the existing `components/ui/*` primitives. Net-new UI is one `RiskBandChip` only.
>
> Investigation basis (read-only, verified 2026-06-03 on Supabase `xvwvwbnxdsjpnealarkh`): live column
> shapes of all 5 source relations; RLS/grant audit of every relation; the dashboard's list pattern
> (`app/operator/flags/page.tsx` + `lib/api/operator/flags.ts`), detail pattern
> (`app/operator/runs/[id]/page.tsx` + `lib/api/operator/runs.ts`), nav (`lib/nav-config.ts`), server
> client (`lib/supabase/server.ts`), shell/counts wiring (`app/operator/layout.tsx`,
> `lib/api/operator/counts.ts`), the safe-markdown precedent, and the synthesis JSON contract
> (`tasks/bc_v4_phase2_synthesis_contract.md` §1).

---

## 0. Live-schema facts this plan is pinned to (verified 2026-06-03)

All 19 `bc_*` tables + the `bc_candidates` **matview** are deployed and **empty** (0 rows in every
relation, incl. `bc_applications`). Column shapes below are authoritative (pulled from
`information_schema.columns` / `pg_attribute`).

### 0.1 `bc_candidates` — MATVIEW (LIST primary source)
`relkind='m'`. Columns (ordinal):
`application_number` text · `last_scored_at` timestamptz · `p_crl` numeric **(FORBIDDEN in UI)** ·
`risk_band` text · `oof_percentile_rank` numeric · `refusal_reason` text · `sponsor_cik` text ·
`appl_type` text · `pdufa_date` date · `days_to_pdufa` int · `market_cap_usd` numeric ·
`avg_daily_volume_usd` numeric · `options_chain_exists` bool · `borrow_available` bool ·
`g1_active` bool · `g1_watchlist` bool · `g2_pass` bool · `g3_in_window` bool · `tier` text ·
`materialized_at` timestamptz.
- **No `sponsor_name` in the matview** — only `sponsor_cik`. The human-readable sponsor name lives on
  `bc_applications.sponsor_name` (nullable). LIST must LEFT-JOIN it in (see §3.2) or label by CIK.
- Unique index on `application_number` (`bc_candidates_appl_uidx`); index on `tier`.

### 0.2 `bc_rubric_scores` — TABLE (LIST/DETAIL band+percentile context)
`application_number` · `scored_at` · `scorer_name` (CHECK ∈ `{M14_adjusted, sNDA_pooled}`) ·
`scorer_version` · `p_crl` **(FORBIDDEN)** · `raw_p_uncalibrated` **(FORBIDDEN)** ·
`ci_low`/`ci_high` **(FORBIDDEN)** · `oof_percentile_rank` · `confidence_flag` · `risk_band`
(CHECK ∈ `{low, moderate, elevated, high}`) · `refusal_reason` · `features_id`.
- The matview already carries the *latest* `risk_band`/percentile, so the LIST does **not** need this
  table. DETAIL uses it only to show **scorer + confidence_flag + refusal_reason + scored_at**
  provenance (and to show score history if desired). **Never SELECT the probability columns.**

### 0.3 `bc_market_signals` — TABLE (DETAIL: the 3 streams)
`(application_number, signal_date date, signal_type text, payload jsonb, computed_at)`,
UNIQUE `(application_number, signal_date, signal_type)`.
- **No CHECK on `signal_type`** — values are convention (per Phase 2 doc §1.3): the 3 streams are
  `insider` (e.g. `insider_cluster_buy`), `options` (`options_iv`; payload keys
  `implied_move_pct_pdufa`/`implied_move_pct_30d`, `iv30_dod_pp`), and any news-derived signal.
  Render generically: group rows by `signal_type`, show `signal_date` + a compact `payload` view; do
  **not** hard-code a payload schema (it's loose and will evolve).

### 0.4 `bc_news_events` — TABLE (DETAIL: news)
`application_number` · `news_id` · `published_at` · `source` · `source_tier`
(CHECK ∈ `{primary, secondary, low}`) · `url?` · `raw_text?` · `verdict?`
(CHECK ∈ `{confirms_thesis, contradicts_thesis, neutral_update, requires_review}`) · `topic?` ·
`classifier_confidence?` · `classified_at?` · `ingested_at`.

### 0.5 `bc_thesis_updates` — TABLE (DETAIL: latest synthesis)
`application_number` · `update_date` date · `fired_at` timestamptz · `trigger_reasons` **text[]** ·
`synthesis` **jsonb** (the contract — Phase 2 §1) · `cost_usd?` · `prompt_version?`.
UNIQUE `(application_number, update_date)` → at most one synthesis per name per day. "Latest synthesis" =
`ORDER BY update_date DESC, fired_at DESC LIMIT 1`.
- `synthesis` shape (Phase 2 §1.1 `bc_synthesis_v1`), fields the DETAIL renders:
  `headline`, `what_changed`, `risk_vs_market{ model_risk_band, model_percentile,
  options_implied_move_pct, implied_move_horizon, stance, gap_bps, rationale }`,
  `drivers[]{ stream, direction, magnitude, evidence_ref{kind,id,metric?,value?}, summary }`,
  `bullets_up[]`, `bullets_down[]`, `risks[]`, `watch_items[]{label, why, evidence_ref?}`,
  `recommended_action` (∈ `no_change|monitor|investigate|exit`), `confidence` (0..1),
  `provenance{ market_signal_ids[], news_event_ids[], score_id?, input_window_days, streams_available{insider,options,news} }`.
  These are plain strings/enums/arrays → render with typed accessors; only `rationale`/`summary`/
  `what_changed`/bullets are free text and go through `react-markdown`.

### 0.6 Security audit — VERIFIED, the pages are safe to ship read-only
| relation | RLS | policy | effect on `anon` | effect on `authenticated` |
|---|---|---|---|---|
| `bc_candidates` (matview) | n/a (matviews ignore RLS) | n/a | `relacl` = `anon=awdDxtm` → **no `r`** ⇒ SELECT **revoked** (mig 006) | `authenticated=arwdDxtm` → has `r` ⇒ **SELECT OK** |
| `bc_rubric_scores` | enabled | `bc_rubric_scores_select` SELECT TO **authenticated** USING `true` | RLS blocks (policy excludes anon) | SELECT OK |
| `bc_market_signals` | enabled | `bc_market_signals_select` SELECT TO **authenticated** USING `true` | blocked | SELECT OK |
| `bc_news_events` | enabled | `bc_news_events_select` SELECT TO **authenticated** USING `true` | blocked | SELECT OK |
| `bc_thesis_updates` | enabled | `bc_thesis_updates_select` SELECT TO **authenticated** USING `true` | blocked | SELECT OK |

- **No grant/RLS change is required by this phase.** The matview is correctly readable by `authenticated`
  and revoked for `anon` (`pg_class.relacl` is authoritative; `information_schema.role_table_grants`
  under-reports matview grants — don't trust its empty result).
- Note (informational, NOT this phase's job): the 4 base tables still carry **table-level**
  INSERT/UPDATE/DELETE grants to `anon`/`authenticated`, but there are **no INSERT/UPDATE/DELETE RLS
  policies**, so writes are RLS-blocked for both roles. These pages are SELECT-only regardless. If a
  hardening sweep wants to drop those write grants, that's a separate DB task — do not couple it here.

### 0.7 Typegen gap — **must regenerate `types/database.ts` first**
The committed `dashboard/types/database.ts` (regenerated 2026-06-03 10:47) contains **zero** `bc_*`
types (`grep -c` = 0). The loaders below reference `Database['public']['Tables'|'Views']['bc_*']`. Two
options, pick **A**:
- **A (preferred):** run `pnpm typegen` (`dashboard/scripts/gen-types.sh`, project
  `xvwvwbnxdsjpnealarkh`, `--schema public --schema archive_v2`) to emit the `bc_*` types, commit the
  regenerated file. The matview `bc_candidates` is exposed to `authenticated`, so PostgREST/typegen will
  include it under `Database['public']['Views']` (matviews surface as Views in generated types) or
  `Tables` — confirm which after regen and adjust the loader's type alias accordingly.
- **B (fallback, only if typegen can't run in this env):** define narrow local row types in the loader
  files (hand-written `type BcCandidateRow = { … }`) and cast the PostgREST result. Less safe; flag as
  tech-debt to reconcile on next typegen. **Do not** invent columns — copy §0.1–0.5 exactly.

---

## 1. Routes & files (create / modify)

All paths under `dashboard/`. **Repo = `marazuela/conan-dashboard`** (separate from `marazuela/conan`;
see memory `dashboard_repo_context`). Branch off that repo's default; do not commit dashboard changes
into the workers repo.

### CREATE
| path | what |
|---|---|
| `app/operator/bc-candidates/page.tsx` | LIST page (RSC). §2. |
| `app/operator/bc-candidates/[appNumber]/page.tsx` | DETAIL page (RSC). §3. |
| `lib/api/operator/bc-candidates.ts` | loaders: `listBcCandidates`, `loadBcCandidate`, `loadBcStreams`, `loadBcNews`, `loadLatestBcSynthesis`. §4. |
| `components/operator/risk-band-chip.tsx` | the ONE net-new primitive — maps `risk_band` → color/tone. §5.1. |
| `components/operator/bc-synthesis-view.tsx` | typed renderer for the `bc_synthesis_v1` JSON (markdown-safe). §5.2. |
| `lib/bc-bands.ts` | `RISK_BANDS` const + `isRiskBand()` + `riskBandTone()` helpers (mirrors `lib/bands.ts`). §5.1. |

### MODIFY
| path | change |
|---|---|
| `lib/nav-config.ts` | add a nav item for `/operator/bc-candidates` under a new `BC universe` section (or `Monitor`). Add an optional `countKey: 'bcCandidatesTotal'` to the `NavItem` union. §6. |
| `lib/api/operator/counts.ts` | (optional, only if a sidebar badge is wanted) add a `bcCandidatesTotal` count from `bc_candidates`. §6. |
| `components/shell/sidebar-counts.ts` (`LiveCounts` type) | (optional) add `bcCandidatesTotal: number` if §6 badge is built. |
| `types/database.ts` | regenerate via `pnpm typegen` (§0.7). |

### NO new `loading.tsx` required
`app/operator/loading.tsx` is a **shared segment skeleton** that auto-covers any `/operator/*` route,
so `/operator/bc-candidates` and its `[appNumber]` child get a loading state for free. A tailored
`app/operator/bc-candidates/loading.tsx` is **optional polish** (nice-to-have, not in the critical path).
`notFound()` (Next built-in) handles the unknown-`appNumber` case on DETAIL; no custom `not-found.tsx`
needed (matches `/operator/runs/[id]`).

---

## 2. LIST page — `app/operator/bc-candidates/page.tsx`

**Pattern source:** `app/operator/flags/page.tsx` (RSC, `searchParams: Promise<…>`, filter chips as
`<Link>`s that rewrite the query string, `Pane`+`Pane.Header/Body/Footer`, `OperatorPageHeader`,
`EmptyState`, server-side pagination via `.range()`).

### 2.1 What it shows (one row per candidate, from `bc_candidates` ⨝ `bc_applications`)
Columns, left→right (mirrors the flags-row density; collapse low-priority cols at `md`):
1. **Risk-band dot + label** — `<RiskBandChip band={risk_band} />` (§5.1). Tone: `high`→danger,
   `elevated`→warning, `moderate`→info, `low`→muted. `null`/refused → muted "—" + `refusal_reason`
   tooltip.
2. **Name / sponsor** — `application_number` (mono) as the primary label; `sponsor_name` (or
   `sponsor_cik` fallback) muted secondary. (There is no drug/brand-name column in the BC schema; the
   application number is the identity.)
3. **Tier** — `tier` text as a small chip (mono, uppercase).
4. **Days-to-PDUFA** — `days_to_pdufa` (mono, right-aligned). Tone by proximity: `≤30`→warning,
   `≤7`→danger, negative ("past")→muted. Show `pdufa_date` on hover.
5. **Market-implied move** — **derived**, not a matview column. The matview has no implied-move; the
   number lives in `bc_market_signals(signal_type='options_iv').payload.implied_move_pct_pdufa`
   (else `_30d`). For the LIST, **avoid an N+1 per-row signal fetch**: either
   (a) **MVP**: render `options_chain_exists` as a boolean affordance ("options ✓/�—") and defer the
   actual implied-move number to DETAIL; or
   (b) **richer (optional)**: one batched query over `bc_market_signals` for the visible page's
   `application_number`s + `signal_type='options_iv'`, latest per name, joined in memory. Ship (a) first;
   (b) is a follow-up. **Decide (a) for the lagging MVP** — keeps the LIST a single round-trip.
6. **Last-synthesis action** — **derived** from `bc_thesis_updates`. Same N+1 concern. **MVP**: a
   batched query for the visible page's names → latest `update_date` + `synthesis->>'recommended_action'`,
   joined in memory (one extra round-trip per page, bounded by `PAGE_SIZE`). Render
   `recommended_action` as a small chip (`exit`→danger, `investigate`→warning, `monitor`→info,
   `no_change`→muted) + relative age of `update_date`. If no synthesis row: "—".
7. **Link** — whole row is a `<Link href={'/operator/bc-candidates/' + encodeURIComponent(application_number)}>`.

### 2.2 Filters (chips, URL-driven — clone flags' `hrefForFilter`/`toggle*`)
- **risk band**: multi-select `low|moderate|elevated|high` → `?band=elevated,high`.
- **tier**: multi-select over the distinct `tier` values present → `?tier=…`.
- **PDUFA window**: a few preset chips → `?pdufa=30|60|90|past` mapped to `days_to_pdufa` ranges.
- **tradeable**: a toggle `?tradeable=1` → `g2_pass=true AND options_chain_exists=true` (or
  `borrow_available`), surfacing only actionable names.
- **q**: free-text on `application_number`/`sponsor_name` via PostgREST `.or(ilike)` (escape `%_`, exactly
  as `flags.ts` does).
All filters compose into the loader filter object; default (no params) = all rows,
`ORDER BY days_to_pdufa ASC NULLS LAST` then `risk_band` severity.

### 2.3 Sorting
- Default: nearest PDUFA first (`days_to_pdufa ASC NULLS LAST`), tie-break risk-band desc.
- Optional `?sort=band|pdufa|tier|scored` via column-header `<Link>`s (clone the runs/flags ordering
  idiom). MVP can ship the default sort only + the band/tier/pdufa filters; clickable sort headers are
  polish.

### 2.4 Header / stats / empty
- `<OperatorPageHeader title="BC candidates" subtitle="FDA binary-catalyst universe — risk band vs market, drill-down. Mirrors the daily digest." stats={…} />`.
- Stats cluster (`InlineStat`): **Total**, and a count per risk band (high/elevated/moderate/low) from
  the loaded page or a cheap `head:true` count query (clone `loadOperatorFlagCounts`).
- **Empty state** (expected until pipeline lands data): `<EmptyState title="No BC candidates yet"
  description="The BC-FDA monitor has not materialized candidates. This view lags the daily digest." />`.
  This is the normal state today (matview empty) — the page must render cleanly empty, not error.
- Pagination: `PAGE_SIZE = 100`, `.range(offset, offset+PAGE_SIZE-1)`, prev/next links — verbatim from
  flags' `Pane.Footer`.

---

## 3. DETAIL page — `app/operator/bc-candidates/[appNumber]/page.tsx`

**Pattern source:** `app/operator/runs/[id]/page.tsx` (RSC, `params: Promise<{…}>`, `notFound()` on
missing, parallel `Promise.all` loads, breadcrumb nav, `Pane` panels, `InlineStat` grid header).

Param is `appNumber` (the `application_number`, e.g. `BLA-761333`). **Decode** with
`decodeURIComponent` (application numbers contain no slashes but may contain `-`; encode on the LIST
link side anyway).

### 3.1 Data loads (parallel)
```
const [candidate, scores, streams, news, synthesis] = await Promise.all([
  loadBcCandidate(appNumber),       // bc_candidates ⨝ bc_applications  → header
  loadBcRubricContext(appNumber),   // bc_rubric_scores latest (NO p_crl cols) → provenance
  loadBcStreams(appNumber),         // bc_market_signals, last N days, grouped by signal_type
  loadBcNews(appNumber),            // bc_news_events, recent, newest-first
  loadLatestBcSynthesis(appNumber), // bc_thesis_updates ORDER BY update_date DESC LIMIT 1
])
if (!candidate) notFound()
```

### 3.2 Panels (top → bottom)
1. **Breadcrumb**: `operator / bc-candidates / {appNumber}` (clone runs' `<nav>`).
2. **Header `Pane`** — identity + band:
   - `RiskBandChip` (band), `tier` chip, `application_number` (mono), `sponsor_name`/`sponsor_cik` muted.
   - `InlineStat` grid: **Risk band** (band label, NOT p_crl), **Percentile** (`oof_percentile_rank`,
     shown as "Nth pct" or "—"), **Days to PDUFA** (+`pdufa_date` hint), **Market cap**, **ADV**,
     **Last scored** (`last_scored_at`). **No probability anywhere.**
   - If `refusal_reason`/`confidence_flag` present (from rubric context), render a muted note line
     ("score refused: …" / "low_confidence") so an operator knows the band is soft.
3. **`risk_vs_market` `Pane`** (the moat object, from `synthesis.risk_vs_market`) — only if a synthesis
   exists. Render: `stance` as the headline chip (`market_underpricing_risk`→danger-ish accent,
   `market_overpricing_risk`→info, `aligned`→muted, `indeterminate_no_options`→muted), then `InlineStat`s:
   **Model band**, **Model percentile**, **Implied move** (`options_implied_move_pct` % @
   `implied_move_horizon`), **Gap (bps)** if non-null; then `rationale` as a markdown paragraph. This is
   the band-vs-market panel the whole product is built around — give it visual primacy.
4. **3 streams `Pane`** (`bc_market_signals`) — three labelled groups **insider / options / news**
   (group by the `signal_type` prefix/family; §0.3). Each group: a compact table of `signal_date` +
   key `payload` fields. Render `payload` generically (a small key→value list; for `options_iv` surface
   `implied_move_pct_*` + `iv30_dod_pp` if present). Empty group → muted "no insider signals (window)".
   Reuse a `Pane` + simple `<dl>`/table; no new heavy component.
5. **News `Pane`** (`bc_news_events`) — newest-first list: `published_at` · `source` (+`source_tier`
   chip) · `verdict` chip (`confirms_thesis`→success, `contradicts_thesis`→danger,
   `neutral_update`→muted, `requires_review`→warning) · `topic` · headline = `raw_text` first line
   (markdown-escaped) linked to `url` (open in new tab, `rel="noopener noreferrer"`). `classifier_confidence`
   as a muted suffix.
6. **Latest synthesis `Pane`** (`bc_thesis_updates`) — `<BcSynthesisView synthesis={…} />` (§5.2):
   `headline` (h-level), `what_changed` (markdown), `drivers[]` (one row each: stream chip + direction +
   magnitude + `summary`), `bullets_up`/`bullets_down`/`risks` (three short lists), `watch_items[]`,
   `recommended_action` chip + `confidence`. Footer: `update_date`, `prompt_version`, `cost_usd`,
   `trigger_reasons[]` as chips. **All free-text via `react-markdown`.** `provenance` ids are shown as a
   collapsed/muted footnote (they reference signal/news ids already on the page) — do not fabricate links.
   Empty → `<EmptyState compact title="No synthesis yet" />`.

### 3.3 Hard rules on DETAIL
- **`p_crl` / `raw_p_uncalibrated` / `ci_low` / `ci_high` are never SELECTed and never rendered.** The
  loader column lists in §4 deliberately omit them.
- All HTML originates from `react-markdown` component-mapped output (clone the `components={{…}}` map
  from `app/(user)/decisions/[id]/page.tsx`; map `a`→`<Link rel="noopener noreferrer" target="_blank">`,
  strip `img` or render as plain text, no `html` passthrough — `react-markdown` ignores raw HTML by
  default unless `rehype-raw` is added, which we must NOT add).

---

## 4. Loaders — `lib/api/operator/bc-candidates.ts`

**Pattern source:** `lib/api/operator/flags.ts` + `runs.ts` (server client via `createClient()`,
typed rows from `Database`, throw on `error`, return `{rows,total,…}`). Every function below is a thin
PostgREST query. **Column lists are explicit — never `select('*')` on score-bearing tables**, to
guarantee the forbidden probability columns never enter the payload.

```ts
import { createClient } from '@/lib/supabase/server'
import type { Database } from '@/types/database'

// Adjust the alias to whatever typegen emits for the matview (Views vs Tables) — §0.7.
export type BcCandidateRow = Database['public']['Views']['bc_candidates']['Row']

export type BcCandidatesFilter = {
  bands?: Array<'low'|'moderate'|'elevated'|'high'>
  tiers?: string[]
  pdufaWindow?: 30 | 60 | 90 | 'past' | null
  tradeableOnly?: boolean
  search?: string | null
  limit?: number   // default 100
  offset?: number
}

// LIST: single round-trip over the matview (+ batched synthesis/options enrich in the page, §2.1).
// Explicit column list — NOTE: p_crl is OMITTED on purpose.
export async function listBcCandidates(f: BcCandidatesFilter = {}) {
  const supabase = await createClient()
  const limit = f.limit ?? 100, offset = f.offset ?? 0
  let q = supabase
    .from('bc_candidates')
    .select(
      'application_number, risk_band, oof_percentile_rank, refusal_reason, sponsor_cik, ' +
      'appl_type, tier, pdufa_date, days_to_pdufa, market_cap_usd, avg_daily_volume_usd, ' +
      'options_chain_exists, borrow_available, g1_active, g1_watchlist, g2_pass, g3_in_window, ' +
      'last_scored_at, materialized_at',
      { count: 'exact' },
    )
  if (f.bands?.length)  q = q.in('risk_band', f.bands)
  if (f.tiers?.length)  q = q.in('tier', f.tiers)
  if (f.tradeableOnly)  q = q.eq('g2_pass', true).eq('options_chain_exists', true)
  if (f.pdufaWindow === 'past')      q = q.lt('days_to_pdufa', 0)
  else if (typeof f.pdufaWindow === 'number')
    q = q.gte('days_to_pdufa', 0).lte('days_to_pdufa', f.pdufaWindow)
  if (f.search) { const t = f.search.trim().replace(/[%_]/g, m => `\\${m}`)
    if (t) q = q.or(`application_number.ilike.%${t}%`) } // sponsor_name lives on bc_applications; see note
  q = q.order('days_to_pdufa', { ascending: true, nullsFirst: false })
       .order('risk_band', { ascending: false })
       .range(offset, offset + limit - 1)
  const { data, error, count } = await q
  if (error) throw new Error(`listBcCandidates: ${error.message}`)
  return { rows: (data ?? []) as BcCandidateRow[], total: count ?? 0 }
}
```
- **Sponsor name + free-text on sponsor**: the matview lacks `sponsor_name`. Two options:
  (a) a second batched query `bc_applications.select('application_number,sponsor_name').in('application_number', ids)`
  joined in memory (covers display **and** lets the page filter `q` on sponsor client-side); or
  (b) a thin DB **view** `v_bc_candidates_named` that LEFT JOINs `bc_applications` and is granted to
  `authenticated` — cleaner, lets `q` hit sponsor server-side and avoids the join in TS. **(b) is the
  recommended follow-up**; ship (a) for the lagging MVP to avoid a migration. (If you do (b), that view —
  not the matview — becomes the LIST source, and you add `GRANT SELECT … TO authenticated`.)
- **`loadBcCandidate(appNumber)`**: same explicit column list, `.eq('application_number', appNumber).maybeSingle()`;
  return `null` when absent (page calls `notFound()`).
- **`loadBcRubricContext(appNumber)`**: `bc_rubric_scores` latest row, columns
  `scored_at, scorer_name, scorer_version, oof_percentile_rank, confidence_flag, risk_band, refusal_reason`
  — **NO `p_crl`/`raw_p_uncalibrated`/`ci_*`**. `.order('scored_at', desc).limit(1).maybeSingle()`.
- **`loadBcStreams(appNumber, days=30)`**: `bc_market_signals.select('id, signal_date, signal_type, payload, computed_at')
  .eq('application_number', appNumber).gte('signal_date', sinceISODate).order('signal_date', desc)`.
  Group by `signal_type` in TS.
- **`loadBcNews(appNumber, limit=25)`**: `bc_news_events.select('id, news_id, published_at, source, source_tier, url, raw_text, verdict, topic, classifier_confidence, classified_at')
  .eq('application_number', appNumber).order('published_at', desc).limit(limit)`.
- **`loadLatestBcSynthesis(appNumber)`**: `bc_thesis_updates.select('id, update_date, fired_at, trigger_reasons, synthesis, cost_usd, prompt_version')
  .eq('application_number', appNumber).order('update_date', desc).order('fired_at', desc).limit(1).maybeSingle()`.
- **Batched LIST enrich** (§2.1 (b)/§2.1 item 6): `loadLatestSynthesisActions(appNumbers: string[])` →
  one query returning latest `update_date` + `synthesis->>recommended_action` per name; join in memory.
  (PostgREST can't do per-group LIMIT cleanly; fetch recent rows for the page's names ordered desc and
  reduce to first-per-name in TS — bounded by `PAGE_SIZE`.)

All loaders **throw** on `error` so the segment error boundary shows a real failure (consistent with
`flags.ts`/`runs.ts`); the *empty* (no rows) case is normal and handled by `EmptyState` in the page.

---

## 5. Net-new components (minimal)

### 5.1 `RiskBandChip` + `lib/bc-bands.ts`
The existing `BandChip` (`components/ui/band-chip.tsx`) and `StatusDot` band tones are bound to the **v3**
`Band` enum (`immediate|watchlist|archive|discard`) — a **different** taxonomy. Do **not** reuse it for
risk bands. Add:
- `lib/bc-bands.ts`:
  ```ts
  export const RISK_BANDS = ['low','moderate','elevated','high'] as const
  export type RiskBand = (typeof RISK_BANDS)[number]
  export function isRiskBand(v: string|null|undefined): v is RiskBand { return RISK_BANDS.includes(v as RiskBand) }
  // tone for StatusDot/InlineStat: high→danger, elevated→warning, moderate→info, low→muted
  export function riskBandTone(b: RiskBand|null): 'danger'|'warning'|'info'|'muted' { … }
  ```
- `components/operator/risk-band-chip.tsx`: a small chip mirroring `BandChip`'s structure (border +
  bg via `color-mix`) but colored by `riskBandTone` (reuse existing `--state-danger|warning|info` and
  `--text-muted` CSS vars; no new design tokens). `null`/refused → muted "—" with optional
  `refusal_reason` `title`.

### 5.2 `BcSynthesisView` — `components/operator/bc-synthesis-view.tsx`
A typed renderer for the `bc_synthesis_v1` JSON (do **not** reuse `components/ui/thesis-view.tsx` — that
is keyed to the v3 conviction/citation thesis shape, wrong contract). Responsibilities:
- Accept `synthesis: unknown`; validate/coerce with small `as*` accessors (clone the defensive
  `asNumber/asString/asArray` idiom from `thesis-view.tsx`) — never trust the JSON blindly even though
  it's schema-validated upstream.
- Render `headline` (heading), `what_changed` (markdown), `drivers[]`, `bullets_up/down/risks`,
  `watch_items[]`, `recommended_action` chip + `confidence`. Free-text fields go through a shared
  `<Markdown>` wrapper that reuses the exact `components={{…}}` map from
  `app/(user)/decisions/[id]/page.tsx` (extract it to `components/ui/markdown.tsx` if you want to share;
  otherwise inline). **No `rehype-raw`, no `dangerouslySetInnerHTML`.**
- Stream/direction/magnitude/verdict/stance → small chips via a local enum→tone map (no new primitive).

---

## 6. Nav registration + (optional) sidebar count

### 6.1 `lib/nav-config.ts` (required)
Add a section to `NAV_SECTIONS_OPERATOR`. Recommended: a dedicated top section so BC reads as its own
product line, e.g.:
```ts
{
  label: 'BC universe',
  items: [
    { href: '/operator/bc-candidates', label: 'BC candidates', icon: FlaskConical /* or Radar */,
      countKey: 'bcCandidatesTotal' /* optional, see §6.2 */ },
  ],
},
```
(If a new section feels heavy for a lagging surface, drop the single item into the existing `Monitor`
group instead — it's read-only.) Pick an already-imported `lucide-react` icon (`FlaskConical`, `Radar`,
`Activity` are already imported) to avoid touching the import block; if you want a distinct icon, add it
to the existing `lucide-react` import.

### 6.2 Sidebar badge (OPTIONAL — skip for MVP)
Only if a live count is wanted: extend the `NavItem.countKey` union in `lib/nav-config.ts` with
`'bcCandidatesTotal'`, add `bcCandidatesTotal: number` to `LiveCounts`
(`components/shell/sidebar-counts.ts`), and add one `head:true` count in `loadOperatorCounts`
(`lib/api/operator/counts.ts`):
```ts
supabase.from('bc_candidates').select('application_number', { count: 'exact', head: true })
```
Because the matview is empty today the badge would read 0 — **defer this** until the pipeline lands rows;
it's pure polish and adds a query to every operator page load. **MVP: register nav WITHOUT a countKey.**

---

## 7. Test plan

Lightweight, matching the dashboard's existing `vitest` (unit) + `playwright` (e2e) setup. Because the
tables are **empty**, tests focus on (a) empty-state correctness, (b) the forbidden-column invariant,
and (c) pure logic — not on rendering real rows.

### 7.1 Unit (`vitest`)
- **`lib/bc-bands.ts`**: `isRiskBand` accepts the 4 values + rejects `null`/junk; `riskBandTone` maps
  each band to the expected tone.
- **Loader column-list invariant (the security test):** assert the `.select(...)` strings in
  `lib/api/operator/bc-candidates.ts` do **not** contain `p_crl`, `raw_p_uncalibrated`, `ci_low`,
  `ci_high`. Implement either by importing the column-list constants (refactor the select strings into
  exported consts) and asserting, or a source-grep test (read the file, regex-assert absence). This is
  the load-bearing guard that keeps the CRL probability out of the UI — **make it a real test**.
- **`BcSynthesisView` accessors**: feed a malformed `synthesis` (missing keys, wrong types) → component
  returns an empty/compact state, never throws; feed the §1.4 worked example from the Phase 2 doc →
  renders `headline`, all `drivers`, the `risk_vs_market` stance, and `recommended_action` without
  emitting `p_crl`.
- **Filter→query mapping**: `pdufaWindow='past'` → `lt('days_to_pdufa',0)`; `tradeableOnly` →
  `g2_pass=true & options_chain_exists=true`; `bands` → `.in('risk_band', …)` (assert via a mocked
  supabase builder, the way existing loader tests mock PostgREST).

### 7.2 Integration / e2e (`playwright`)
- **Empty-state render:** with the live (empty) DB, `/operator/bc-candidates` returns 200 and shows the
  "No BC candidates yet" empty state, no console errors, nav item present and highlighted. (Auth: reuse
  the e2e auth setup the existing operator tests use; if none, this is a manual smoke step.)
- **Unknown name → 404:** `/operator/bc-candidates/DOES-NOT-EXIST` calls `notFound()` (Next 404 page).
- **No-`p_crl` DOM assertion:** once a seed/fixture row exists (or via a temporary seeded row in a
  branch), assert the rendered HTML of both pages contains the band label but **no** numeric probability
  / no `p_crl` token. Until then, cover this at the unit level (7.1 column-list test).
- **Markdown safety:** seed a `bc_thesis_updates.synthesis` whose `what_changed` contains a raw
  `<script>`/`<img onerror>` string → assert it renders as inert text (react-markdown drops raw HTML),
  no script node in the DOM.

### 7.3 Manual smoke (pre-merge, since data is empty)
- `pnpm typegen` succeeds and `bc_*` types appear in `types/database.ts`.
- `pnpm lint && pnpm typecheck && pnpm test` green.
- `pnpm build` compiles both new routes.
- Local `pnpm dev`: log in, visit LIST (empty state), visit a hand-constructed DETAIL URL for a name you
  temporarily insert via MCP on a throwaway basis (then delete) to eyeball all 6 panels — **optional**,
  since the phase legitimately ships empty.

---

## 8. Risks & gotchas

1. **Lowest priority / lagging — sequencing risk.** This phase has zero downstream dependents and the
   matview is empty. **Do not start it before Phases 0–3 are landing real `bc_*` rows**, or you'll be
   QA-ing against empty tables and guessing at payload shapes. The digest (Phase 2/3) is the product;
   this is convenience drill-down. Build last.
2. **`p_crl` leakage (highest-severity functional risk).** The forbidden columns physically exist on
   `bc_candidates` and `bc_rubric_scores`. The only defense is the **explicit `.select()` column lists**
   (§4) + the **unit invariant test** (§7.1). A lazy `select('*')` would ship the CRL probability to the
   browser. Enforce the column-list test in CI.
3. **Typegen gap (§0.7).** `bc_*` types are absent from the committed `types/database.ts`; the loaders
   won't typecheck until `pnpm typegen` is re-run and committed. First task of the phase. Confirm whether
   the matview lands under `Views` or `Tables` in the generated file and fix the `BcCandidateRow` alias.
4. **Matview vs base-table grant confusion.** `information_schema.role_table_grants` **under-reports**
   matview grants (returned `[]`). `pg_class.relacl` is authoritative and confirms
   `authenticated` has `r` and `anon` does not. No grant migration is needed — don't be misled into
   "fixing" a non-existent gap (cf. memory `supabase_migrations_drift`: verify live state, MCP-authoritative).
5. **No `sponsor_name`/drug-name on the matview.** Identity is `application_number`; `sponsor_name` needs
   a join to `bc_applications` (§4). Decide MVP = in-memory batched join (no migration) vs follow-up DB
   view `v_bc_candidates_named` (cleaner server-side `q`, needs a grant). Don't block the page on the view.
6. **Loose `signal_type` + `payload` schema.** No CHECK on `signal_type`; `payload` is free JSONB that
   will evolve. Render streams **generically** (group by type, key→value payload view) — do **not**
   hard-code payload keys beyond a best-effort surfacing of `implied_move_pct_*`/`iv30_dod_pp`. Brittle
   payload assumptions will break when fetchers change.
7. **N+1 on the LIST.** Implied-move and last-synthesis-action are not matview columns. Naive per-row
   fetches = N+1. MVP: defer implied-move to DETAIL (show `options_chain_exists` boolean) and batch the
   synthesis-action enrich in **one** query per page (§2.1). Don't fan out per row.
8. **`react-markdown` raw-HTML safety.** Safe **only** because we do NOT add `rehype-raw` and never use
   `dangerouslySetInnerHTML`. Anyone "improving" markdown rendering by adding `rehype-raw` reopens XSS on
   LLM-authored `synthesis` text. Lock this in a comment + the §7.2 markdown-safety test.
9. **Two-repo discipline.** Dashboard changes go to `marazuela/conan-dashboard`, not the workers repo
   (`dashboard_repo_context`). Branch off that repo's default; don't co-mingle with `bc_*` worker PRs.
10. **Wrong-chip reuse.** `BandChip`/`ThesisView`/`StatusDot` band tones are the v3 taxonomy
    (`immediate|watchlist|…` / conviction-citations). Reusing them for risk bands or the synthesis
    contract will mislabel data. Use the net-new `RiskBandChip` + `BcSynthesisView` (§5).
11. **Empty-state is the default, not an error.** Every panel and the LIST must render cleanly with zero
    rows (that's today's live state). Loaders throw only on a real PostgREST `error`; "no rows" → EmptyState.
12. **PostgREST per-group LIMIT.** "Latest synthesis per name" and "latest options signal per name" can't
    be expressed as a single grouped-LIMIT PostgREST call; fetch recent rows for the page's names and
    reduce to first-per-name in TS (bounded by `PAGE_SIZE`). Acceptable; documented in §4.

---

## 9. Execution order (within this lagging phase)
1. `pnpm typegen` → commit `types/database.ts` with `bc_*` types (§0.7). Resolve the matview type alias.
2. `lib/bc-bands.ts` + `components/operator/risk-band-chip.tsx` (+ unit test). (§5.1, §7.1)
3. `lib/api/operator/bc-candidates.ts` — all loaders with **explicit column lists** (+ column-list
   invariant test). (§4, §7.1)
4. LIST page `app/operator/bc-candidates/page.tsx` (clone flags). (§2)
5. `components/operator/bc-synthesis-view.tsx` (+ accessor tests) and the shared `<Markdown>` wrapper. (§5.2)
6. DETAIL page `app/operator/bc-candidates/[appNumber]/page.tsx` (clone runs/[id]). (§3)
7. Nav registration in `lib/nav-config.ts` (no countKey for MVP). (§6.1)
8. Tests green (`lint`/`typecheck`/`vitest`/`build`), playwright empty-state + 404 + markdown-safety. (§7)
9. (Optional, defer) sidebar `bcCandidatesTotal` badge; `v_bc_candidates_named` view for server-side
   sponsor filtering. (§6.2, §4)

**Definition of done:** both routes build and render the correct **empty** states against the live
(empty) DB; nav item present; loaders provably omit `p_crl`/probability columns (CI test); synthesis/news
free-text renders markdown-safe with no raw-HTML path; zero changes to the workers repo; no DB migration
required (matview already correctly granted). Real-data QA is a **follow-up** once Phases 0–3 populate
`bc_*`.
