# BC-FDA Light v4 — Phase 3 detail plan: **daily digest** (edge-fn) + **outcome logging** (Modal)

> **CANONICAL Phase-3 plan.** This file merges two earlier overlapping Phase-3 drafts
> (`bc_v4_phase3_digest.md` — edge-fn digest; `bc_v4_phase3_digest_outcomes.md` — Modal-worker digest) into
> one execution-ready doc. The `_outcomes` draft has been deleted. See **`## Reconciliation notes`** (end of
> file) for every place the two drafts disagreed and which way this doc resolved it — the load-bearing one is
> the **digest transport** (edge-fn + pg_cron, §3, vs the draft's Modal worker).
>
> **v1 SHIP DECISION (Pedro, 2026-06-03): SHIP BAND-ONLY.** The v1 digest shows **risk-band + percentile +
> what-changed + the 1–2 worth a look** — and **NO market-implied-move column**. The synthesis contract
> (Phase 2) still *carries* `risk_vs_market.options_implied_move_pct` (it ships dormant/`null` — Polygon
> options is entitlement-gated, §1.3), but the v1 digest **does not render an implied-move column at all**;
> it renders the band alone. The implied-move column is a **v1.1 addition once Polygon options lands** — at
> which point the digest reads the already-present `risk_vs_market` fields with near-zero render change
> (§1.3, §10/RN). **Outcome LOGGING only — no refit loop, no drift alarm** (the endorsed direction cut L7 to
> logging).
>
> Component owner doc. Scope = Phase 3 of `~/.claude/plans/plan-the-high-level-peppy-shell.md`:
> **(1) the daily email digest — THE product surface** (renders, for the ~20 in-window tradeable
> NDA/BLA names, the model **risk-band/percentile + what-changed today + the 1–2 watch items worth a look**;
> **band-only in v1**, implied-move column deferred to v1.1 per the decision above), and **(2) outcome
> logging only** (write `bc_prediction_outcomes` when a PDUFA resolves; no refit loop). **No drift alarms, no
> gated refit** — the endorsed direction cut L7 to outcome logging.
>
> **Hard project constraints honored throughout:** **digest-first** (usable without the dashboard, which
> may lag); **zero Cowork** anywhere on this path; **zero LLM** (the digest is a *pure deterministic
> renderer* of the `synthesis` JSON Phase 2 already wrote+validated — it never calls Anthropic and scores
> nothing); **fail-loud** (every run writes a `bc_pipeline_runs` row, **send-or-throw**, even on crash);
> **idempotent** (a recipient is never emailed twice for the same digest day; an outcome is never
> double-labeled — both enforced by a DB UNIQUE + a re-check); **strangle, don't entangle** (the digest
> calls Resend **directly** — it does **NOT** bolt a 5th entry point onto the v4 `fanout` edge function and
> does **NOT** add a DB trigger on `bc_thesis_updates`; it is fired by its own pg_cron tick — §3);
> **email-gating honored** (memory `email_alert_gating`: a "flagged" email fires only after AI review +
> promotion — here the Phase-2 synthesis IS the AI review and `recommended_action ∈ {investigate,exit}` is
> the promotion event; the digest never fires off a raw `bc_market_signals` INSERT).
>
> **Investigation basis (read-only, 2026-06-03, verified live on `xvwvwbnxdsjpnealarkh`):** the deployed
> `supabase/functions/fanout/index.ts` (the Resend call shape `:493/:716/:959`, secret `RESEND_API_KEY`
> `:103`, from-addr `:104`, recipient pool `notifications_prefs.email_on_immediate` → `auth.admin.listUsers`
> `:281–:299`, 23505 dedup idiom `:941`); the deployed standalone `supabase/functions/scanner-health/index.ts`
> (the non-webhook edge-fn pattern: service-role client, `x-service-key` bypass for Modal); the
> `bc_candidates` matview **definition** + columns; `bc_prediction_outcomes / bc_thesis_updates /
> bc_rubric_scores / bc_market_signals / bc_news_events / bc_pipeline_runs / bc_applications /
> bc_company_tradeable / bc_config` schemas + **all CHECK/UNIQUE constraints** (via `pg_constraint`); the
> live pg_cron+net.http_post+vault idiom (`supabase/migrations/20260605000050_earnings_calendar_pg_cron.sql`,
> `..._ic_memo_backlog_cron_schedule.sql`); `modal_workers/ingestion/openfda_ingest.py`
> (`ingest_drugsfda_approvals` / `extract_submission_rows`); `modal_workers/app.py` (the conan-v2 cron
> registry — 4 crons live). **Migrations are authoritative over the spec's printed §7 SQL** (per
> `supabase_migrations_drift` / `rubric_v2_seed`). Sibling plans read (do not redo):
> `bc_v4_phase2_synthesis_contract.md` (writes the `synthesis` JSON the digest renders — note its
> `risk_vs_market` object), `bc_v4_a0_cohort_confidence.md` (§6 specs `openfda_crl_transparency.py`, the CRL
> outcome source the labeler reuses).
>
> **Merge provenance (the now-deleted `tasks/bc_v4_phase3_digest_outcomes.md` draft):** that draft was
> ~95% correct and its outcome-labeler section is absorbed here almost verbatim. The one architecture choice
> it made differently — it built the *digest* as a **Modal worker** (with a new Modal `RESEND_API_KEY` secret
> + a `bc_digest_recipients()` SECURITY DEFINER RPC + a `dispatch_release_times` hour-15 edit) — is **not**
> the path this canonical doc takes. Live verification shows `pg_cron`+`pg_net`+`http` are all enabled and
> `RESEND_API_KEY`/`auth.admin.listUsers` already live in the **edge runtime**, so an **edge-function digest
> fired by pg_cron** is strictly cleaner (reuses the existing Resend secret + recipient resolver, needs **no**
> new Modal secret, needs **no** `bc_digest_recipients()` RPC, and **sidesteps the Modal 5-cron cap entirely**
> — the draft itself flagged that cron-hour wiring as its #5 hand-off risk). The **outcome-labeler stays a
> Modal worker** (it needs Polygon + openFDA Python). §3.5 records the head-to-head so the choice is
> auditable; if Pedro prefers the all-Modal symmetry, the draft's Modal-digest path is the documented
> fallback (preserved in `## Reconciliation notes`).

---

## 0. Live-schema facts this plan is pinned to (verified 2026-06-03)

These are the **deployed** shapes; do not infer from the spec's stale §7 text. The ⚠️ rows **correct the
Phase-2 sibling docs** and are load-bearing for this plan's code.

### 0.1 `bc_pipeline_runs` — **`status` CHECK = `{running, succeeded, failed, partial}`** ⚠️
Verified `pg_constraint`: `CHECK (status = ANY (ARRAY['running','succeeded','failed','partial']))`.
**The Phase-2 synthesis/fetcher docs are STALE here** — they assert "no CHECK on `status`" and use tokens
`ok` / `error` / `killed_budget`. **Those would 23514.** This plan (and every BC worker) uses the allowed
set and folds nuance into `log`/`reason`:
| intent | allowed `status` | detail preserved in |
|---|---|---|
| in progress | `running` | (set at open, replaced at close) |
| completed, all good | `succeeded` | — |
| completed, ≥1 name/app failed | `partial` | `n_failed > 0`; per-item detail in `log` jsonb |
| threw before completing | `failed` | `reason` = exception summary (stamped in `finally`) |
- A "digest sent 0 emails because nothing flagged" is **not** a distinct status — it is `succeeded` with
  `log = {"emailed": N, "n_flagged": 0}`. Columns (verified, all nullable except noted): `id` (uuid, PK),
  `pipeline_name` (text, NOT NULL), `started_at` (NOT NULL, dflt `now()`), `finished_at`, `status`,
  `snapshot_date` (date), `n_processed` (int), `n_failed` (int), `cost_usd` (numeric), `log` (jsonb),
  `reason` (text). **Hand-off (§10):** Phase-2 docs must reconcile to this CHECK before they build.

### 0.2 `bc_prediction_outcomes` (the outcome-log destination — verified)
| column | type | null | note |
|---|---|---|---|
| `id` | uuid | NO | `gen_random_uuid()` |
| `application_number` | text | NO | FK → `bc_applications(application_number)` |
| `horizon_days` | int | NO | the price-return horizon (1/7/30); **part of the UNIQUE key** |
| `regulatory_outcome` | text | **YES** | **CHECK ∈ {`approved`,`crl`,`withdrawn`,`extended`}** (verified — note `crl` lowercase, NOT `CRL`) |
| `price_return_pct` | numeric | YES | t+N return vs the pre-PDUFA close |
| `hypothesis_outcome` | text | YES | free text (no CHECK) — did the band's read match reality (§5.4) |
| `scored_p_crl` | numeric | YES | the `p_crl` shown at prediction time (the paired probability — storage only) |
| `labeled_at` | timestamptz | NO | `now()` |
- **UNIQUE `(application_number, horizon_days)`** (verified) → at most one row per app per horizon ⇒ the
  labeler writes **three rows per resolved app** (h=1,7,30), each idempotent. Upsert
  `on_conflict=application_number,horizon_days`, `prefer=resolution=merge-duplicates,return=minimal`.
- **`regulatory_outcome` is NULLABLE** (load-bearing): the regulatory verdict and the price returns resolve
  on **different clocks** (verdict known at PDUFA; t+30 return needs ~30 trading days). There is **no CHECK
  forcing it non-null**, so a verdict-only or price-only row is legal — the labeler writes the verdict the
  day it's known, then **merges** each price horizon as it matures (§5.3). **There is NO column for the
  predicted band/action** — only `scored_p_crl` pairs the prediction; the band is recoverable from the
  pre-PDUFA `bc_rubric_scores` row for `hypothesis_outcome`.

### 0.3 `bc_candidates` matview (the universe/band/rank read surface — the digest's primary source)
`relkind='m'`. **Read its committed definition (verified) — the digest reads it instead of re-joining base
tables.** Its WITH-pipeline already joins `latest_features` + `latest_score` (M14 for NDA/BLA, sNDA_pooled
for sNDA/sBLA) + `latest_tradeable` (by `sponsor_cik`) and applies the gates from `bc_config`
(`l3.window_days=120`, `l3.min_market_cap=2.5e8`, `l3.min_adv=2e6`, `l3.tau_*`). **Exposed columns
(verified):**
`application_number, last_scored_at, p_crl, risk_band, oof_percentile_rank, refusal_reason, sponsor_cik,
appl_type, pdufa_date, days_to_pdufa, market_cap_usd, avg_daily_volume_usd, options_chain_exists,
borrow_available, g1_active, g1_watchlist, g2_pass, g3_in_window, tier, materialized_at`.
- **`tier` ∈ `{refused, gate1_failed, gate2_failed, active, watchlist}`** (verified in the CASE). The
  **digest row set = `g3_in_window = true AND tier IN ('active','watchlist')`** (≈20 names by design).
- **The digest renders `risk_band` + `oof_percentile_rank`, NEVER `p_crl`** (the v4 reframe; A0). `p_crl`
  is selected only to pair into `bc_prediction_outcomes.scored_p_crl` at outcome time — never shown. Render
  with an **explicit column select that omits `p_crl` from the render struct** (a structural guarantee, not
  a discipline note — see §2.1).
- ⚠️ **The matview does NOT expose `ticker`** (it carries `sponsor_cik`). The digest gets the symbol by the
  `sponsor_cik → latest bc_company_tradeable.ticker` hop (§0.4). `options_chain_exists` here is the Phase-0
  *tradeability* boolean (does Polygon list any chain) — it is **NOT** the daily "did the options fetcher
  emit an `options_iv` row" signal; that lives in the synthesis's `provenance.streams_available.options`.
  In v1.1 (when the implied-move column exists) the digest trusts the **synthesis** for that cell, not this
  boolean (§1.3); **v1 renders no implied-move cell at all** (band-only).
- **No UNIQUE index on the matview** was found in this draft's index introspection (only the base-table
  indexes). ⇒ if that holds, Phase 1's refresh is plain `REFRESH MATERIALIZED VIEW public.bc_candidates`
  (**not** `CONCURRENTLY`, which requires a unique index). ⚠️ **The merged-away draft asserted the opposite**
  — a UNIQUE index `bc_candidates_appl_uidx` exists ⇒ `REFRESH … CONCURRENTLY` is possible. The two drafts'
  live reads disagreed; this is flagged in `## Reconciliation notes` for Phase 1 to settle by re-introspecting
  `pg_index` on `bc_candidates`. **The digest is read-only on the matview regardless**, so this does not block
  Phase 3 — it is a Phase-1 refresh-mode hand-off (§10).  This is only a hand-off
  note to Phase 1 (§10) so it doesn't assume a concurrent refresh.

### 0.4 `bc_applications` + `bc_company_tradeable` (identity + ticker — verified)
- `bc_applications`: `application_number (PK), sponsor_cik (NOT NULL), sponsor_name, appl_type
  (CHECK ∈ {NDA,BLA,sNDA,sBLA}), created_at`. **No `status`, no `pdufa_date` column.** ⇒ **"pending" is
  implicit**: an app is pending when its in-window `bc_application_features.pdufa_date` (surfaced as the
  matview's `pdufa_date`/`days_to_pdufa`) has no resolved `bc_prediction_outcomes` row yet. The
  lifecycle-hygiene sweep (§5.6) keys on `pdufa_date < today AND no outcome row`, not on a status flip.
- `bc_company_tradeable`: `id, sponsor_cik (NOT NULL), ticker (nullable), snapshot_date (NOT NULL),
  market_cap_usd, avg_daily_volume_usd, options_chain_exists, borrow_available, borrow_cost_bps,
  data_source, fetched_at`. **`ticker` is the only place the symbol lives** — needed for the digest header
  label and the labeler's Polygon price lookup. Latest row per CIK = `ORDER BY snapshot_date DESC LIMIT 1`.

### 0.5 `bc_thesis_updates` (the synthesis source the digest renders — verified)
| column | type | null | note |
|---|---|---|---|
| `application_number` | text | NO | FK → `bc_applications` |
| `update_date` | date | NO | the digest day |
| `fired_at` | timestamptz | NO | `now()` |
| `trigger_reasons` | text[] | NO | the deterministic reasons that fired (Phase 2 §2.5) |
| `synthesis` | jsonb | NO | **the contract the digest renders — Phase 2 §1.1** |
| `cost_usd` | numeric | YES | — |
| `prompt_version` | text | YES | e.g. `bc_synthesis_v1` |
- **UNIQUE `(application_number, update_date)`** → at most one synthesis per name per day. The digest is a
  **pure consumer** (SELECT-only). Its card fields ARE the synthesis contract fields (`headline,
  what_changed, risk_vs_market{model_risk_band,model_percentile,options_implied_move_pct,
  implied_move_horizon,stance,gap_bps,rationale}, drivers[], bullets_up[], bullets_down[], risks[],
  watch_items[], recommended_action, confidence, provenance{...,streams_available{insider,options,news}}`)
  — see §1.2's field→layout map.

### 0.6 `bc_config` (live, verified) — keys the digest/labeler read; none mutated destructively
Present (11 rows): `l3.delta_novel, l3.min_adv=2e6, l3.min_market_cap=2.5e8, l3.tau_nda=0.30,
l3.tau_nda_watchlist=0.50, l3.tau_snda=0.30, l3.tau_snda_watchlist=0.50, l3.window_days=120,
l4.daily_budget_usd=5, l4.max_events_per_candidate_day=40, l7.refit_min_crl_events=30`. **No `l4.digest_*`
/ outcome keys exist** → §6.4 seeds the small set via one disk-first migration. Values are jsonb scalars;
read via `value #>> '{}'` server-side (the matview does it) or the shared `bc_monitor/config.py` `get_*`
cached helper Modal-side (Phase 2 §2.6) — missing key ⇒ documented default + warn, never a silent 0.

### 0.7 The Resend path (`fanout`) — what we reuse vs. what we deliberately do NOT (§3)
`supabase/functions/fanout/index.ts` is the deployed Resend sender. Verified facts that drive §3:
- Sends via a **direct inlined `fetch("https://api.resend.com/emails", {Authorization: 'Bearer '+RESEND_API_KEY})`**
  (`:493/:716/:959`) — there is **no shared sender library**; reusing "the path" inherits only the trigger
  plumbing, which is exactly what we must NOT inherit.
- **Secret = `RESEND_API_KEY`** (env, `:103`); **from-addr = `RESEND_FROM_ADDRESS`** default
  `Conan Alerts <alerts@alerts.solutz.com>` (`:104`); **recipients = `notifications_prefs.email_on_immediate=true`
  → `auth.admin.listUsers` two-hop** (`resolveRecipients`, `:281–:303`), with a `FAN_OUT_DEV_RECIPIENTS`
  env fallback (`:106`). **These are edge-runtime secrets** (already provisioned for `fanout`) — the digest
  **edge function reuses them as-is** (the all-Modal draft needed to re-mint `RESEND_API_KEY` as a Modal
  secret; the edge-fn approach does not — §3.4).
- **All four `fanout` entry points are webhook-triggered** (`alerts`/`candidate_events`/
  `convergence_assessments` INSERT/UPDATE — `:96`). Dedup is `alert_deliveries`, whose FKs are
  `alert_id/candidate_event_id/candidate_id/assessment_id` and whose dedup unique index is
  `(assessment_id, channel, target) WHERE assessment_id IS NOT NULL` — **no `application_number` /
  `bc_thesis_update_id` column, no BC-compatible parent or dedup key.** Bolting BC on would need a schema
  migration on a shared v2/v3 table **+** a 5th entry point **+** a webhook on `bc_thesis_updates` —
  precisely the convergence-trigger entanglement the strangle plan forbids. This is the decisive evidence
  for §3's standalone-digest choice.

### 0.8 Price-return + regulatory-outcome sources (verified — labeler reuse)
- **Approval outcome — Drugs@FDA:** `modal_workers/ingestion/openfda_ingest.py::ingest_drugsfda_approvals(
  since, until, application_search=…)` (`:119`) queries `drug/drugsfda.json` filtered on
  `submissions.submission_status_date:[since TO until]`; the pure `extract_submission_rows(app)` (`:288`)
  projects `{submission_type, submission_status, submission_status_date, submission_class_code,
  review_priority, …}` per submission. An **ORIG submission with `submission_status='AP'`** on the app
  number, dated ≥ `pdufa_date`, = `approved`; a `WD` status = `withdrawn`. The labeler reuses the **read
  path** (`_openfda_get` `:79` + the parser) — it does **not** need the full ingest's `documents`/table
  writes. Auth via `modal_workers/shared/openfda_client.py` (`openfda_auth_params`, `openfda_url`).
- **CRL outcome — CRL Transparency dump:** the A0 fetcher
  `modal_workers/fetchers/universe/openfda_crl_transparency.py` (`api.fda.gov/download.json` →
  `results.transparency.crl`; 426 CRLs, 100% FDA-keyed, A0 §1.1). ⚠️ **NOT BUILT YET** (A0 §6 deliverable,
  verified absent on disk). The labeler's `crl` path depends on it; the **approvals path works
  independently**. Match a CRL record to a universe app by **digit-normalized `application_number`** (both
  `["NDA 215344"]`-style list and bare digits — see A0 §2.2 regex).
- **Price returns — Polygon:** `modal_workers/providers/polygon/market_data.py::PolygonMarketData.
  get_historical_prices(ticker, days)` → `/v2/aggs/ticker/{T}/range/1/day/{from}/{to}` (`adjusted=true,
  sort=asc`) returns daily OHLC `{t,o,h,l,c,v}` — the split/dividend-adjusted closes for t+1/7/30 (§5.2).
  Build the provider via `modal_workers/scanners/fda_signal_bridge.py::_build_polygon_providers()` (the
  verified reuse pattern; the labeler needs only the market-data half). **Polygon options/IV methods do
  not exist** — irrelevant to the labeler (it needs prices, not IV).

---

## 1. THE DAILY EMAIL DIGEST (the product surface)

### 1.0 Design intent
One email per day. For the watched ~20 names it answers — **band-only in v1** (Pedro 2026-06-03) — **"is
anything worth my attention today, and what is the model's CRL-risk read?"** — without opening a dashboard.
It is a **pure deterministic render** of what Phase 1 (band/rank) and Phase 2 (synthesis) already produced
and validated; the digest invents nothing, calls no LLM, scores nothing. Two visual jobs:
1. A **flagged section** at the top — the 0–N names that crossed the alert gate today (§1.1) — each a full
   synthesis card. This is "the 1–2 worth a look."
2. A **watchlist table** below — every in-window name, one row: band · rank, days-to-PDUFA, and a one-line
   `what_changed` if a synthesis fired today.

**v1 = band-only; the implied-move column is deferred to v1.1.** The v1 digest renders **no
market-implied-move column** — Polygon options is entitlement-gated and the synthesis ships its
`risk_vs_market.options_implied_move_pct` dormant (`null`), so the digest would have only "unavailable" to
show. Rather than render a column of "unavailable" (noise that reads as a missing feature), v1 **omits the
column entirely** and lets the band carry the risk read alone. The synthesis contract already *carries* the
implied-move fields, so v1.1 lights up the column with near-zero render change the day Polygon options lands
(§1.3, §10/RN). The product framing for v1 is therefore **"model risk band + what changed + the 1–2 worth a
look"**, NOT "risk vs the market-implied move" — say so to Pedro before positioning it as the latter (§9.1).

If **nothing is flagged**, the digest still sends (watchlist table + a "nothing crossed the attention
threshold today" header) — a silent no-send would make "did the monitor run?" ambiguous, violating
fail-loud. Config `l4.digest_send_when_empty` (default `true`) lets Pedro switch to "only email on a flag"
later (§6.4).

### 1.1 The row set + the gate that bolds a name
- **Row universe = today's in-window watchlist.** From `bc_candidates` where `g3_in_window = true AND tier
  IN ('active','watchlist')` (this excludes `refused`/`gate1_failed`/`gate2_failed`). Order
  `oof_percentile_rank DESC NULLS LAST` (highest model CRL-risk first), then `days_to_pdufa ASC` (nearest
  catalyst breaks ties). This is the watchlist table (§1.4).
- **Flagged subset (the bolded cards) — the §6.6 alert gate** (verified against memory `email_alert_gating`):
  a name is **flagged** iff **today's** `bc_thesis_updates` row exists for it AND
  `synthesis->>'recommended_action' ∈ {investigate, exit}` AND `(synthesis->>'confidence')::numeric ≥
  l4.digest_flag_min_confidence` (default **0.6**, per spec §6.6). **This honors email-gating exactly:** the
  *synthesis* (Phase 2's Sonnet pass, behind the deterministic threshold + corroboration clamp) IS the AI
  review; `recommended_action` reaching `investigate`/`exit` is the post-review promotion. The digest never
  flags off a raw `bc_market_signals` INSERT or a bare band — only off a reviewed, gate-passed synthesis.
  (`exit` is Python-capped to `investigate` for model-authored actions in Phase 2 §2.3; an operator-set
  `exit` via `bc_operator_overrides` still flags. The digest treats both as "flagged" and does **not**
  re-derive the cap.)

> **Why read the matview + thesis_updates, not re-run any logic:** the digest is downstream of all gating.
> Phase 2 already decided what fired and clamped the action; Phase 1 already set the band. The digest renders
> their committed output — which keeps "zero LLM in control flow" trivially true for Phase 3.

### 1.2 Synthesis-contract field → digest layout map (the flagged card)
Every card field is a **direct read** of `bc_thesis_updates.synthesis` (Phase 2 §1.1) — no transform beyond
formatting:
| synthesis field | card element |
|---|---|
| `headline` | card title (one-line subject for this name) |
| `risk_vs_market.model_risk_band` + `…model_percentile` | **the band badge** (`elevated · 78th pct`) — **the v1 risk read, standing alone** (no implied-move number beside it) |
| `risk_vs_market.options_implied_move_pct` + `…implied_move_horizon` + `…stance` | **v1.1 ONLY — NOT rendered in v1.** When Polygon options lands: an implied-move cell `±14% @ PDUFA · market underpricing risk` beside the band badge (§1.3). In v1 the renderer ignores these fields. |
| `what_changed` | the "what changed today" paragraph |
| `drivers[]` | a compact 1–4 row list (`stream · direction · magnitude — summary`), `evidence_ref` retained for Phase-4 deep-links |
| `bullets_up[]` / `bullets_down[]` | two short bulleted columns ("for" / "against") |
| `risks[]` | a "risks" bullet list |
| `watch_items[]` | **the call-to-attention** — 1–2 items as the card footer (`Watch: …`) — the literal "1–2 worth a look" the product promises |
| `recommended_action` + `confidence` | a labeled chip (`INVESTIGATE · conf 0.66`); drives whether the card is in the flagged section at all (§1.1) |
| `provenance.streams_available` | a small footnote (`streams: insider ✓ · options ✗ · news ✓`) so a missing stream is visible, not implied |

### 1.3 The implied-move column — **OMITTED in v1, lit up in v1.1** (band-only decision, Pedro 2026-06-03)
**v1 (now): there is NO implied-move column.** Polygon options is entitlement-gated (snapshot = 403 at the
current tier), so per the Phase-2 fetcher plan **options ships dormant** and the synthesis carries
`risk_vs_market.stance='indeterminate_no_options'`, `options_implied_move_pct=null`,
`implied_move_horizon='unavailable'`, `provenance.streams_available.options=false` for essentially every name.
A column that reads "unavailable" on every row is noise that scans as a broken feature, so **the v1 digest
omits the implied-move column from both the flagged card and the watchlist table** and lets the band carry
the risk read alone. The only acknowledgement of the absent stream in v1 is the per-card
`provenance.streams_available` footnote (`streams: insider ✓ · options ✗ · news ✓`, §1.2) — which is a
factual provenance line, not a column promising a value it cannot give.

**v1.1 (when Polygon options lands): the column lights up — these are the rules to implement then, kept here
so the upgrade is a near-zero render change** (the synthesis already carries the fields):
- **Data-driven, not a config flag.** Gate the column's appearance on
  `provenance.streams_available.options=true` for **at least one** in-window row. While it is `false` for
  **all** rows (the v1 reality), the column does not render at all — there is no "unavailable" placeholder,
  because there is no column.
- Per name with options available → render `±{options_implied_move_pct}% @ {horizon} · {stance phrase}` (e.g.
  `±14% @ PDUFA · market underpricing risk`) beside the band badge. Stance→phrase map:
  `market_underpricing_risk`→"market underpricing risk", `market_overpricing_risk`→"market overpricing risk",
  `aligned`→"aligned with market".
- A name still missing options after the tier upgrade (`stance='indeterminate_no_options'` /
  `options_implied_move_pct=null` while other rows have it) renders the literal **"implied move unavailable"**
  in its cell — **never** a blank cell, `0%`, or a bare `—` that reads as "no move". (This per-cell honest
  fallback only exists once the column exists, i.e. v1.1+.)
- When v1.1 first ships the column, add a one-time header note ("Options-implied move now available via
  Polygon …") if useful; v1 ships **no** options-related header note (there is nothing to explain — the
  column simply is not there).

> **Why omit rather than render "unavailable" everywhere:** the earlier draft rendered an "unavailable"
> implied-move cell on every row plus a global "options unavailable on this tier" header note. Pedro's
> 2026-06-03 band-only decision is to **not surface the market-implied-move dimension at all in v1** — the
> column, the per-cell "unavailable" string, and the global tier-note are all v1.1. This keeps v1 honest by
> *omission* (the band is the only risk number, full stop) rather than by a row of disclaimers. The §8 render
> tests assert v1 emits **no implied-move column and no "implied move unavailable" string** (the inverse of
> the old draft's golden); the per-cell-unavailable golden moves to the v1.1 test set (§8.1).

### 1.4 The watchlist table (every in-window name, one row) — **band-only columns in v1**
| column | source | note |
|---|---|---|
| Name | ticker (`sponsor_cik → bc_company_tradeable.ticker`) else `application_number` | symbol if resolvable |
| Band | `bc_candidates.risk_band` | the badge; **never `p_crl`** |
| Rank | `bc_candidates.oof_percentile_rank` | "78th" (within-universe rank) |
| PDUFA | `bc_candidates.days_to_pdufa` | "41d" |
| Changed today | today's `synthesis->>'headline'` (truncated) if a row fired, else "—" | links table↔cards |
| Flag | `recommended_action` if flagged (§1.1) | "🔴 investigate" / blank |
- **No "Implied move" column in v1** (band-only, §1.3). v1.1 inserts an `Implied move` column between Band/Rank
  and PDUFA, sourced from today's synthesis `risk_vs_market` (per-cell "unavailable" fallback per §1.3), only
  once `streams_available.options=true` for ≥1 row.
- Low-feature-coverage rows (Phase 1 marks `feature_quality='low'` on `bc_application_features`) get a
  subtle `·low-conf` rank caveat per A0's display decision (`bc_v4_a0_rank_confidence_note.md`). The matview
  does **not** surface `feature_quality`, so the digest either (a) adds a small `LEFT JOIN LATERAL` to the
  latest `bc_application_features` row, or (b) waits for Phase 1 to add it to the matview. Until then the
  caveat is best-effort; default prominence = **caveat** until A0's note lands (§10 hand-off).

### 1.5 Subject line + send envelope
- **Subject:** `[BC-FDA] {today} — {n_flagged} flagged · {n_watch} watched` (e.g.
  `[BC-FDA] 2026-06-03 — 1 flagged · 19 watched`); 0 flagged → `… nothing flagged · 19 watched`. The
  distinct `[BC-FDA]` prefix never collides with v2/v3 `[IMMEDIATE]`/`[PRE-EDGE]`.
- **From:** env `BC_DIGEST_FROM_ADDRESS` (default reuses `Conan Alerts <alerts@alerts.solutz.com>`, set in
  the edge fn's secrets alongside the inherited `RESEND_FROM_ADDRESS`).
- **Recipients:** `notifications_prefs.email_on_immediate=true` → `auth.admin.listUsers` (the **same opt-in
  pool and the same two-hop** `fanout` uses — Pedro is already on it; §3.4 reuses `resolveRecipients`
  verbatim). A `BC_DIGEST_DEV_RECIPIENTS` env override (comma list) mirrors `FAN_OUT_DEV_RECIPIENTS` for
  testing.
- **One email per recipient per digest day** — idempotency in §3.3.

### 1.6 Worked example digest (v1 band-only — NO implied-move column anywhere)
Universe of 3 (illustrative); today PRTX crossed the gate, AXSM and VKTX did not. **There is no implied-move
column and no options tier-note — the band is the only risk number** (band-only v1).
```
Subject: [BC-FDA] 2026-06-03 — 1 flagged · 3 watched

BC-FDA daily monitor — 2026-06-03

──────────────────────────────────────────────────────────────────────
🔴 FLAGGED (1)
──────────────────────────────────────────────────────────────────────
PRTX (BLA-761333) — INVESTIGATE · conf 0.66
  Risk: elevated · 78th pct
  What changed: Three insiders (2 directors, CFO) bought $2.1M open-market over
    14 days and an 8-K manufacturing-buildout filing hit, 41 days before PDUFA.
  Drivers:
    • insider · bullish · notable — cluster: 2 directors + CFO, $2.1M/14d, no 10b5-1
    • news    · bullish · minor   — 8-K manufacturing scale-up (primary tier)
  For:    insider cluster buying into the PDUFA window; manufacturing buildout
  Against: model sits in the elevated band (78th pct) — first-cycle CRL base rate material
  Risks:  manufacturing 8-K corroborated by SEC filing but launch outcome unproven
  Watch:  ▸ 8-K cadence into PDUFA — a CRL-risk or financing 8-K would flip the read
  streams: insider ✓ · options ✗ · news ✓ · PDUFA in 41d

──────────────────────────────────────────────────────────────────────
WATCHLIST (3)        band · rank        PDUFA   changed today        flag
──────────────────────────────────────────────────────────────────────
PRTX  BLA-761333     elevated · 78th    41d     insider cluster…     🔴 investigate
AXSM  NDA-216789     moderate · 41st    63d     —
VKTX  NDA-217001     low · 12th         96d     —    ·low-conf
──────────────────────────────────────────────────────────────────────
Model band is a ranking input, not a calibrated probability.
```
PRTX is flagged (`investigate` + conf 0.66 ≥ 0.6); AXSM/VKTX are watch-only. The `streams: … options ✗ …`
footnote is the only mention of options — there is no implied-move cell or column to render, so the band
(`elevated · 78th pct`) carries the risk read alone, exactly as the band-only v1 decision requires. v1.1 adds
an `Implied move` column here once Polygon options lands (§1.3).

---

## 2. DIGEST DATA FLOW + GATING (edge function)

### 2.1 The read query (one pass, matview-anchored) — runs **inside the edge fn** via the service-role client
```sql
SELECT c.application_number, c.risk_band, c.oof_percentile_rank,   -- p_crl deliberately NOT selected
       c.appl_type, c.pdufa_date, c.days_to_pdufa, c.tier, c.materialized_at,
       t.ticker,
       u.synthesis, u.trigger_reasons, u.fired_at
FROM public.bc_candidates c
LEFT JOIN LATERAL (
   SELECT ticker FROM public.bc_company_tradeable
   WHERE sponsor_cik = c.sponsor_cik ORDER BY snapshot_date DESC LIMIT 1
) t ON true
LEFT JOIN public.bc_thesis_updates u
   ON u.application_number = c.application_number AND u.update_date = $1   -- today (UTC)
WHERE c.g3_in_window = true AND c.tier IN ('active','watchlist')
ORDER BY c.oof_percentile_rank DESC NULLS LAST, c.days_to_pdufa ASC;
```
- **`p_crl` is omitted from the SELECT** — a *structural* guarantee it can never be rendered (the matview
  exposes it, but the digest's row struct never carries it). This is the explicit-column-select the
  constraint in the brief demands. `p_crl` is read **only** by the labeler (§4), in a separate worker.
- The digest is **SELECT-only** on all `bc_*` tables except its own `bc_digest_sends` idempotency rows
  (§3.3) and its `bc_pipeline_runs` open/close (§4). `feature_quality` for the low-conf caveat (§1.4) is a
  best-effort second LATERAL to `bc_application_features` (latest by `snapshot_date`), omitted in v1 if
  Phase 1 hasn't surfaced it.
- **Edge runtime:** the `@supabase/supabase-js` client created with `SUPABASE_SERVICE_ROLE_KEY` (exactly as
  `scanner-health/index.ts` and `fanout` do) can run this via `.rpc()` on a thin SECURITY DEFINER reader or
  via PostgREST `.from('bc_candidates').select(...)` with embedded resources. **Decision:** use a single
  `bc_digest_rows(p_day date)` SECURITY DEFINER SQL function returning the row set above (one round trip,
  the LATERAL joins are awkward in PostgREST), created in §6's migration and granted to the function's
  caller. This keeps the SQL in version control and the edge fn thin.

### 2.2 Flag computation (pure JS/TS, deterministic — no LLM, no threshold re-derivation)
For each row with a non-null `synthesis`:
```ts
const action = synthesis.recommended_action;
const conf   = Number(synthesis.confidence);
const flagged = (action === "investigate" || action === "exit")
                && conf >= flagMinConfidence;   // flagMinConfidence from bc_config l4.digest_flag_min_confidence (default 0.6)
```
`flagged` rows → the cards section; **all** rows → the watchlist table.

### 2.3 Render → send (the edge-fn happy path)
Build the HTML + text bodies (§1) → resolve recipients (§3.4, reuse `resolveRecipients`) → per-recipient
idempotency check (§3.3) → direct Resend POST (§3.4) → record the send in `bc_digest_sends`. The whole run
is wrapped in `bc_pipeline_runs` open/close (§4) — **the function opens the row first thing and closes it in
a `finally`, so even a thrown render error stamps a `failed` row** (send-or-throw + liveness).

### 2.4 Email-gating compliance restated (memory `email_alert_gating`)
The rule: emails fire only after **AI review + promotion**, not on a raw INSERT. BC mapping:
- **AI review** = the Phase-2 Sonnet synthesis, which only runs behind the deterministic threshold +
  corroboration clamp (Phase 2 §2–§3). A `bc_thesis_updates` row existing at all means review happened.
- **Promotion** = `recommended_action ∈ {investigate, exit}` ∧ `confidence ≥ 0.6` (§1.1) — the BC analogue
  of "promoted to pre-edge."
- The **flagged** cards fire only on that promotion; the watchlist table is audit/context (it does not
  "page" — analogous to `fanout`'s alerts-INSERT audit-only path). **No BC email ever fires off a raw
  `bc_market_signals`/`bc_news_events` INSERT.** Compliance is structural: the only email trigger is "a
  gate-passed synthesis exists today."

---

## 3. THE TRANSPORT DECISION (investigated live) — **standalone edge fn, fired by pg_cron, calls Resend directly**

### 3.1 Decision
**Build the digest as a new standalone Supabase edge function `bc-digest`, fired once a day by a pg_cron
`net.http_post` tick, that calls Resend directly** (its own ~8-line `fetch`, reusing the runtime's existing
`RESEND_API_KEY`), with its own `bc_digest_sends` idempotency table. **Do NOT reuse `fanout`; do NOT add a
DB trigger on `bc_thesis_updates`.** This is the brief's "call Resend direct + standalone digest worker"
intent, realized as an edge fn rather than a Modal worker because live verification makes the edge fn the
lower-friction host (§3.5).

### 3.2 Why standalone, not `fanout` (evidence-backed)
1. **`fanout` is trigger-coupled, and the trigger is the thing we must not entangle.** All four entry points
   are webhook-triggered (`:96`); reusing it means a **5th entry point keyed on a webhook/trigger on
   `bc_thesis_updates`** — bolting BC onto the same edge-fn + trigger fabric that carries the v4 convergence
   path. **strangle-don't-entangle** forbids exactly this. A daily digest is also the **wrong shape** for a
   row-INSERT webhook: it is a once-a-day *batch* over ~20 names, not a per-row reaction — a trigger would
   fire ~20×/day or need a synthetic "digest tick" row.
2. **`alert_deliveries` has no BC-shaped slot (verified).** FKs `alert_id/candidate_event_id/candidate_id/
   assessment_id`; dedup unique `(assessment_id, channel, target) WHERE assessment_id IS NOT NULL`. No
   `application_number` / `bc_thesis_update_id`, no BC dedup key. Reuse would need a migration on a shared
   v2/v3 table — extra blast radius for zero BC benefit, plus the cross-system coupling the strangle plan
   avoids. The BC-owned `bc_digest_sends` (§3.3) is cleaner and isolated.
3. **No sender abstraction to inherit.** `fanout` inlines the Resend `fetch` three times (`:493/:716/:959`);
   reusing "the path" saves no library. The only thing it offers is `resolveRecipients` + the 23505 dedup
   idiom — both reproduced in a handful of lines against the **same** `notifications_prefs` pool.
4. **Standalone buys independent liveness + the strangle off-ramp.** `bc-digest` writes its own
   `bc_pipeline_runs` row (the uniform liveness primitive) and can be paused/retired independently when v4's
   FDA path is strangled off (`UPDATE cron.job SET active=false WHERE jobname='bc-digest-daily'`) — no shared
   edge function to coordinate. It keeps the daily path zero-Cowork, zero-shared-trigger.

### 3.3 Idempotency (don't email a recipient twice/day) — new tiny table `bc_digest_sends`
One row per `(digest_date, target)`, inserted **before** the Resend POST under a UNIQUE constraint; a 23505
= "already sent today" → skip (idempotent re-run, mirrors `fanout`'s `alert_deliveries` 23505 at `:941`):
| column | type | note |
|---|---|---|
| `id` | uuid PK | `gen_random_uuid()` |
| `digest_date` | date NOT NULL | today (UTC) |
| `target` | text NOT NULL | recipient email |
| `flagged_app_numbers` | text[] | which apps were flagged (audit + "did we page this?") |
| `n_watch` | int | watchlist size |
| `status` | text NOT NULL | `sent` / `failed` (**CHECK ∈ {`sent`,`failed`}**) |
| `resend_message_id` | text | |
| `response_body` | jsonb | Resend response (audit) |
| `sent_at` | timestamptz | `now()` |
- **UNIQUE `(digest_date, target)`.** Insert-then-send: insert `status='sent'` (optimistic) in a try; on
  23505 skip that recipient + no POST; after the POST, UPDATE `resend_message_id`/`response_body`, or set
  `status='failed'` on a non-2xx (so a failed send is visible; a same-day retry is allowed only by the
  operator clearing the row — default is "one attempt/day", fail-loud). BC-owned, disk-migration-created
  (§6), never touches `alert_deliveries`.
- **Granularity = `(date, target)`** (one digest per person per day), **not** per-name (the digest is a
  single batch email). A same-day re-run with new flagged names does **not** re-email (the day's row exists);
  the operator sees the next day's digest. A `?force=1` query param (service-role only) bypasses for a manual
  resend, logged.

### 3.4 The edge-fn send path (concrete spec, reusing fanout's verified pieces)
- **Recipients** — **copy `fanout`'s `resolveRecipients`** (`index.ts:281–303`) verbatim into `bc-digest`:
  query `notifications_prefs.email_on_immediate=true` → `sb.auth.admin.listUsers({perPage:200})` → map
  `user_id`→email; fall back to `BC_DIGEST_DEV_RECIPIENTS` env when empty (dev). **No new RPC needed** —
  this is the single biggest win over the all-Modal draft, which had to add a `bc_digest_recipients()`
  SECURITY DEFINER RPC because Modal has no `auth.admin`.
- **Send** — for each recipient not already in `bc_digest_sends` for today:
  `POST https://api.resend.com/emails` with `{from: BC_DIGEST_FROM_ADDRESS, to:[email], subject, html, text}`
  and `Authorization: 'Bearer ' + Deno.env.get('RESEND_API_KEY')` (**the runtime secret `fanout` already
  uses** — nothing new to provision). Mirror `fanout`'s response handling (`:497–:513`): on `r.ok` record
  `resend_message_id`; else `status='failed'` + `response_body`. **No retry inside the fn** (Resend
  transient failures surface as a failed row + a `partial` run — fail-loud; the next day's digest
  re-includes the name).
- **Template** — pure functions `renderDigestHtml(rows, today): string` and `renderDigestText(rows, today):
  string` consuming the §2.1 row list; the §1.2 field map + §1.3 honest-options rules live here. **Reuse the
  `escapeHtml` helper** (port `fanout`'s `:403`). No Storage upload in v1 (Phase 4 reads `bc_thesis_updates`
  directly); an optional `reports`-bucket archive is post-v1.
- **Auth on the function itself:** deploy with the same posture as `fanout`/`scanner-health` — the function
  trusts the `pg_cron` caller via a service-role bearer (or a shared `x-service-key`, as `scanner-health`
  does at its header check) so only the cron tick (and an operator with the service key) can invoke it. Set
  `verify_jwt = false` for `bc-digest` in `supabase/config.toml` and gate on the service-role header inside
  the function (the digest is machine-triggered, not user-facing).

### 3.5 Edge fn vs the all-Modal draft (head-to-head — recorded so the choice is auditable)
| factor | **edge fn + pg_cron (chosen)** | Modal worker (the prior draft `bc_v4_phase3_digest_outcomes.md`) |
|---|---|---|
| Resend secret | **reuses runtime `RESEND_API_KEY`** (already live for `fanout`) | needs a **new Modal `RESEND_API_KEY`** secret (Pedro must add/mint) |
| recipient resolve | **reuses `auth.admin.listUsers`** (copy `resolveRecipients`) | needs a new `bc_digest_recipients()` SECURITY DEFINER RPC (Modal has no `auth.admin`) |
| scheduling | **`pg_cron` `net.http_post` tick** — unbounded job count; the repo's standard idiom (earnings_calendar, ic_memo) | rides `dispatch_release_times`; must add hour `15` to its `@modal.Cron` **(the draft's #5 hand-off risk)** — conan-v2 already has **4 of the 5** allowed crons |
| 5-cron cap | **not affected** (cron lives in Postgres) | tight: conan-v2 has 4 crons; the labeler also wants a slot |
| coupling to v4 | none (own fn, own cron job) | none (own Modal fn) — equal here |
| liveness | own `bc_pipeline_runs` row | own `bc_pipeline_runs` row — equal |
| language fit | digest is pure string-rendering of JSON the DB already holds → TS/Deno is a fine fit | Python, but no Python-only dependency in the digest |
**Conclusion:** the edge fn removes two net-new provisioning items (Modal Resend secret, recipients RPC) and
the only cron-hour-wiring risk, with no downside for a pure renderer. **The labeler is the opposite** — it
*needs* Polygon + openFDA Python — so it stays Modal (§4). If Pedro wants the all-Modal symmetry for one
deploy surface, the draft's Modal-digest path is the documented fallback (it works; it is just heavier).

---

## 4. OUTCOME LOGGING (the one surviving feedback element — NO refit) — **Modal worker**

### 4.0 Intent
When a watched PDUFA **resolves**, record what actually happened — the regulatory verdict + the price
reaction — **paired with the `p_crl`/band that was shown at prediction time**. That's it. **No drift alarms,
no gated refit** (refit ≈ 1 CRL/yr makes a loop pointless; the `l7.refit_min_crl_events=30` config and the
`bc_refit_log` table exist but Phase 3 **neither reads nor writes** them). The log is the evidence base a
*human* re-reads when (rarely) re-vendoring the scorer.

### 4.1 Cadence + worker
A **separate daily Modal cron** `bc_outcome_labeler` (NOT folded into the digest — different failure domain,
different language need, and it must keep labeling even if the digest breaks). It writes its own
`bc_pipeline_runs` row (`pipeline_name='bc_outcome_labeler'`). It is a Modal worker because it needs the
Polygon provider + the openFDA read path (Python). Daily is ample: it scans for newly-resolved PDUFAs and
for maturing price horizons.

### 4.2 The "prediction at PDUFA time" snapshot
The outcome must pair against **the band shown when the bet was live**, not today's band. Source = the
`bc_rubric_scores` row (its `p_crl`/`risk_band`) **as of the PDUFA date** for that app — the last score with
`scored_at <= pdufa_date` (`bc_rubric_scores` has an index on `(application_number, scored_at DESC)`). Its
`p_crl` → `bc_prediction_outcomes.scored_p_crl`; its `risk_band` feeds `hypothesis_outcome` (§5.4). Phase 1
scores weekly and stores every run, so the pre-PDUFA score is retrievable historically.

### 4.3 Resolution detection (which apps to label)
An in-window or just-past-window app is **resolved** when any of:
- a CRL Transparency record matches its `application_number` (→ `regulatory_outcome='crl'`), OR
- Drugs@FDA shows an `AP` ORIG submission on/after its PDUFA (→ `approved`), OR
- Drugs@FDA shows a withdrawal (→ `withdrawn`), OR
- its `bc_application_features.pdufa_date` moved later than the previously-recorded one with no decision
  (→ `extended`; §5.5).
The labeler considers apps with `pdufa_date <= today` (resolution can lag the date by days) that **lack** a
complete `bc_prediction_outcomes` triple, plus apps with an outcome row but immature price horizons (§5.3).

---

## 5. OUTCOME-LABELER DATA CONTRACTS

### 5.1 Regulatory outcome — source precedence (most authoritative first)
1. **CRL Transparency** (`openfda_crl_transparency.py`): match on digit-normalized `application_number`,
   `letter_year >= pdufa_year` → `crl`. (100% FDA-keyed, A0 §1.1.) ⚠️ **Module not built yet** (A0 §6) —
   gate this branch behind "if the module imports," else log `crl_source_unavailable` and continue.
2. **Drugs@FDA** (`ingest_drugsfda_approvals` read path / `extract_submission_rows`): an ORIG submission
   `submission_status='AP'` with `submission_status_date >= pdufa_date` → `approved`; a withdrawal status
   (`WD`) → `withdrawn`.
3. **PDUFA extension** (§5.5): `pdufa_date` advanced with no decision → `extended` (a **non-terminal**
   outcome; the row stays overwrite-eligible for the eventual terminal verdict via the merge upsert).
If none yet → app stays unresolved (normally no outcome row written; a prior partial row may still accrue
price horizons). **All regulatory_outcome values are lowercase** (`approved|crl|withdrawn|extended`) per the
verified CHECK (§0.2) — emit lowercase, never `CRL`/`Approved`.

### 5.2 Price returns t+1 / t+7 / t+30 (Polygon)
- Ticker via `sponsor_cik → latest bc_company_tradeable.ticker` (§0.4). No ticker ⇒ skip price (record the
  verdict only; `price_return_pct=null`; log `no_ticker`).
- Pull daily adjusted bars bracketing PDUFA with `PolygonMarketData.get_historical_prices(ticker, days≈45)`
  (built via `_build_polygon_providers()`). **Base** = the adjusted close on the **last trading day strictly
  before** `pdufa_date`; **t+N** = the adjusted close on the Nth **trading day** at/after `pdufa_date`.
  `price_return_pct = (close_tN / base − 1) * 100`. Trading-day counting on the returned bars (the
  aggregates contain only trading days, so weekends/holidays are skipped naturally).
- **Horizon maturity:** t+1 ≈ 1 trading day post-PDUFA; t+30 ≈ 30 trading days (~6 weeks). The labeler
  writes the horizons mature **now** and re-runs daily to fill the rest; `regulatory_outcome` is duplicated
  across the three horizon rows so each row is self-describing.

### 5.3 Write contract — three rows, merge-upsert, partial-friendly (null-omitting)
Per resolved app, for `horizon_days ∈ l4.outcome_price_horizons` (default `[1,7,30]`):
```
row = {
  application_number,
  horizon_days: N,
  regulatory_outcome: <crl|approved|withdrawn|extended — OMIT if not yet known>,
  price_return_pct:   <computed if horizon N is mature — OMIT otherwise>,
  hypothesis_outcome: <§5.4 — OMIT until both a pre-PDUFA band and a terminal verdict exist>,
  scored_p_crl:       <p_crl from the pre-PDUFA bc_rubric_scores row (§4.2)>,
}
UPSERT on_conflict=application_number,horizon_days  prefer=resolution=merge-duplicates,return=minimal
```
- **`regulatory_outcome` NULLABLE is the enabler** (§0.2): write the verdict the day it's known (3 rows,
  prices null), then **merge** each `price_return_pct` as that horizon matures. **Build the upsert body to
  include only the fields it can fill** (omit nulls) so a later merge never clobbers a set value with null.
  The UNIQUE `(application_number, horizon_days)` makes each merge idempotent.
- A verdict **upgrade** (`extended` → terminal) overwrites `regulatory_outcome` on all three rows on the
  next run (the new value is non-null, so it is included in the body).

### 5.4 `hypothesis_outcome` (did the band's read match reality — free text, no CHECK)
Pairs the **shown band** (pre-PDUFA, §4.2) with the **terminal** `regulatory_outcome`, for the human
re-read:
- band `elevated`/`high` + `crl` → `band_correct_high_risk`.
- band `low`/`moderate` + `approved` → `band_correct_low_risk`.
- band `elevated`/`high` + `approved` → `band_overstated_risk` (model worried, drug approved).
- band `low`/`moderate` + `crl` → `band_understated_risk` (model calm, drug CRL'd — the costly miss).
- `extended`/`withdrawn` or no band → `indeterminate`.
Computed only when both a pre-PDUFA band and a **terminal** verdict exist; else omitted. Logging, not
gating — feeds no alarm.

### 5.5 PDUFA extension handling
A date push (`bc_application_features.pdufa_date` advanced vs the value last seen, no terminal verdict) →
`regulatory_outcome='extended'` — a **non-terminal** marker (the bet is still live, just later). The row is
overwrite-eligible: when the new PDUFA resolves, the terminal verdict replaces `extended` via the merge
upsert. To detect a push the labeler keeps the last-seen PDUFA per app in `bc_pipeline_runs.log` (or
re-derives from the `bc_application_features` snapshot history) — **no new column**.

### 5.6 Lifecycle hygiene — retire past-dated still-pending rows (no destructive writes)
Because "pending" is implicit (§0.4), a **past-dated** PDUFA with **no** outcome row is a stuck "pending"
polluting the universe. Each run, the labeler finds apps with `pdufa_date < today − l4.outcome_resolve_
grace_days` (default **14**) and no outcome row and:
- attempts resolution once more (§5.1); if still unresolvable (tiny sponsor, no Transparency/Drugs@FDA
  trace), **logs** it to `bc_pipeline_runs.log` as `stale_pending_unresolved` (operator eyes) — it does
  **not** fabricate a verdict, and it does **not** delete/mutate `bc_applications`/`bc_application_features`.
- The **digest** drops the app automatically once `days_to_pdufa < 0` pushes `g3_in_window` false — so no
  extra digest filter is needed. Hygiene here is only about getting a resolved-history row written or the
  gap surfaced.

---

## 6. FILES TO CREATE / MODIFY (paths)

```
# ── DIGEST (edge function — TypeScript/Deno) ──────────────────────────────────
supabase/functions/bc-digest/index.ts        # §2–§3 — the fn: open bc_pipeline_runs → call bc_digest_rows($today)
                                              #   → flag(§2.2) → render → resolveRecipients (copied from fanout)
                                              #   → per-recipient bc_digest_sends idempotency → direct Resend POST
                                              #   → close bc_pipeline_runs (finally). verify_jwt=false + service-role gate.
supabase/functions/bc-digest/render.ts        # §1 — pure renderDigestHtml / renderDigestText (field map §1.2 + band-only v1, NO implied-move column §1.3) + escapeHtml (ported from fanout)
supabase/functions/bc-digest/deno.json        # (if the repo's fns pin imports; match fanout/scanner-health layout)

# ── MIGRATION (disk-first, then `supabase db push`; NOT MCP apply — feedback_mcp_apply_migration_discipline) ──
supabase/migrations/<ts>_bc_digest_and_outcomes.sql
        # 1. CREATE TABLE public.bc_digest_sends (§3.3): UNIQUE(digest_date,target), CHECK status∈{sent,failed}.
        # 2. CREATE FUNCTION public.bc_digest_rows(p_day date) RETURNS TABLE(...) SECURITY DEFINER  -- the §2.1 query.
        # 3. bc_config seeds (§6.4): l4.digest_flag_min_confidence, l4.digest_send_when_empty,
        #    l4.outcome_resolve_grace_days, l4.outcome_price_horizons.
        # 4. pg_cron jobs (§6.5): 'bc-digest-daily' (net.http_post → the bc-digest fn URL, vault bearer);
        #    'bc-outcome-labeler-daily' (net.http_post → the Modal labeler endpoint, vault compute_secret).
        #    Both: unschedule-if-exists then schedule (idempotent), exit-clean-on-unconfigured-URL (earnings_calendar idiom).

# ── OUTCOME LABELER (Modal worker — Python) ───────────────────────────────────
modal_workers/bc_monitor/outcomes/__init__.py
modal_workers/bc_monitor/outcomes/resolve.py       # §5.1/§5.5 — regulatory outcome via openfda_crl_transparency (gated on import) + drugsfda read path
modal_workers/bc_monitor/outcomes/price_returns.py # §5.2 — t+1/7/30 via PolygonMarketData.get_historical_prices (_build_polygon_providers)
modal_workers/bc_monitor/outcomes/run_labeler.py   # §4/§5 — resolve→price→merge-upsert bc_prediction_outcomes; stale-pending sweep; bc_pipeline_runs open/close
modal_workers/app.py                               # ADD: bc_outcome_labeler_once fastapi_endpoint (label e.g. 'bc-outcome-labeler', secrets=[scanner_secrets, supabase_secrets, compute_auth_secrets]) — the pg_cron POSTs to its URL; verify x-conan-compute-secret like the other compute endpoints
internal_config (DB rows, not code)                # INSERT modal_url_bc_outcome_labeler (the deployed endpoint URL) — mirrors modal_url_earnings_calendar_fetch_daily; pg_cron reads it, exits clean if empty

# ── TESTS ─────────────────────────────────────────────────────────────────────
supabase/functions/bc-digest/render.test.ts        # §8.1 render/gating (band-only golden: NO implied-move column; p_crl-never-rendered; v1.1 column golden skipped)
modal_workers/tests/test_bc_outcome_labeler.py      # §8.3 outcome upsert/merge/hypothesis/price math
modal_workers/tests/test_bc_outcome_e2e.py          # §8.5 resolved-catalyst → bc_prediction_outcomes triple
```
**Reuse (do NOT modify):** `supabase/functions/fanout/index.ts` (`resolveRecipients` `:281`, Resend POST
`:493`, `escapeHtml` `:403`, 23505 idiom `:941` — **copy the small pieces into `bc-digest`; the function is
not shared**); `supabase/functions/scanner-health/index.ts` (standalone service-role edge-fn + `x-service-key`
pattern); `modal_workers/providers/polygon/market_data.py::get_historical_prices`;
`modal_workers/scanners/fda_signal_bridge.py::_build_polygon_providers`;
`modal_workers/ingestion/openfda_ingest.py` (`ingest_drugsfda_approvals` `:119`, `extract_submission_rows`
`:288`, `_openfda_get` `:79`); `modal_workers/fetchers/universe/openfda_crl_transparency.py` (A0 §6,
pending); `modal_workers/shared/openfda_client.py`; `modal_workers/shared/supabase_client.py`
(`_rest_with_retry`, on_conflict upsert idiom); `modal_workers/shared/bc_pipeline_runs.py` (Phase 1's
open/close helper — **verify it emits the CHECK-valid `succeeded`/`failed`/`partial` tokens**, §0.1; if it
emits `ok`/`error`, fix there once for all phases); the pg_cron+vault idiom in
`supabase/migrations/20260605000050_earnings_calendar_pg_cron.sql`.

### 6.4 `bc_config` seeds (this plan's keys only)
| key | default | purpose |
|---|---|---|
| `l4.digest_flag_min_confidence` | `0.6` | confidence floor for `investigate`/`exit` to flag a name (spec §6.6) |
| `l4.digest_send_when_empty` | `true` | send the digest (watchlist only) even when 0 names flagged |
| `l4.outcome_resolve_grace_days` | `14` | days past PDUFA before a still-unresolved app is swept as stale-pending (§5.6) |
| `l4.outcome_price_horizons` | `[1,7,30]` | the t+N horizons logged (jsonb array; drives the 3-row write) |
The digest (edge fn) reads `l4.digest_*` via a `SELECT value #>> '{}' FROM bc_config WHERE key=…` (one query,
defaults inlined in TS if missing). The labeler reads via the shared `bc_monitor/config.py` `get_*` helper.

### 6.5 Cron wiring (the seam — digest AFTER the monitor; labeler after US close)
The daily chain (per Phase-2 fetcher plan): `bc_universe_pdufa @11 UTC → bc_fetchers @13 UTC →
bc_daily_monitor @14 UTC` (these are the **upstream** crons; not this plan's). This plan adds two **pg_cron**
jobs (both in the §6 migration; both idempotent unschedule-then-schedule; both exit-clean if the target URL
is unconfigured — the earnings_calendar idiom):
- **`bc-digest-daily` @ 15 UTC** (`'0 15 * * *'`): `net.http_post` to the `bc-digest` edge-fn URL with a
  service-role bearer (read from `vault.decrypted_secrets`/`internal_config` like the earnings job reads
  `compute_secret`). 15 UTC is ≥1 h after `bc_daily_monitor`'s 14 UTC so today's `bc_thesis_updates` exist.
  **No Modal cron consumed** — this is the decisive advantage over the all-Modal draft (which had to wedge
  hour 15 into `dispatch_release_times`).
- **`bc-outcome-labeler-daily` @ 22 UTC** (`'0 22 * * *'`): `net.http_post` to the Modal labeler endpoint
  (URL in `internal_config.modal_url_bc_outcome_labeler`, bearer = `compute_secret`). 22 UTC is after US
  close so the day's t+1 bar exists for any same-day-resolved PDUFA. Independent of the digest's success.
- **Kill switches:** `UPDATE cron.job SET active=false WHERE jobname IN ('bc-digest-daily',
  'bc-outcome-labeler-daily')` — the per-job off-switch the strangle off-ramp wants.
- **Stricter option (offered, not default):** instead of a fixed 15 UTC tick, the `bc_daily_monitor` Modal
  fn could `net.http_post` the digest URL on success (a true DAG edge, one line). The 15 UTC time-gap matches
  the codebase idiom and is the default; the on-success edge is the upgrade if monitor latency drifts.

---

## 7. DATA FLOW (end to end)
```
        ── DIGEST (edge fn bc-digest, pg_cron @15 UTC, after bc_daily_monitor) ──
 pg_cron 'bc-digest-daily' ──net.http_post──► supabase/functions/bc-digest
        │
 bc_candidates (band/rank/in-window, tier∈{active,watchlist}) ─┐
 bc_company_tradeable (sponsor_cik→ticker) ───────────────────┤→ bc_digest_rows($today) → rows  (NO p_crl in the struct)
 bc_thesis_updates[update_date=today] (synthesis) ────────────┘        │
                                                                       │ flag = action∈{investigate,exit} ∧ conf≥0.6  (pure TS, no LLM)
                                                                       ▼
                                          render.ts → HTML+text  (band-only v1 — NO implied-move column; v1.1 adds it)
                                                                       │
                                resolveRecipients (notifications_prefs.email_on_immediate → auth.admin.listUsers; copied from fanout)
                                                                       │  per (digest_date,target) not in bc_digest_sends:
                                                                       ▼
                                  POST api.resend.com/emails (RESEND_API_KEY — the runtime secret fanout already uses)
                                                                       │ → INSERT/UPDATE bc_digest_sends (idempotent, UNIQUE(date,target))
                                            open/close bc_pipeline_runs('bc_daily_digest')  ◀── finally (send-or-throw)

        ── OUTCOME LABELER (Modal bc_outcome_labeler, pg_cron @22 UTC, independent) ──
 pg_cron 'bc-outcome-labeler-daily' ──net.http_post──► Modal bc_outcome_labeler_once
        │ in-window/just-past apps lacking a complete outcome triple
        │ resolve.py:  openfda_crl_transparency→crl (gated on import) | drugsfda AP→approved | WD→withdrawn | pdufa moved→extended
        │ price_returns.py:  get_historical_prices → base=pre-PDUFA close; t+1/7/30 adjusted closes (trading-day count)
        │ pair scored_p_crl from pre-PDUFA bc_rubric_scores (§4.2); hypothesis_outcome (§5.4)
        ▼
  UPSERT bc_prediction_outcomes  (3 rows/app, on_conflict=application_number,horizon_days, merge, null-omitting)
        │ stale-pending sweep (§5.6): pdufa_date<today-grace ∧ no outcome → resolve-or-LOG (never fabricate, never delete)
        ▼
  open/close bc_pipeline_runs('bc_outcome_labeler')  ◀── finally   (NO refit, NO drift alarm — never touches bc_refit_log/l7.*)
```

---

## 8. TEST PLAN
**No live Resend/Polygon/openFDA/Anthropic** — fakes/fixtures throughout. Edge-fn tests run under Deno
(`deno test`, matching `supabase/functions/_shared/*.test.ts`); labeler tests under pytest.

### 8.1 Digest render + gating — `supabase/functions/bc-digest/render.test.ts`
- **Flag gate:** synthesis `recommended_action='investigate', confidence=0.66` ⇒ flagged; `0.55` ⇒ NOT
  flagged (watch-only); `'monitor'` ⇒ NOT flagged; `'exit'` + 0.7 ⇒ flagged. (`flagMinConfidence` injected.)
- **Band-only golden (load-bearing — replaces the old draft's "options-unavailable" golden):** a row whose
  synthesis has `streams_available.options=false`, `options_implied_move_pct=null`,
  `stance='indeterminate_no_options'` ⇒ the rendered HTML **and** text contain **NO implied-move column, NO
  implied-move cell, and NOT the string "implied move unavailable"** anywhere (band-only v1); the band badge
  `elevated · 78th pct` renders standing alone; the `streams: … options ✗ …` footnote is the only options
  mention; **no global options tier-note** appears. (This is the inverse of the merged-away draft's golden,
  per Pedro's band-only decision — §1.3.)
- **v1.1 column golden (FUTURE — marked skip/pending until options lands):** with ≥1 row carrying
  `streams_available.options=true` + a numeric `options_implied_move_pct`, the implied-move column renders
  `±X% @ horizon · {stance phrase}`, and a row still missing options renders the literal **"implied move
  unavailable"** in its cell (never blank/`0%`/`—`). Kept as a skipped test so v1.1 has its spec; **not part
  of the v1 exit gate.**
- **`p_crl` never rendered:** assert the rendered body contains no occurrence of any `p_crl` value (and the
  row struct from `bc_digest_rows` has no `p_crl` field — structural §2.1).
- **Field map:** the §1.6 worked example renders the band badge (`elevated · 78th pct`), the `what_changed`
  paragraph, the `watch_items` footer, the drivers list, the `streams:` footnote.
- **Empty digest:** 0 flagged + `digest_send_when_empty=true` ⇒ body with watchlist + "nothing flagged"
  header; subject `… nothing flagged · N watched`.
- **Subject:** `[BC-FDA] {date} — {n_flag} flagged · {n_watch} watched`.

### 8.2 Digest idempotency / send — `supabase/functions/bc-digest/index.test.ts` (fake Resend + fake sb client)
- First invocation inserts a `bc_digest_sends(digest_date,target)` row and POSTs; a **second same-day
  invocation** hits the UNIQUE → 23505 → recipient skipped, **no second POST** (asserted on the fake Resend
  call count).
- A non-2xx Resend response ⇒ row `status='failed'` + `response_body` captured; the run closes `partial`.
- `?force=1` (service-role) ⇒ bypasses dedup (manual resend), logged.
- Recipients: `resolveRecipients` stub returns 2 emails ⇒ 2 sends; empty ⇒ falls back to
  `BC_DIGEST_DEV_RECIPIENTS`.
- **Liveness/finally:** a render exception ⇒ the `bc_pipeline_runs` row still closes `failed` with `reason`.

### 8.3 Outcome labeler — `modal_workers/tests/test_bc_outcome_labeler.py` (fixtures, no network)
- **Regulatory precedence:** a fixture CRL-Transparency match ⇒ `crl`; a Drugs@FDA `AP` ORIG ⇒ `approved`;
  `WD` ⇒ `withdrawn`; a PDUFA-date push ⇒ `extended`. **All lowercase** (CHECK conformance, §0.2).
- **Price math:** fixture daily bars ⇒ base = last close strictly before PDUFA; `t+1/7/30` = Nth trading-day
  close; `(c_tN/base−1)*100` within tolerance. No-ticker app ⇒ price null + `no_ticker` log.
- **Three-row merge / partial:** a verdict-only first run writes 3 rows with null prices; a later run
  **merges** each mature `price_return_pct` without clobbering the verdict (null-omitting upsert);
  `on_conflict=application_number,horizon_days`; re-run idempotent; `extended`→terminal overwrites all three.
- **`scored_p_crl` pairing:** uses the **pre-PDUFA** `bc_rubric_scores` row, not today's.
- **`hypothesis_outcome`:** band `low` + `crl` ⇒ `band_understated_risk`; band `elevated` + `crl` ⇒
  `band_correct_high_risk`; `extended` ⇒ `indeterminate`.
- **Stale-pending sweep:** an app `pdufa_date=today−20` with no outcome ⇒ resolve-attempt; unresolved ⇒
  logged `stale_pending_unresolved` (not dropped, not fabricated, no row delete).
- **CRL-source-absent degradation:** with `openfda_crl_transparency` not importable, the labeler still does
  the approvals path and logs `crl_source_unavailable` (no crash).
- **No refit touched:** assert the labeler never reads/writes `bc_refit_log` or `l7.refit_min_crl_events`.

### 8.4 Pipeline-runs CHECK conformance — folded into 8.2 / 8.3
Assert the digest + labeler close their `bc_pipeline_runs` row with a status in
`{succeeded,failed,partial}` (the deployed CHECK, §0.1) — a regression guard against the stale `ok`/`error`
tokens. A forced mid-run exception ⇒ `failed` + `reason`, row still closed (finally).

### 8.5 Integration — the two Phase-3 exit-gate proofs
- **Digest end-to-end** (`test_bc_digest_e2e` — Deno or a seeded staging branch): seed a `bc_applications`
  row + the matview's base rows (then `REFRESH MATERIALIZED VIEW bc_candidates`) with `risk_band='elevated'`,
  percentile 78, `g3_in_window=true`, `tier='watchlist'`; a `bc_company_tradeable` ticker; a today
  `bc_thesis_updates` row with the §1.6 synthesis (`investigate`, conf 0.66, options dormant/`null`). Invoke
  `bc-digest` with a fake Resend ⇒ one `bc_digest_sends` row/recipient (`sent`); the rendered body has the
  flagged PRTX card with the band badge standing alone (**no implied-move cell, no "implied move unavailable"
  string** — band-only v1) + the watchlist row; a `bc_pipeline_runs('bc_daily_digest')`
  row `succeeded`; a second invocation sends nothing new. **(Maps the high-level gate "a real digest renders
  end-to-end.")**
- **Outcome backfill** (`test_bc_outcome_e2e.py`): seed a resolved app (PDUFA = today−10, a fixture CRL match,
  a pre-PDUFA `bc_rubric_scores` row, fixture price bars) ⇒ run `run_labeler.py` ⇒ assert 3
  `bc_prediction_outcomes` rows (`regulatory_outcome='crl'`, `scored_p_crl` from the pre-PDUFA score,
  `price_return_pct` for mature horizons, `hypothesis_outcome` set), idempotent on re-run, a
  `bc_pipeline_runs('bc_outcome_labeler')` row `succeeded`. **(Maps "resolved catalysts land in
  `bc_prediction_outcomes`.")**

### 8.6 Manual live verification (after Phase 0/1/2 land) — **the brief's "send a real test digest to a single recipient"**
1. Set `BC_DIGEST_DEV_RECIPIENTS=<your-email>` on the `bc-digest` function secrets (overrides the prod pool
   so only you receive it).
2. Invoke the function directly once (bypassing the cron): `curl -X POST
   "$SUPABASE_URL/functions/v1/bc-digest" -H "x-service-key: $SERVICE_ROLE_KEY"` — inspect the rendered email
   in your inbox (band/rank present, **no `p_crl`**, **no implied-move column** (band-only v1), subject
   `[BC-FDA] …`).
3. Confirm exactly one `bc_pipeline_runs(pipeline_name='bc_daily_digest')` row closed `succeeded` and one
   `bc_digest_sends(digest_date=today,target=<you>)` row; **re-invoke** and confirm **no second email** (23505
   dedup) and no new send row.
4. Then enable the cron job (`UPDATE cron.job SET active=true WHERE jobname='bc-digest-daily'`), or leave it
   scheduled and just confirm the next 15 UTC tick fires (check `cron.job_run_details`).
5. Labeler: `curl` the Modal endpoint (or `modal run …::bc_outcome_labeler_once`) against any resolved
   fixture/real app → confirm the `bc_prediction_outcomes` triple + a `succeeded` run row.

---

## 9. RISKS
1. **Options dormant ⇒ the "vs market-implied move" differentiator is absent in v1** (the project's known big
   one). Polygon options is entitlement-gated; per Pedro's 2026-06-03 **band-only** decision the v1 digest
   therefore ships with **no implied-move column at all** (§1.3) — it does not even render an "unavailable"
   placeholder. The product is truthful by omission (the band is the only risk number), but the "risk vs the
   market-implied move" framing the moat narrative leans on is **not present in v1** — **say so to Pedro
   before positioning the v1 digest as that.** v1.1 lights up the column the day Polygon options lands, with
   near-zero render change (the digest already reads `risk_vs_market`/`streams_available`).
2. **`openfda_crl_transparency.py` (the labeler's CRL source) is NOT built yet** (A0 §6, verified absent).
   The `crl` path is blocked until A0 lands it; approvals (Drugs@FDA) work independently. Mitigation: build
   the labeler's drugsfda path first; gate the CRL match behind "if the module imports," log
   `crl_source_unavailable` until then. Hand-off §10.
3. **Matview staleness** — the digest reads `bc_candidates`; if Phase 1's weekly `REFRESH` lagged, bands are
   stale. The digest can't refresh (weekly cadence vs daily digest). Mitigation: the digest reads
   `materialized_at` and renders a "(scores as of …)" note when it's > 8 days old; a stale matview is a
   Phase-1 liveness issue surfaced there, not silently absorbed.
4. **Price-return edge cases** — halted/delisted tickers, post-PDUFA gaps, sponsors that delist after a CRL.
   Mitigation: trading-day counting on returned bars (gaps skipped); a missing bar for a horizon ⇒ that
   horizon's `price_return_pct` stays null + logged, the row still records the verdict.
5. **Edge-fn cold start / timeout on a 20-name batch** — negligible (the read is one `bc_digest_rows` call;
   the work is string-building + ≤N small Resend POSTs). If recipient count grows, the per-recipient POST
   loop is the only O(N) cost; well within edge-fn limits for the design's small pool.
6. **`bc_pipeline_runs.status` CHECK** (`{running,succeeded,failed,partial}`) **contradicts the Phase-2
   sibling docs** (which used `ok`/`error`/`killed_budget`). This plan complies; the Phase-2 docs must be
   reconciled or their runs 23514 (§10).

---

## 10. OPEN DEPENDENCIES / HAND-OFFS
1. **Phase 0/1/2 must land first** — the digest reads `bc_candidates` (Phase 0 universe + Phase 1 scores) and
   `bc_thesis_updates` (Phase 2 synthesis). No upstream ⇒ an empty/zero-row digest (logged `succeeded` with
   `n_watch=0`, not an error).
2. **A0 → `openfda_crl_transparency.py`** (risk 2) — the labeler's CRL outcome source. Coordinate with the A0
   track; the approvals path is independent.
3. **A0 → rank-display prominence** (`bc_v4_a0_rank_confidence_note.md`) — the digest reads A0's
   show-prominently / caveat / de-emphasize decision for low-coverage rows (§1.4); ships with "caveat"
   default until the note lands.
4. **`bc_pipeline_runs` token reconciliation** — Phase-2 docs (synthesis + fetchers) must switch to the
   CHECK-valid set (§0.1). Verify `modal_workers/shared/bc_pipeline_runs.py` (Phase 1's open/close helper)
   emits `succeeded`/`failed`/`partial` — fix there once for all phases.
5. **`bc_candidates` refresh mode — UNRESOLVED draft conflict, Phase 1 to settle.** This draft's introspection
   found **no** unique index on the matview ⇒ plain `REFRESH MATERIALIZED VIEW` (not `CONCURRENTLY`); the
   merged-away draft asserted a UNIQUE index `bc_candidates_appl_uidx` **does** exist ⇒ `CONCURRENTLY` is
   possible. The matview definition is not on disk (live DB is ahead — `supabase_migrations_drift`), so
   neither is byte-verifiable here. **Phase 1 must re-introspect `pg_index` on `bc_candidates` and pick the
   refresh mode accordingly** (see `## Reconciliation notes`). The digest is read-only on the matview, so this
   does **not** block Phase 3. If a future surface needs ticker or `feature_quality` in the matview, Phase 1
   adds them + a unique index (then `CONCURRENTLY` becomes possible and the digest's cik→ticker LATERAL
   collapses).
6. **Resend domain / from-address** — the digest reuses `RESEND_API_KEY` and the `alerts@alerts.solutz.com`
   verified domain (`fanout` already sends from it). `BC_DIGEST_FROM_ADDRESS` defaults to the same; if Pedro
   wants a distinct BC sender domain it must be verified in Resend first (memory `secrets_and_accounts`).
7. **`exit` authoring** — Phase 2 caps model-authored `exit` to `investigate` (its §2.3); the digest treats
   `exit` as flagged regardless (covers an operator-set `exit` via `bc_operator_overrides`). No change unless
   Pedro raises the Phase-2 cap.
8. **Migration 005 (`operator_flags` bc_ sources) is NOT applied — and is NOT a Phase-3 dependency.** Because
   005 has not landed, Phase 3 takes **all** of its liveness from `bc_pipeline_runs` (the uniform primitive):
   the digest and labeler each write a `bc_pipeline_runs` row and write **no** `operator_flags` (the
   stale-pending sweep logs to `bc_pipeline_runs.log`, not a flag). Nothing in Phase 3 blocks on 005 applying.
9. **`bc_digest_sends` + `bc_digest_rows()` + config seeds + the 2 pg_cron jobs** ship in **one disk-first
   migration** (`supabase db push`), NOT MCP `apply_migration` (`feedback_mcp_apply_migration_discipline`).
   Re-introspect the live CHECK/grants after apply (`migration_drift_sweep` discipline).
10. **`internal_config.modal_url_bc_outcome_labeler`** must be set to the deployed labeler endpoint URL
    (mirror `modal_url_earnings_calendar_fetch_daily`); the pg_cron job exits clean if it is empty
    (pre-deploy), so ordering is safe.
11. **The `bc_v4_phase3_digest_outcomes.md` draft has been deleted; this is the single canonical Phase-3
    plan.** That draft built the digest as a Modal worker (with a new Modal Resend secret + a
    `bc_digest_recipients()` RPC + a `dispatch_release_times` hour-15 edit); this canonical doc instead fires
    the digest from a pg_cron-triggered edge fn (no new secret, no RPC, no Modal cron). The draft's
    outcome-labeler section is absorbed here essentially unchanged. Its Modal-digest path is preserved as the
    documented fallback in `## Reconciliation notes` (use it only if Pedro wants all-Modal symmetry).

---

## 11. BUILD ORDER
1. **Migration** (`<ts>_bc_digest_and_outcomes.sql`): `bc_digest_sends` table, `bc_digest_rows(p_day)`
   function, `bc_config` seeds, the two pg_cron jobs (unscheduled/inactive at first, or scheduled with
   exit-clean-on-unconfigured-URL). Disk-first → `supabase db push`; re-introspect grants/CHECK.
2. **Digest render** (`render.ts`) + `render.test.ts` (band-only golden: NO implied-move column + p_crl-never) —
   pure, fixture-testable, no upstream dependency.
3. **Digest fn** (`index.ts`): open `bc_pipeline_runs` → `bc_digest_rows($today)` → flag → render →
   `resolveRecipients` (copied) → `bc_digest_sends` idempotency → Resend POST → close (finally). Add
   `bc-digest` to `supabase/config.toml` with `verify_jwt=false`; deploy. + `index.test.ts` (fake Resend).
4. **Outcome labeler** (Python): `resolve.py` (drugsfda first, CRL gated on import), `price_returns.py`,
   `run_labeler.py` + `test_bc_outcome_labeler.py`. Add `bc_outcome_labeler_once` to `modal_workers/app.py`
   (compute-secret-gated endpoint); deploy; set `internal_config.modal_url_bc_outcome_labeler`.
5. **Activate the two pg_cron jobs** (`UPDATE cron.job SET active=true …`).
6. **Manual live verification** (§8.6, after Phase 0/1/2): real test digest to `BC_DIGEST_DEV_RECIPIENTS`,
   confirm `bc_pipeline_runs` + `bc_digest_sends` + no double-send; then the labeler against a resolved app.

---

## Reconciliation notes

This file is the merge of two Phase-3 drafts — **`bc_v4_phase3_digest.md`** (edge-fn digest; the structural
base for this canonical doc) and **`bc_v4_phase3_digest_outcomes.md`** (Modal-worker digest; now **deleted**).
The two were ~95% identical (same §0 schema facts, same §1 digest design, same §4–§5 outcome-labeler — those
were taken as the UNION of the correct content). They diverged on the points below; each is recorded with the
evidence and the resolution, so the choice is auditable and the discarded path is recoverable.

### RN-1 — Digest transport: **edge fn + pg_cron** (chosen) vs **Modal worker** (the deleted draft) — the load-bearing conflict
- **The disagreement.** The deleted draft built the digest as a **Modal worker** that calls Resend directly,
  resolves recipients via a new `bc_digest_recipients()` SECURITY DEFINER RPC, mints a **new Modal
  `RESEND_API_KEY` secret**, and schedules itself by adding hour 15 to the `dispatch_release_times`
  `@modal.Cron` hour list (its own #5 hand-off risk). This canonical doc builds the digest as a **standalone
  Supabase edge function `bc-digest`**, fired once a day by a **pg_cron `net.http_post` tick**, that reuses
  the edge runtime's existing `RESEND_API_KEY` + copies `fanout`'s `resolveRecipients` verbatim, with its own
  `bc_digest_sends` idempotency table (§3).
- **Why the edge-fn path wins (evidence, verified live 2026-06-03 + spot-checked on disk in this session):**
  - `supabase/functions/fanout/index.ts` confirms the Resend secret (`RESEND_API_KEY` `:103`), the recipient
    resolver (`resolveRecipients` `:281`, `email_on_immediate` `:286`), the inlined Resend POST (`:493`), and
    `escapeHtml` (`:403`) **already live in the edge runtime** — the digest reuses them with no new
    provisioning. The Modal path needed a **new Modal Resend secret** AND a **new recipients RPC** (Modal has
    no `auth.admin.listUsers`).
  - `pg_cron`+`pg_net`+`http` are enabled and the idiom is in-repo —
    `supabase/migrations/20260605000050_earnings_calendar_pg_cron.sql` and
    `20260601173528_ic_memo_backlog_cron_schedule.sql` both exist (spot-checked). So the cron lives in
    Postgres and **does not consume a Modal cron slot** — sidestepping the **Modal 5-cron cap** entirely
    (conan-v2 already has 4 of 5). The Modal path had to wedge hour 15 into `dispatch_release_times`.
  - `supabase/functions/scanner-health/index.ts` exists — the verified standalone-edge-fn pattern
    (service-role client + `x-service-key` gate, `verify_jwt=false`) the `bc-digest` fn copies.
  - **Net:** the edge-fn path removes **two** net-new provisioning items (a Modal Resend secret, a recipients
    RPC) and the only cron-hour-wiring risk, with no downside for a pure JSON renderer. This is exactly the
    direction the merge brief preferred ("standalone bc-digest edge fn fired by a pg_cron tick … prefer that
    if better-evidenced").
- **What is identical either way:** both honor strangle-don't-entangle (Resend **direct**, decoupled from the
  v4 `convergence_assessments`/`bc_thesis_updates` trigger; no 5th `fanout` entry point), both write a
  `bc_pipeline_runs` liveness row, both use a BC-owned `bc_digest_sends` UNIQUE(`digest_date`,`target`) dedup,
  both gate on the same `notifications_prefs.email_on_immediate` pool.
- **Fallback (recoverable).** If Pedro wants all-Modal symmetry for one deploy surface, the deleted draft's
  Modal-digest path still works; it is heavier by exactly the two items above. To rebuild it: a Modal worker
  `modal_workers/bc_monitor/digest/{query,render,recipients,send,run_digest}.py`; a `bc_digest_recipients()
  RETURNS text[]` SECURITY DEFINER RPC (joins `notifications_prefs`→`auth.users.email`) in the migration in
  place of copying `resolveRecipients`; `RESEND_API_KEY`+`BC_DIGEST_FROM_ADDRESS` added to the worker's Modal
  secret set; and the digest scheduled via `public.scanners` (`scheduled_hour_utc=15`) + a tick ≥15 UTC in
  `dispatch_release_times`. **The outcome-labeler is Modal in BOTH drafts** (it needs Polygon + openFDA
  Python) — no conflict there.

### RN-2 — `bc_candidates` matview refresh mode: **no unique index found** (this doc) vs **`bc_candidates_appl_uidx` exists** (deleted draft) — UNRESOLVED, Phase-1 owns it
- The base draft's index introspection found **no** unique index ⇒ Phase 1's refresh must be plain `REFRESH
  MATERIALIZED VIEW` (not `CONCURRENTLY`). The deleted draft asserted a UNIQUE index `bc_candidates_appl_uidx`
  **does** exist ⇒ `REFRESH … CONCURRENTLY` is available. **These are mutually exclusive live-introspection
  claims and cannot be settled from disk** — the matview definition is not in `supabase/migrations/` (verified
  absent this session; the live DB is ahead of disk per `supabase_migrations_drift`).
- **Resolution:** left **unresolved on purpose**, flagged to Phase 1 (§0.3, §10 hand-off #5). Phase 1 must
  re-introspect `pg_index` on `public.bc_candidates` and choose the refresh mode. **This does not block Phase
  3** — the digest is strictly read-only on the matview regardless of how it is refreshed. (This doc keeps the
  conservative "plain REFRESH" assumption in §0.3 so no one assumes `CONCURRENTLY` without verifying.)

### RN-3 — Stale-pending hygiene (§5.6): **never fabricate a verdict** (this doc, kept) vs **write a terminal `extended` if the date merely lapsed** (deleted draft, dropped)
- The deleted draft's §5.6 allowed the labeler, when a past-dated PDUFA could not be resolved, to "write a
  terminal `regulatory_outcome` of best available (`extended` if the date merely lapsed undocumented)." The
  base draft (kept) is stricter and correct per the brief: the sweep **attempts resolution once more, and if
  still unresolvable LOGS `stale_pending_unresolved` to `bc_pipeline_runs.log` — it does NOT fabricate a
  verdict and does NOT delete/mutate `bc_applications`/`bc_application_features`.**
- **Why the strict path wins:** `regulatory_outcome`'s CHECK is `{approved,crl,withdrawn,extended}` and
  `extended` is explicitly the **non-terminal "PDUFA pushed later"** marker (§5.5) — minting it for an
  *undocumented lapse* would corrupt the outcome ledger with a verdict that never happened, poisoning the
  exact evidence base the log exists to protect. Fabrication-free + log-only is the honest, no-destructive
  posture the project mandates.

### RN-4 — `bc_pipeline_runs.status` CHECK tokens — both drafts already corrected; restated for the merge
- Both drafts independently caught that the live CHECK is `{running,succeeded,partial,failed}` (the brief's
  authoritative set), correcting the Phase-2 sibling docs' stale `ok`/`error`/`killed_budget`/
  `skipped_no_entitlement` tokens (which would 23514). This doc carries that correction (§0.1) and the
  cross-phase hand-off to reconcile the Phase-2 docs + the shared `bc_pipeline_runs.py` helper (§10 #4).
  ⚠️ Both drafts assumed `modal_workers/shared/bc_pipeline_runs.py` **already exists** (from Phase 1); a disk
  spot-check this session found it **absent** — so that helper is an *expected Phase-1 deliverable*, not a
  present file. The labeler must not assume it exists; if Phase 1 has not landed it, the labeler open/close
  must emit CHECK-valid tokens directly (or Phase 1 ships the helper first). Same caveat for
  `openfda_crl_transparency.py` (A0 §6) — verified absent, already gated on import (§5.1, risk 2).

### RN-5 — band-only v1 (Pedro, 2026-06-03) — applied across the merged doc
- Neither original draft reflected the band-only decision; both rendered an "implied move unavailable" cell on
  every row + a global "options unavailable on this tier" header note. Per Pedro 2026-06-03 the **v1 digest
  surfaces NO market-implied-move dimension at all** — no column, no per-cell "unavailable" string, no
  tier-note. The band is the only risk number in v1. The synthesis contract still carries the implied-move
  fields, so **v1.1 lights the column up with near-zero render change once Polygon options lands** (noted as a
  v1.1 addition throughout: §1.0, §1.2, §1.3, §1.4, §1.6, §8.1 golden inverted, §9.1). **Outcome LOGGING only
  — no refit loop, no drift alarm** (unchanged from both drafts; restated for the decision).

### RN-6 — minor structural picks (taken from the base draft)
- Read path: a **`bc_digest_rows(p_day date)` SECURITY DEFINER reader** (one round trip; LATERAL joins are
  awkward in PostgREST) — base draft, kept; the deleted draft's `query.py` Modal-side read is the fallback's
  equivalent.
- Recipient resolution: **copy `fanout`'s `resolveRecipients`** into the edge fn (no new RPC) — base draft,
  kept; the deleted draft's `bc_digest_recipients()` RPC is RN-1's fallback.
- File layout (§6): edge-fn TS files under `supabase/functions/bc-digest/` + the Modal labeler under
  `modal_workers/bc_monitor/outcomes/` — base draft. The deleted draft's all-`modal_workers/bc_monitor/digest/`
  layout is RN-1's fallback.
- Everything else (§0 schema facts incl. the lowercase `regulatory_outcome` CHECK + NULLABLE + UNIQUE(`application_number`,`horizon_days`),
  §2 gating, §3.3 idempotency table, §4–§5 outcome contracts, §6.4 config seeds, §7 data-flow) is the union of
  the two drafts' agreeing content.
