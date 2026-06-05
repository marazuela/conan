# BC-FDA Light v4 — Phase 2 detail plan: the **deterministic streams + daily monitor orchestration**

> **CANONICAL doc** (reconciled 2026-06-04 from the two overlapping drafts
> `bc_v4_phase2_monitor_streams.md` + `bc_v4_phase2_fetchers.md`; the latter is now deleted — its unique
> content is folded in here and the conflicts are resolved in **§12 Reconciliation notes**).
>
> Component owner doc. Scope = the **deterministic half** of Phase 2 of
> `~/.claude/plans/plan-the-high-level-peppy-shell.md`: the zero-LLM deterministic streams that write
> `bc_market_signals` + `bc_news_events` for the in-window universe, **plus** the daily Modal orchestration
> that loads the universe, writes all signals, and hands off to the threshold→synthesis path. For **v1
> (Pedro decision 2026-06-03 — §0.0) the monitor runs on the two streams that work today: Form 4 (EDGAR)
> and news/8-K (EDGAR).** The options/IV stream is **v1.1, deferred** (entitlement-gated 403); its design is
> retained in §2 marked as such.
>
> **Sibling doc owns the LLM + gate:** `tasks/bc_v4_phase2_synthesis_contract.md` (the "synthesis-contract
> doc") defines the synthesis JSON contract (`bc_synthesis_v1`), the pure-Python threshold/corroboration
> gate (`bc_monitor/threshold.py`), the Haiku-classify/Sonnet-synthesize calls (`bc_monitor/llm.py`), the
> contract validator/clamp (`bc_monitor/contract.py`), the persist RPCs, and the budget kill. **This doc
> references that interface — it does not re-specify the LLM, the threshold predicates, or the contract.**
> The boundary is the function signature `decide(app) -> Decision` (synthesis-contract §2.5) and the input
> bundle `SynthesisInputs` (synthesis-contract §3.3): my orchestration calls `decide()`, and my streams
> produce the `bc_market_signals` / `bc_news_events` rows whose payload keys `decide()` reads
> (synthesis-contract §2.1).
>
> **Hard project constraints honored throughout:** zero Cowork; **zero LLM in the stream/orchestration
> control flow** (this doc adds no Anthropic import outside the handoff into `bc_monitor/llm.py`);
> **fail-loud** (the daily run writes a `bc_pipeline_runs` row even on crash, per stream **and** per
> orchestration pass); **idempotent** (every write uses the table's natural UNIQUE key with `ON CONFLICT`);
> **trust boundary** — the fetch worker (web egress) is separated from the persist path (the
> `bc_news_event_upsert` RPC as least-priv `bc_scanner`); **fetched text is DATA**, never instructions, and
> is never interpreted by the streams (classification is the synthesis doc's job); **digest-first / minimize
> sources** (two live streams + one deferred; 13F / price-cohort / tiers all cut per the high-level plan).
>
> Investigation basis (read-only, verified 2026-06-03; re-verified live 2026-06-04 in §0/§1): live schema
> on `xvwvwbnxdsjpnealarkh` (column shapes, UNIQUE keys, CHECK constraints, `bc_config` values, the
> `bc_candidates` matview definition, the deployed `bc_news_event_upsert` RPC + `bc_scanner` grants, the
> `documents` table shape, `operator_flags` source CHECK, the **live Polygon entitlement probe** of §2.0);
> the code anchors `modal_workers/scanners/insider_form4_scanner.py`,
> `modal_workers/providers/polygon/{base,market_data,options_data,news_data}.py`,
> `modal_workers/sub_agents/options_microstructure.py`,
> `modal_workers/scanners/fda_signal_bridge.py` (`_build_polygon_providers`),
> `modal_workers/scanners/fda_event_features.py` (straddle consumption shape),
> `modal_workers/fetchers/universe/edgar_8k_pdufa.py`, `modal_workers/shared/edgar_efts.py`,
> `modal_workers/shared/supabase_client.py`, `modal_workers/app.py` (cron topology),
> `orchestrator_runtime/client.py`.

---

## 0. Headline verdict + load-bearing corrections (read this first)

### 0.0 DECISION (Pedro, 2026-06-03): SHIP BAND-ONLY FOR v1

The v1 daily monitor runs on the **two streams that work today**:

| stream | source | v1 status | what it emits |
|---|---|---|---|
| **1. Insider / Form 4** | EDGAR Form 4 XML (free) | **GO — build now** | `bc_market_signals` `signal_type ∈ {insider_cluster_buy, insider_cluster_sell, c_suite_open_market_buy}` (§3) |
| **3. News / 8-K** | EDGAR EFTS 8-K (free); the `documents` table is a *supplement*, not the feed | **GO — build now** | raw `bc_news_events` rows via `bc_news_event_upsert(...)` as `bc_scanner`, capture-only (§4) |
| **2. Options / IV** (the moat input) | Polygon options snapshot | **v1.1, DEFERRED — entitlement-gated 403 (proven live §2.0)** | `options_iv` row **only after a Polygon tier upgrade**; until then `streams_available.options=false`, monitor degrades to **band-only** and `recommended_action` is **capped at `monitor`** (already handled by the synthesis-contract doc) |

**v1 = band + insider + news.** The monitor "degrades to band-only": with no `options_iv` row, the
synthesis-contract sets `streams_available.options=false`, forces `risk_vs_market.stance` to
`indeterminate_no_options`, renders "options data unavailable" honestly, and **caps `recommended_action` at
`monitor`** (synthesis-contract §1.3 / §2.3 / §4.2). This is the honest, fail-loud degradation — not a
silent no-op. **The options/IV stream design is fully retained in §2, marked `v1.1, deferred`**, so the day
the tier is bought it is a **drop-in**: flip `l4.options_enabled=true`, the math (§2.4) is pre-specified +
fixture-tested, the `bc_config` keys are seeded, and `streams_available.options` flips `false`→`true` with
**no synthesis change**. Pedro's options decision is a **$/mo line item, not a code risk** (§2.0–§2.2).

> **Product caveat to carry to Pedro:** the "framed vs market-implied move" edge — the *stated
> differentiator* of the whole monitor — **does not exist in v1** (it is the deferred options stream). Until
> the tier clears, do **not** position the product around "what options are pricing in." Band + insider +
> news is a materially thinner product; resolve the options sub-gate (§2.1) before marketing the moat.

### 0.1 Load-bearing corrections to the briefs (verified live 2026-06-03/06-04 — read before building)

Several "VERIFIED FACTS" in the high-level plan / synthesis-contract doc / the source briefs are **stale or
mutually contradictory**. These are the tie-broken, live-verified facts. **Cite this section in the build
PR.**

1. **The Polygon options *code* EXISTS and is wired; the live *key* is 403 (entitlement-gated).** Both
   source briefs were partly right and neither settled it. `modal_workers/providers/polygon/options_data.py`
   (`PolygonOptionsData`, committed `d22b2f7`, hardened `#103`) is a full provider —
   `get_chain`, `get_iv`, `get_straddle_implied_move`, `get_event_window_liquidity` — already constructed by
   `fda_signal_bridge._build_polygon_providers()` and already consumed by `fda_event_features.py`
   (`implied_move_pct` flows to `market_implied_probability`); `sub_agents/options_microstructure.py` already
   computes `iv_30d/iv_60d/iv_term_slope/straddle_implied_move_pct` from it. **So the unresolved question was
   never code — it is entitlement.** A live probe (§2.0, ephemeral Modal run, `scanner-secrets`, 2026-06-03)
   returned **`403 NOT_AUTHORIZED`** on `/v3/snapshot/options/{T}` for AXSM/PTGX/CRVS while equities +
   reference endpoints returned 200. ⇒ The options stream is a **WIRING + pure-math job gated on a paid
   Polygon tier**, not a from-scratch fetcher build *and* not buildable today. Per §0.0 it is **v1.1,
   deferred**.

2. **`bc_pipeline_runs.status` HAS a CHECK constraint — `{running, succeeded, partial, failed}`** (verified
   live 2026-06-04: `bc_pipeline_runs_status_check = CHECK (status = ANY (ARRAY['running','succeeded',
   'failed','partial']))`). Both source briefs proposed *invalid* tokens: monitor_streams' synthesis-contract
   reference said `ok|partial|killed_budget|error`; the fetchers brief said "no CHECK on status" and used
   `ok|partial|error` + `skipped_no_entitlement`. **All of `ok` / `killed_budget` / `error` /
   `skipped_no_entitlement` would be REJECTED on INSERT.** The allowed mapping (§8.1 is authoritative; the
   synthesis-contract §4.1 must be reconciled to it): `running` → `{succeeded | partial | failed}`;
   budget-kill and uncaught-crash both → `failed`; **no-entitlement options skip → `partial`** (or
   `succeeded` if it's the only stream and everything else is clean) with the 403 detail carried in `reason`
   (text) + `log` (jsonb).

3. **`documents` has NO `entity_id` column, holds only ~424 8-K rows, and is a *supplement* — not the
   authoritative 8-K feed.** The brief's "VERIFIED FACT (a)" (`documents … 8-K … entity_id`) is wrong on the
   column. The CIK/ticker link lives in `documents.extensions` jsonb (keys: `ciks`, `tickers`, `adsh`,
   `file_id`, `items`, `form`, `display_names`, `file_type`). The 17,608 figure is the **whole** `documents`
   table; `source='edgar' AND doc_type='8-K'` is **424** rows. ⇒ the news/8-K stream's **authoritative**
   source is a fresh **EFTS count-by-CIK** pull (§4.1 source 1); `documents` is only a de-duped supplement
   (§4.1 source 2), filtered by `extensions->'ciks'` jsonb containment, **never** a column join.

4. **CIK is the spine; `bc_candidates` IS the universe loop source but exposes NO `ticker`.** `bc_candidates`
   (the matview; verified columns 2026-06-04) selects `application_number, last_scored_at, p_crl, risk_band,
   oof_percentile_rank, refusal_reason, sponsor_cik, appl_type, pdufa_date, days_to_pdufa, market_cap_usd,
   avg_daily_volume_usd, options_chain_exists, borrow_available, g1_active, g1_watchlist, g2_pass,
   g3_in_window, tier, materialized_at` — **`tier IN {refused, gate1_failed, gate2_failed, active,
   watchlist}` IS present; `ticker` is NOT.** The monitor universe = **`bc_candidates` rows with
   `tier IN ('active','watchlist')`** (the G1∩G2∩G3 set). The Polygon (deferred) stream needs a ticker →
   resolve it by joining **`bc_company_tradeable` on `sponsor_cik`** (latest `snapshot_date`); the EDGAR
   streams key on `sponsor_cik` directly (CIK is the EDGAR issuer key). **One CIK → many applications
   (fan-out):** a per-CIK signal **fans out to every in-window `application_number` under that CIK** (§3.2,
   §4.2, §5.2). *(This supersedes the fetchers brief's "iterate `bc_application_features` directly because
   Phase-1 scores are absent" — see §12.A.)*

5. **The deployed `bc_news_event_upsert` RPC + `bc_scanner` role are CAPTURE-ONLY. A separate
   `bc_news_event_classify` RPC is needed for the classify UPDATE — and it is OWNED BY the synthesis-contract
   doc, not this one.** Verified live (2026-06-04): the **only** `bc_*` RPCs that exist are
   `bc_news_event_upsert` and `bc_refresh_candidates`. There is **no** `bc_news_event_classify`,
   **no** `bc_market_signal_upsert`, **no** `bc_pipeline_run_open/close`. `bc_scanner` has **zero** direct
   table grants and **EXECUTE on `bc_news_event_upsert` only**. ⇒ This doc's news stream writes only
   `verdict=NULL` rows via the upsert RPC; the `verdict/topic/confidence` UPDATE goes through a
   `bc_news_event_classify` RPC that the **synthesis-contract doc owns and ships** (§4, §12.C). The
   `bc_market_signal_upsert` + `bc_pipeline_run_open/close` RPCs the fetchers brief proposed are a real,
   not-yet-built design need — carried into §6 (role decision) + §7 (files).

6. **`p_crl` is never displayed downstream.** `bc_candidates.p_crl` exists and is carried through the matview,
   but **no Phase-2/3/4 surface renders it** — the deterministic CRL score is (per the v4 redesign) a *ranking
   input*, demoted from the moat/gate, and the monitor frames against market-implied pricing, not `p_crl`.
   Do not add `p_crl` to any stream payload, synthesis input, or digest field. (Recorded so a later agent
   doesn't "surface the obvious risk score.")

7. **`bc_market_signals.application_number` is an FK → `bc_applications`; all `bc_*` tables are live & empty
   today.** A signal can only be written for an enumerated application. Verified row counts (2026-06-04):
   `bc_applications`, `bc_candidates`, `bc_company_tradeable`, `bc_market_signals`, `bc_news_events` **all 0**.
   ⇒ Phase 2 produces nothing until Phase 0/1 populate `bc_applications` / `bc_application_features` /
   `bc_company_tradeable` / `bc_rubric_scores` and `bc_candidates` has rows. **Build + unit-test against
   fixtures now; the integration gate (§9.5) needs the Phase-0 universe.** This is a hard upstream dependency.

---

## 1. Live-schema facts this plan is pinned to (verified 2026-06-03; re-confirmed 2026-06-04)

Verified via `information_schema` + `pg_attribute` (matviews) + `pg_proc` + `pg_constraint` + `pg_roles` on
`xvwvwbnxdsjpnealarkh`. Do **not** infer from the spec's stale §7 SQL.

**`bc_market_signals`** (the deterministic-stream destination for insider + the deferred options):
| column | type | null | note |
|---|---|---|---|
| `id` | uuid | NO | `gen_random_uuid()` |
| `application_number` | text | NO | **FK → `bc_applications`** |
| `signal_date` | date | NO | the trading date the signal is *as-of* (US/Eastern calendar day) |
| `signal_type` | text | NO | this plan's enum — §1.2 (**no DB CHECK**; the type set is a code convention) |
| `payload` | jsonb | NO | per-stream payload (§3/§4) |
| `computed_at` | timestamptz | NO | `now()` |
- **UNIQUE `(application_number, signal_date, signal_type)`** ⇒ idempotent re-run: at most one row per name
  per day per stream. **`ON CONFLICT (application_number, signal_date, signal_type) DO UPDATE SET
  payload = EXCLUDED.payload, computed_at = now()`** (re-run *refreshes* the snapshot — unlike news/thesis
  which are skip-on-conflict; a same-day re-run should pick up the latest IV/insider state).

**`bc_news_events`** (the news/8-K destination; written via the deployed RPC):
- `(id uuid, application_number text NO [FK], news_id text NO, published_at timestamptz NO, source text NO,
  source_tier text NO default 'low', url text, raw_text text, verdict text, topic text,
  classifier_confidence numeric, classified_at timestamptz, ingested_at timestamptz NO default now())`.
- **UNIQUE `(application_number, news_id)`**. `news_id = md5(source|url|published_at)` is set **by the RPC**
  (the stream never computes the hash itself).
- `source_tier` CHECK ∈ `{primary, secondary, low}`; `verdict` CHECK ∈ `{confirms_thesis,
  contradicts_thesis, neutral_update, requires_review}` (nullable until classify). **This plan's capture
  writes only `verdict=NULL` rows.**
- Note the column is **`ingested_at`** (not `created_at`/`computed_at`).

**`bc_news_event_upsert`** (deployed, `SECURITY DEFINER`, the only news-write path; `bc_scanner` has
EXECUTE — verified): args **`(p_application_number text, p_source text, p_published_at timestamptz, p_url
text, p_raw_text text, p_source_tier text DEFAULT 'low')` RETURNS uuid**. Validates the application exists +
tier, computes `news_id`, `ON CONFLICT (application_number, news_id) DO NOTHING`, returns the row id
(existing or new). Call via `client._rest("POST", "rpc/bc_news_event_upsert", json_body={...})`.
**Capture-only:** the classify UPDATE is a *separate* `bc_news_event_classify` RPC owned by the
synthesis-contract doc (§0.1.5) — it does **not** exist yet.

**`bc_pipeline_runs`** (liveness; **status CHECK = `{running, succeeded, partial, failed}`** — verified
live, see §0.1.2): `(id, pipeline_name, started_at, finished_at, status, snapshot_date, n_processed,
n_failed, cost_usd, log jsonb, reason)`. Every run opens one row (`status='running'`) and closes it in an
**outer try/finally** so a crash still stamps the row (anti-`dispatch_observability_silent_swallow` /
`cowork_session_halt` blind-spot). **Open/close goes through a `bc_pipeline_run_open/close` RPC that does
NOT exist yet** (§6 role decision; or service-role for the structured streams).

**`bc_candidates`** (matview — **the universe loop source**; refreshed by `bc_refresh_candidates()`):
columns (verified) `application_number, last_scored_at, p_crl, risk_band, oof_percentile_rank,
refusal_reason, sponsor_cik, appl_type, pdufa_date, days_to_pdufa, market_cap_usd, avg_daily_volume_usd,
options_chain_exists, borrow_available, g1_active, g1_watchlist, g2_pass, g3_in_window, tier,
materialized_at`. **`tier ∈ {refused, gate1_failed, gate2_failed, active, watchlist}`; NO `ticker`.** The
monitor universe = rows with **`tier IN ('active','watchlist')`**. `days_to_pdufa` / `tier` are computed at
materialization, so the run **refreshes the matview first** (§5.1) or they can be a day stale.

**`bc_company_tradeable`** (CIK→ticker + tradeability): `(sponsor_cik, ticker, snapshot_date,
market_cap_usd, avg_daily_volume_usd, options_chain_exists, borrow_available, …)`, UNIQUE
`(sponsor_cik, snapshot_date)`. `options_chain_exists` is the authoritative "does this name have an options
chain" boolean the monitor reads **before** attempting the (deferred) options stream (saves a Polygon call
on names with no chain).

**`documents`** (8-K **supplement**, not the feed — §0.1.3): `(id, source, source_doc_id,
source_content_hash, url, doc_type, raw_text, title, published_at NO, fetched_at NO, extensions jsonb NO,
…)`. EDGAR 8-Ks: `source='edgar'`, `doc_type='8-K'` (~424 rows total), CIK/ticker in `extensions->'ciks'` /
`extensions->'tickers'`, accession in `extensions->>'adsh'`. **No `entity_id` column.**

**`bc_scanner` role — capture-only (verified):** the role exists; `role_table_grants` returns **zero**
direct table grants; it has EXECUTE on `bc_news_event_upsert` only. ⇒ `bc_scanner` **cannot** INSERT
`bc_market_signals` and **cannot** open `bc_pipeline_runs` rows. Resolution = the §6 RPCs (decision (a)) or
service-role for the structured streams (decision (b)).

**`operator_flags`** source CHECK currently **excludes all `bc_*` sources** (migration 005 NOT applied —
confirmed). Any flag intent (`bc_options_unavailable`, `bc_stream_error`, …) must route to
`bc_pipeline_runs.log` until 005 lands (§4 stream notes, §5.5, cross-cutting in the high-level plan).

**`bc_config`** live keys relevant here: `l3.window_days=120`, `l3.min_market_cap=2.5e8`, `l3.min_adv=2e6`
(universe gates, owned by Phase 0/1), `l4.daily_budget_usd=5`, `l4.max_events_per_candidate_day=40` (owned
by the synthesis layer). **None of the stream tunables exist yet** — this doc seeds the small set in §4.4 +
§2.5 into the **single** config-seed migration the synthesis-contract doc ships (its §2.6), so there is
exactly one config-seed migration.

**RPC / client conventions:** Supabase REST via `SupabaseClient._rest` / `_rest_with_retry(method, path, *,
params, json_body, prefer)`; **no generic `.rpc()` helper** — call RPCs as `_rest("POST", "rpc/<name>",
json_body={...})`. Idempotent table writes use `prefer="resolution=merge-duplicates"` (or
`ignore-duplicates`) + `?on_conflict=<cols>` exactly as `edgar_8k_pdufa._insert_event` /
`insider_form4_scanner` do.

### 1.2 `bc_market_signals.signal_type` enum (this plan owns these tokens)

The synthesis-contract threshold (§2.1/§2.2) reads specific `signal_type`s. **Resolution (pin here,
reconcile the briefs to this):** one `signal_type` per stream-event-shape, chosen so the threshold
predicates read them unchanged:

| stream | `signal_type` value(s) | payload owner | v1 status |
|---|---|---|---|
| insider | `insider_cluster_buy`, `insider_cluster_sell`, `c_suite_open_market_buy` | §3 | **GO** |
| options | `options_iv` | §2 | **v1.1, deferred** |
| news | (writes `bc_news_events`, **not** `bc_market_signals`) | §4 | **GO** |

The high-level plan's informal `'form4'`/`'options'` labels are **not** the operative tokens; the three
insider types + `options_iv` are, because synthesis-contract §2.2's predicates dispatch on exactly those
(keeping the buy/sell/csuite distinction the threshold uses). The scanner already emits these three (+ a
harmless `ten_percent_holder_buy` the threshold ignores). If Pedro prefers a single `form4` rollup it is a
one-line change in both the fetcher and the threshold — but it loses the distinction, so **keep the three**.

---

## 2. STREAM 2 — options / IV (the moat input) — **v1.1, DEFERRED (entitlement-gated 403)**

> **v1.1, deferred — do NOT build for v1.** Per §0.0 the v1 monitor ships band-only and writes **no**
> `options_iv` row; `streams_available.options=false` for every name; the synthesis caps the action at
> `monitor`. This section is the **retained design** so the stream is a drop-in the day a Polygon options
> tier is bought: the entitlement evidence (§2.0), the GO/NO-GO sub-gate + cost options (§2.1–§2.2), the
> snapshot shape (§2.3), the pure math + payload (§2.4), the DoD-needs-prior-row design (§2.4), and the
> degradation contract (§2.6) are all complete and fixture-testable now. **Module (when built):**
> `modal_workers/bc_monitor/streams/options.py` + pure `modal_workers/bc_monitor/options_math.py`.

### 2.0 The hard problem, resolved live (decisive evidence — why this is deferred)

The options *code* exists and is wired (§0.1.1); the unresolved question is **entitlement**. Live probe
(ephemeral Modal run, `scanner-secrets`, 2026-06-03):

```
GET /v3/snapshot/options/AXSM   → 403  {"status":"NOT_AUTHORIZED",
   "message":"You are not entitled to this data. Please upgrade your plan ..."}
GET /v3/snapshot/options/PTGX   → 403   (same)
GET /v3/snapshot/options/CRVS   → 403   (same)
# control (same key, same container):
GET /v2/aggs/ticker/AXSM/prev          → 200   (equities OK)
GET /v3/reference/tickers/AXSM         → 200   (market cap path OK)
GET /v3/reference/options/contracts?underlying_ticker=AXSM → 200   (contract *universe* only)
```

**Verdict — the options snapshot is a hard `403 NOT_AUTHORIZED` on the current Polygon plan.** The key is
valid (equities + reference 200); the options snapshot is simply not in the subscription. The
`/v3/reference/options/contracts` endpoint *is* entitled but returns only strike/expiry/type/CFI — **no IV,
no quotes, no OI** — so it **cannot** compute the straddle-implied move, IV30, term-structure slope, or
unusual volume. There is no free Polygon fallback that yields IV. Hence §0.0's deferral: this is a **$/mo
product decision, not an engineering risk** — the math is written and tested (§2.4); only the feed is
missing. **Do not infer entitlement from the bridge's existence — the bridge degrades silently;** re-probe
with one live `get_chain` call before flipping `l4.options_enabled`.

### 2.1 GO/NO-GO sub-gate (explicit, for Pedro)

> **Options sub-gate:** the "vs market-implied move" edge is **NOT buildable** until Polygon options
> snapshots (or an equivalent IV source) are entitled. Until it clears, Light v4 ships as a **band + insider
> + news** monitor with the implied-move column showing "unavailable" (the synthesis `risk_vs_market`
> degrades to `indeterminate_no_options`). Resolve this gate **before** marketing the monitor as "framed
> against what options are pricing in." On clear: re-probe IV presence on the chosen tier, flip
> `l4.options_enabled=true`, and the stream lights up with no synthesis change.

### 2.2 Alternative IV sources (rough cost, for the decision)

| option | what it gives | rough cost | fit / notes |
|---|---|---|---|
| **Polygon "Options Starter/Developer" tier upgrade** | the same `/v3/snapshot/options/{T}` snapshot (per-contract IV, quotes, OI, greeks) — **drops straight into the existing `PolygonOptionsData`** | **~$29–$199/mo** (Starter≈$29 delayed; Developer/Advanced higher for real-time + greeks) | **Lowest-effort GO.** Zero new provider code — the snapshot shape is already parsed by `options_data.py` + `options_microstructure.py`. Verify the chosen tier returns `implied_volatility` (Starter may be 15-min-delayed + IV-light; Developer is the safe floor). **Recommended path if the gate clears.** |
| **ORATS** (Data API) | clean ATM-interpolated IV30/60/90 term structure + implied move, purpose-built for event vol | **~$199–$600+/mo** | Best *quality* for the exact moat metrics (no interpolation work), but a **new provider** (`providers/orats/…`) + a new payload mapper. More $ and more code than the Polygon upgrade. |
| **CBOE DataShop / LiveVol** | per-contract IV, OI, term structure | **per-dataset / per-call**, can be $$$ | Heaviest integration; overkill for ~20 names. |
| **Free/scraped** (Yahoo options, CBOE delayed) | spotty IV, ToS-gray, no SLA | $0 + ToS risk | **Rejected** — the monitor's premise is trustworthy daily liveness; a scraped IV feed that can silently rot violates fail-loud. |

**Recommendation:** if Pedro greenlights the moat, **upgrade Polygon to the tier that returns options
snapshot IV** (one-call probe to confirm `implied_volatility` is populated before committing) — cheapest
path, **no new fetcher code beyond §2.4's pure math**. ORATS is the fallback if Polygon's IV quality proves
inadequate for term-structure work.

### 2.3 What the snapshot returns when entitled (so the math is ready)

From `options_data.py` + `options_microstructure.py` + the reference probe, each contract in
`/v3/snapshot/options/{underlying}` carries (when entitled): `details.{contract_type, strike_price,
expiration_date}`, `implied_volatility`, `open_interest`, `last_quote.{bid,ask,midpoint}`,
`underlying_asset.price`, and (greeks-enabled tiers) a `greeks` object — sufficient for every §1.2 key. The
snapshot is **point-in-time (today)**; there is **no historical per-contract IV** on this endpoint. This is
the load-bearing constraint for day-over-day metrics → the prior-row design below.

### 2.4 The `options_iv` payload + the pure math (spec, ready to build on GO)

`get_straddle_implied_move` and `get_iv` exist; the **term-structure + interpolation + unusual-volume
primitives do not** — add them as **pure functions** in `modal_workers/bc_monitor/options_math.py` (no I/O;
fixture-tested) that consume a raw chain + spot + the PDUFA date. Two implementation depths:

- **(2a, the cheap proxy — recommended floor)** Use the ATM IV from today's straddle as the "IV30 proxy" and
  compute the DoD delta against **yesterday's persisted `options_iv` payload** (read the prior
  `bc_market_signals` row for `(app, signal_date<today, signal_type='options_iv')`, order desc, limit 1). No
  new endpoint; `bc_market_signals` itself is the IV time-series store. First run for a name has no prior ⇒
  `iv30_dod_pp=null` (a null delta cannot fire `iv30_jump` — correct, no false cold-start signal). **Stamp
  `straddle_expiry`; if it changed vs yesterday, null out `iv30_dod_pp`** (don't compare across different
  expiries — the cheapest guard against the worst false positive).
- **(2b, the cleaner term-structure — full payload)** A √T-interpolated constant-maturity IV30/60/90 + term
  slope, per the table below. Higher precision; an extra chain parse but no extra call. Adopt if (2a)'s
  ATM-at-event proxy proves too noisy in the dry-run (§9.6).

`options_math.py` → §1.2 keys (the 2b mapping; 2a is the subset `iv30 = ATM call/put IV at the event
expiry`, everything else `null`/`false`):

| key | computation (pure, from today's chain) |
|---|---|
| `iv30` | ATM IV interpolated to **30 calendar days**: two expiries bracketing T+30, ATM-strike IV (nearest strike to spot, call+put avg) each, **linear-interpolate in √T** (variance-time). Single-side bracket ⇒ nearest-expiry ATM IV + a payload flag. |
| `iv60`, `iv90` | same interpolation at T+60, T+90 (`null` if no expiry ≥ horizon). |
| `front_back_slope` | `iv_front − iv_back` (front = nearest monthly ATM IV, back = next monthly ATM IV), in IV points. |
| `slope_inverted` | `front_back_slope > l4.slope_inversion_pp` (default 0; tune) — front richer than back is the pre-event tell. |
| `implied_move_pct_pdufa` | **reuse `get_straddle_implied_move(ticker, pdufa_date)`** → `implied_move_pct` (ATM straddle mid / spot at the PDUFA-bracketing expiry). **The headline moat number.** |
| `implied_move_pct_30d` | same straddle math at the T+30-bracketing expiry (fallback horizon when PDUFA is far / unbracketed). |
| `implied_move_horizon` | `'pdufa'` if a PDUFA-expiry straddle was computable, else `'30d'`, else `'unavailable'`. |
| `unusual_volume` | needs per-contract `day.volume` (volume-enabled tiers): `sum(event-window contract day-volume) / trailing-avg`. If the tier lacks day volume ⇒ `unusual_volume=false` + payload note `unusual_volume_unavailable=true` (degrade the *field*, not the row). |
| `iv30_dod_pp` | **(today.iv30 − prior.iv30)·100**, pp; `null` on first-ever day (omit / null, **not** 0), and `null` on an expiry roll. |

ATM-strike selection reuses the `min(..., key=|strike − underlying|)` idiom already in
`get_straddle_implied_move`. **`l4.options_min_liquid_contracts` (5)** guards illiquid chains → too-small
chain ⇒ **no** `options_iv` row + `streams_available.options=false` for *that name* (per-name, not global).
A straddle-reuse **parity test** (§9.2) guards `options_math` against drift vs `get_straddle_implied_move`.

Example payload (full 2b shape; 2a writes the same envelope with the reserved keys null/false):
```jsonc
// bc_market_signals.payload for signal_type='options_iv'
{
  "ticker": "PRTX", "underlying_price": 50.50,
  "straddle_expiry": "2026-07-18",
  "implied_move_pct_pdufa": 14.0, "implied_move_horizon": "pdufa",
  "implied_move_pct_30d": null,
  "iv30": 0.86, "iv30_dod_pp": 7.0,        // dod null on first day / expiry-roll
  "iv60": null, "iv90": null,
  "front_back_slope": null, "slope_inverted": false,
  "unusual_volume": false,                 // + "unusual_volume_unavailable": true if no day-volume
  "options_liquidity_score": 4.0,          // get_event_window_liquidity().liquidity_score
  "call_iv": 0.88, "put_iv": 0.84, "call_strike": 50.0, "put_strike": 50.0,
  "straddle_price": 7.07,
  "provider": "polygon", "source": "polygon_straddle"
}
```

### 2.5 New `bc_config` keys the options stream needs (folded into the synthesis-contract migration)

| key | default | purpose |
|---|---|---|
| `l4.options_enabled` | `false` | master switch; flip to `true` **only** after the Polygon tier is bought + IV-presence re-probed (§2.0/§2.1) |
| `l4.options_min_liquid_contracts` | `5` | per-name chain-size floor (mirrors `MIN_LIQUID_CONTRACTS`) |
| `l4.slope_inversion_pp` | `0` | front−back IV pp above which `slope_inverted=true` (tune after data) |

(Threshold/stance keys — `l4.iv30_dod`, `l4.implied_move_*`, etc. — are owned by synthesis-contract §2.6.)

### 2.6 Graceful degradation (the band-only path the v1 contract depends on)

The fetcher signals "no options today" by **writing no `options_iv` row**. Degradation triggers, in order:
1. **`l4.options_enabled=false` (the v1 default) or `POLYGON_API_KEY` unset or a 403 entitlement preflight**
   ⇒ **no options rows for any name**; the options stream writes its `bc_pipeline_runs` row with
   `status='partial'` (or `succeeded` if it's the lone stream) + `reason` carrying the 403 / disabled
   evidence (**not** the invalid `skipped_no_entitlement` token — §0.1.2), and **does not raise** (expected
   degraded state). Log once to `bc_pipeline_runs.log` (`options_provider_unavailable` / `options_disabled`).
2. `bc_candidates.options_chain_exists=false` for a name ⇒ **skip the Polygon call**, write no row.
3. `get_straddle_implied_move` returns `None` (illiquid `< 5` contracts / no expiry on/after PDUFA / missing
   mid) ⇒ write no row for that name; record `options_illiquid` per-name.
4. Any `PolygonError` / network exc ⇒ catch per-name, record `options_error:<msg>`, continue (one name's
   failure never aborts the run).

In all cases `streams_available.options=false` for the name and the synthesis caps the action at `monitor`.
**No operator flag** for routine illiquidity (expected for many small biotechs); a *fleet-wide* outage (case
1, or >50% hitting case 4) earns a `bc_pipeline_runs.log` summary line `options_degraded_fleetwide` for the
digest.

---

## 3. STREAM 1 — insider / Form 4 (EDGAR) — **GO, build now**

**Module:** `modal_workers/bc_monitor/streams/insider.py`. **Do not modify `insider_form4_scanner.py`** —
it emits into the v3 `signals`/`short_positioning` pipeline (keyed on `issuer_cik`/`ticker`, routed by
`scoring_profile`) with a different envelope. This stream is an **adapter**: reuse the scanner's pure
parse+cluster internals, re-target the output to `bc_market_signals`, key on `application_number`.

### 3.1 Reuse strategy — extract the core, don't fork the scanner's IO

The scanner's value is its **parser + clusterer**, pure and battle-tested:
- `_list_form4_filings(date_from, date_to, …)` — EFTS list of Forms 4/4-A.
- `_primary_doc_url`, `_fetch_primary_doc`, `_parse_form4` — XML → `_Form4Transaction[]` (discretionary P/S
  only; 10b5-1 dropped pre-cluster).
- `_reporter_normalized` (affiliate dedup), the **30d** clustering loop (`CLUSTER_WINDOW_DAYS`),
  `_count_tiers`, `_classify_role`, `MIN_NET_VALUE_USD`.
- It already emits **exactly the three §1.2 signal types** (`insider_cluster_buy/sell`,
  `c_suite_open_market_buy`) + a harmless `ten_percent_holder_buy` the threshold ignores; and a rich
  `raw_payload` (`direction`, `holders[]` with `role`/`officer_title`/`net_value_usd`, `holder_count`,
  `c_suite_count`, `total_value_usd`, `contributing_accessions`, `earliest/latest_txn_date`, `issuer_cik`,
  `tickers`).

**Key difference:** the scanner lists Form 4s **fleet-wide** (every US issuer, ~1.5–3k filings, capped 500)
then clusters. The monitor cares about **~20 known CIKs**. So **invert the query: per universe CIK, fetch
that issuer's Form 4s** — far cheaper and removes the 500-cap truncation risk.

**Resolution (to avoid copy-paste drift): extract the parse+cluster core** out of the v3 scanner into a
shared pure function `modal_workers/scanners/insider_form4_core.py::cluster_form4_for_cik(cik, date_from,
date_to, …) -> list[ClusterView]`, and have **both** the v3 scanner and this stream call it (refactor is
behavior-preserving; covered by the scanner's existing tests + a new **parity test** §9.2). Implementation
paths for the per-CIK list:
- **(3a, recommended)** `edgar_efts.efts_search(query="the", forms="4,4/A", date_from, date_to,
  user_agent=…)` **with a CIK constraint** (`&ciks=<10-digit>`). If the shared `efts_search` doesn't expose
  `ciks`, pass it via an extended params dict / inline a CIK-scoped variant **in `bc_monitor`, not in the
  shared module**. ≤20 calls vs the broad sweep's ~5 pages × 100.
- (3b) Reuse the issuer-submissions JSON (`SUBMISSIONS_URL`, already imported by the scanner) to list an
  issuer's recent 4/4-A accessions. Rejected as primary (more parsing surface); fallback if CIK-scoped EFTS
  proves flaky.

> **Do NOT reuse the scanner's FDA-tracked-ticker reroute** (`insider_form4_scanner.py:~900`): it routes
> clusters to `scoring_profile='binary_catalyst'` keyed on `fda_assets.ticker` — a *different* universe (the
> v3/v4 FDA asset set) emitting v3 `Signal`s. The BC universe is `bc_candidates`, CIK-spined. Keep them
> independent (memory `catalyst_universe_vs_fda_regulatory_events`: parallel ledgers, no silent bridge).

### 3.2 CIK → application_number fan-out

The scanner clusters by `issuer_cik`; the monitor must write `bc_market_signals.application_number`. Build
`cik_to_apps: dict[str, list[str]]` once at run start from the universe (`bc_candidates`, keyed on
`sponsor_cik`). For each CIK that produces a cluster, **write one `bc_market_signals` row per in-window
`application_number` under that CIK** (same payload, different `application_number`). The insider signal is
at the *issuer* level but the universe key is the *application*; a sponsor with two in-window NDAs gets the
cluster attributed to both (synthesis frames each application separately). De-dup is automatic via the
UNIQUE key.

### 3.3 Insider payload contract (what the threshold reads — synthesis-contract §2.1)

For each in-window `application_number` whose `sponsor_cik` has a cluster today, write one row
(`signal_date` = the cluster's `latest_txn_date`, the economic event date) per cluster signal type:

```jsonc
// bc_market_signals.payload for signal_type ∈ {insider_cluster_buy, insider_cluster_sell, c_suite_open_market_buy}
{
  "issuer_cik": "0001234567",
  "direction": "buy",                       // 'buy' | 'sell'
  "cluster": true,                          // holder_count >= 2 (false for solo c_suite_open_market_buy)
  "n_insiders": 3,                          // holder_count (distinct deduped reporters)
  "net_buy_usd_30d": 2100000,               // sum of positive net_value_usd over 30d (0 if sell)
  "net_sell_usd_30d": 0,                    // sum of |negative net_value_usd| over 30d (0 if buy)
  "roles": ["csuite","director_only","director_only"],
  "c_suite_count": 1, "vp_count": 0, "director_only_count": 2, "ten_percent_holder_count": 0,
  "has_10b5_1_only": false,                 // see note
  "earliest_txn_date": "2026-05-20", "latest_txn_date": "2026-06-02",
  "total_value_usd": 2100000,
  "contributing_accessions": ["0001234567-26-000045", "..."],
  "tickers": ["PRTX"],
  "source": "edgar_form4"
}
```

**Required by the threshold (synthesis-contract §2.1):** `net_buy_usd_30d, net_sell_usd_30d, n_insiders,
cluster, direction, roles, has_10b5_1_only`. The rest are additive provenance for digest/audit (harmless;
the `payload` jsonb is schemaless — synthesis reads only the §2.1 keys + the row `id`).
- **Direction ↔ signal_type:** `insider_cluster_buy`/`c_suite_open_market_buy` ⇒ `direction='buy'`;
  `insider_cluster_sell` ⇒ `direction='sell'`. Solo C-suite ⇒ `c_suite_open_market_buy`, `cluster=false`,
  `n_insiders=1`, `roles=['csuite']` (the threshold's `csuite_open_market_buy` fires on the type regardless
  of `cluster`).
- **`has_10b5_1_only`:** the scanner *drops* 10b5-1 txns pre-cluster (`discretionary = [t for t in txns if
  not t.is_10b5_1]`), so a surviving cluster is by construction non-10b5-1 ⇒ this is **always `false`**
  here. **Emit `false` explicitly** (the threshold guards on it, ignoring rows where it's `true`). If a
  cluster is empty post-10b5-1-drop, **write no row**. *(The only place this could become `true` is a future
  variant that keeps 10b5-1 txns to compute it honestly.)*
- **`net_buy/sell_usd_30d`:** sum the scanner's signed per-holder `net_value_usd` into the matching
  non-negative bucket so the threshold's directional predicates read a clean number.

### 3.4 Cost / rate

Per CIK: 1 EFTS list + N primary-doc fetches (N = that issuer's 4/4-A count in the window, typically 0–10).
~20 CIKs ⇒ ~20–200 SEC requests/day, well under SEC's 10 req/s ceiling using the shared `_rate_limiter`
(import via `insider_form4_scanner`'s already-imported seam). Reuse `SEC_USER_AGENT` (required; the scanner
raises `MissingAuthError` without it — mirror that). **~$0** marginal cost.

---

## 4. STREAM 3 — news / 8-K (EDGAR) — **GO, capture-only via the deployed RPC**

**Module:** `modal_workers/bc_monitor/streams/news.py`. This stream **only captures raw rows** into
`bc_news_events` (`verdict=NULL`); the Haiku classifier fills `verdict/topic/confidence` later via the
`bc_news_event_classify` RPC that the **synthesis-contract doc owns** (§0.1.5). Capture writes go
**exclusively through `bc_news_event_upsert`** as the least-priv `bc_scanner` role — **never** raw
service-role INSERT (preserves the trust boundary the spec closed; fetched filing text is **DATA**, never
acted on by the fetcher beyond the deterministic source-tier label).

### 4.1 Two sources, merged (authoritative + supplement)

1. **Authoritative: fresh EFTS 8-K count-by-CIK** (§0.1.3 — `documents` holds only ~424 8-Ks total and may
   lag, so it is **not** the feed). For each `sponsor_cik`, `edgar_efts.efts_search(query="the", forms="8-K"
   [+ "8-K/A"], date_from=now-window, date_to=now, user_agent=…)` constrained to that CIK (same CIK-scoped
   query as §3.1), then `edgar_efts.fetch_filing_text(file_id, cik, adsh, user_agent=…)` for the
   whitespace-collapsed body. Reuse the `edgar_8k_pdufa.py::fetch()` discovery machinery — multi-query EFTS,
   per-accession dedup, page loop, and **per-query partial-failure tracking** (the 2026-05-19 lesson: a
   partial query plan must not report `ok`/`succeeded`). **Difference from `edgar_8k_pdufa`:** that fetcher
   mines a *specific* PDUFA phrase → `fda_regulatory_events`; the BC news stream is **capture-broad** — pull
   recent 8-Ks regardless of content and capture raw. **No item-code filter in v1** (the classifier decides
   relevance; restricting to items 8.01/7.01/2.02 risks dropping a material 1.01/2.01 — revisit if volume is
   high).
2. **Supplement: the live `documents` table** for any 8-K already ingested for the universe CIKs
   (`source='edgar' AND doc_type='8-K' AND extensions->'ciks' ?| array[<universe ciks>]`, `published_at` in
   window). Cheaper than re-fetching; **de-dup against the EFTS pull by accession** (`extensions->>'adsh'`).

**Optional secondary (free-tier, probe-gated): Polygon news.** `providers/polygon/news_data.py::get_news(
ticker, since=)` hits `/v2/reference/news` — likely entitled (reference-class, like the 200 endpoints). Each
article ⇒ one row, `source='polygon:<publisher>'`, `source_tier='low'`, `url=article_url`, `raw_text=title +
'. ' + description`. **Confirm with a one-call probe at build; if 403, drop silently** (8-K alone satisfies
the GO). **Explicitly deferred for v1** anyway: it is `low`-tier (cannot escalate alone), it is the classic
denial-of-wallet vector (PR storms) the dedup is built for, and the edge is 8-K/regulatory primacy, not news
breadth. Wiring it later is a small addition (same RPC, `source="polygon:<pub>"`, `source_tier="low"`).

Window: **`l4.news_window_days`** (default **7**, the synthesis input window — synthesis-contract §2.1).
Capturing exactly the classify/threshold window avoids ingesting rows the synthesis will never read.

### 4.2 News write contract (the RPC call)

Per 8-K, resolve `application_number`(s) via the same `cik_to_apps` fan-out (§3.2) — one upsert per
(application, filing):

```python
client._rest("POST", "rpc/bc_news_event_upsert", json_body={
    "p_application_number": app_no,
    "p_source": "edgar:8-K",                # short stable source token; accession carried in url
    "p_published_at": filing_file_date_iso, # 8-K file_date (timestamptz) — what news_id hashes on
    "p_url": accession_index_url,           # sec.gov .../<adsh>-index.htm
    "p_raw_text": body_text[:N],            # whitespace-collapsed; cap N = l4.news_raw_text_cap_chars
    "p_source_tier": "primary",             # an SEC 8-K is a PRIMARY-tier source
})  # returns news_id-row uuid; ON CONFLICT(app, news_id) DO NOTHING
```

**Source-tier mapping (drives the synthesis corroboration gate):**
| source | `p_source` | `p_source_tier` |
|---|---|---|
| EDGAR 8-K / 8-K/A | `edgar:8-K` (+ accession in `url`) | **`primary`** (SEC filing — can corroborate an escalation alone, synthesis-contract §2.3) |
| Polygon news (press wire, deferred) | `polygon:<publisher>` | **`low`** (cannot escalate alone) |
| (future) FDA press / Federal Register | `fda:*` / `fedreg:*` | `primary` |

(`bc_news_events.source_tier` CHECK ∈ `{primary, secondary, low}` — honor it; v1 captures only 8-Ks ⇒ all
`primary`.)
- **`p_raw_text` cap N (`l4.news_raw_text_cap_chars`):** cap to a few KB — the classifier only needs lede +
  context; oversized 8-K exhibits (full 10-Q-sized attachments) waste storage + classify tokens. The
  near-dup dedup (synthesis-contract §3.2) hashes the first 200 chars, so the cap doesn't affect dedup.
- **`p_published_at` = the 8-K `file_date`** (EFTS `_source.file_date`), **not** `fetched_at` — it is what
  `news_id` hashes on, so it must be stable across the EFTS-vs-`documents` merge (use the filing date).

### 4.3 Trust boundary (honor it precisely)

- The **fetch worker** does the web egress (EFTS + optional Polygon news) and produces `(application_number,
  source, published_at, url, raw_text, source_tier)` tuples. Fetched text is **DATA**: no "if body contains
  X then escalate," no interpretation as instructions — only the deterministic source-tier label above.
- The **persist** path is `bc_news_event_upsert(...)` called as **`bc_scanner`** (least-priv, capture-only;
  the worker holds the `bc_scanner` credential for the news write, never service-role). The classify UPDATE
  is the synthesis doc's `bc_news_event_classify` RPC.
- **CIK → application fan-out:** one CIK's 8-K maps to every in-window app under that CIK; the RPC's
  `(application_number, news_id)` UNIQUE keeps duplicates idempotent across apps.

### 4.4 New `bc_config` keys this stream adds (folded into the synthesis-contract migration)

Add to the **single** config-seed migration (synthesis-contract §2.6); read via the shared
`bc_monitor/config.py` cached helper with documented-default fallback (missing key ⇒ default + warn, never
silent):

| key | default | purpose |
|---|---|---|
| `l4.news_window_days` | `7` | 8-K capture + classify window (= synthesis input window) |
| `l4.insider_lookback_days` | `14` | EDGAR Form-4 EFTS list window (covers the 2-day filing deadline + buffer) |
| `l4.insider_cluster_window_days` | `30` | net-$ cluster window (mirrors scanner `CLUSTER_WINDOW_DAYS`) |
| `l4.news_raw_text_cap_chars` | `16000` | cap on `p_raw_text` stored per 8-K |

(The threshold/stance keys — `l4.insider_buy_30d`, etc. — are owned by synthesis-contract §2.6, not
duplicated here. The options keys are in §2.5.)

---

## 5. THE DAILY MONITOR ORCHESTRATION

**Module:** `modal_workers/bc_monitor/run_daily.py` — the cron entrypoint. Pure control flow + the stream
calls + the handoff into the synthesis layer. **No Anthropic import here**; the only LLM contact is
`bc_monitor.llm.run_synthesis_pass(...)` (the sibling doc's entry seam).

### 5.1 Run skeleton (fail-loud outer envelope; per-stream + per-pass `bc_pipeline_runs` rows)

```
run_daily(snapshot_date=today) -> dict:
  run_id = bc_pipeline_run_open(pipeline_name='bc_daily_monitor', snapshot_date=today)  # status='running'
  log = {"names": {}, "stream_errors": [], "notes": []}
  n_processed = n_failed = 0
  try:
    bc_refresh_candidates()                         # refresh the matview so days_to_pdufa/tier are current
    universe = load_universe()                      # §5.2 — list[CandidateRow], tier in {active,watchlist}
    cik_to_apps = build_cik_to_apps(universe)       # §3.2/§4.1 fan-out map

    # --- 1. DETERMINISTIC STREAMS (no LLM). Each opens+closes its OWN bc_pipeline_runs row (§5.2). ---
    write_insider_signals(universe, cik_to_apps, log)      # §3 → bc_market_signals  [bc_fetch_insider]
    write_options_signals(universe, log)                  # §2 v1.1-DEFERRED: l4.options_enabled=false ⇒
                                                          #   no rows, status='partial', does NOT raise [bc_fetch_options]
    capture_news(universe, cik_to_apps, log)              # §4 → bc_news_events via RPC, verdict=NULL [bc_fetch_news]

    # --- 2. HANDOFF to the synthesis layer (owns budget, classify, threshold, synth) ---
    synth_result = bc_monitor.llm.run_synthesis_pass(universe, snapshot_date=today, log=log)
    #     per name -> decide(app) (threshold) -> if should_fire: classify new bc_news_events (Haiku) ->
    #         Sonnet synthesize -> validate/clamp (action capped at 'monitor' when options absent) ->
    #         persist bc_thesis_updates. Returns {n_failed, cost_usd, status_hint, ...}.
    n_processed = len(universe)
    n_failed = synth_result["n_failed"]
    status = synth_result["status_hint"]            # 'succeeded' | 'partial' | 'failed' — §8.1
    cost = synth_result["cost_usd"]
  except Exception as e:
    status = 'failed'; reason = f"{type(e).__name__}: {e}"; cost = locals().get('cost', 0.0)
    log["fatal"] = reason
  finally:
    bc_pipeline_run_close(run_id, status=status, n_processed=n_processed, n_failed=n_failed,
                          cost_usd=cost, log=log, reason=locals().get('reason'))   # ALWAYS writes
  return {"run_id": run_id, "status": status, "n_processed": n_processed, "n_failed": n_failed}
```

**Ordering invariant:** streams write **before** the synthesis pass, because `decide()` reads today's
`bc_market_signals` + classified `bc_news_events`. News *capture* (verdict=NULL) is in the stream phase;
news *classification* (verdict filled) is inside `run_synthesis_pass` (metered/LLM, must sit under the
budget) — which is why capture and classify are different modules/RPCs.

### 5.2 Per-stream `bc_pipeline_runs` rows (liveness per stream)

The three streams run **sequentially** (insider → options → news), each opening + closing its **own**
`bc_pipeline_runs` row so liveness is per-stream, plus the outer `bc_daily_monitor` row above:
| `pipeline_name` | meaning | v1 typical status |
|---|---|---|
| `bc_fetch_insider` | Form-4 stream run | `succeeded` / `partial` |
| `bc_fetch_options` | options stream run | **`partial`** (deferred: no rows, 403/disabled in `reason`) — NOT `skipped_no_entitlement` (§0.1.2) |
| `bc_fetch_news` | 8-K/news stream run | `succeeded` / `partial` |
| `bc_daily_monitor` | the orchestration pass (threshold→synth) | `succeeded` / `partial` / `failed` |

A failure in one stream marks **that** stream's row `failed` (or `partial`) and continues to the next
(output-or-throw **per stream**, not per cron) — a Polygon hiccup never blinds the insider stream's
liveness.

### 5.3 Universe load (`load_universe`) — `bc_candidates`, joined for ticker

```sql
SELECT c.application_number, c.sponsor_cik, c.appl_type, c.risk_band, c.oof_percentile_rank,
       c.pdufa_date, c.days_to_pdufa, c.options_chain_exists, c.tier,
       t.ticker
FROM bc_candidates c
LEFT JOIN LATERAL (
  SELECT ticker FROM bc_company_tradeable bt
  WHERE bt.sponsor_cik = c.sponsor_cik ORDER BY snapshot_date DESC LIMIT 1
) t ON true
WHERE c.tier IN ('active','watchlist')
ORDER BY c.days_to_pdufa ASC NULLS LAST;
```
- `ticker` may be NULL (no `bc_company_tradeable` row) ⇒ that name gets **no options stream** (deferred
  anyway) and the EDGAR streams key on `sponsor_cik` (always present). Log `no_ticker`.
- Expected ~15–20 rows. If 0 rows → the run still completes `status='succeeded'`, `n_processed=0`, and a
  `log.notes` entry `empty_universe` (an empty Phase-0 universe is visible, not a silent no-op).
- **`p_crl` is intentionally not selected** (§0.1.6 — never displayed downstream).

### 5.4 `streams_available` (the bridge to the synthesis contract) — presence-check, not a flag

For each name, the per-name availability flags the contract needs
(synthesis-contract §1.1 `provenance.streams_available`) are derived **deterministically as a presence-check
on today's rows** — the synthesis layer computes them when it builds `SynthesisInputs` (it already reads
these tables per `decide()`); the orchestration's job is only to ensure rows are written first:
- `insider` = a `bc_market_signals` row of an insider type exists for `(app, today)`.
- `options` = an `options_iv` row with non-null `implied_move_pct_pdufa` exists for `(app, today)` —
  **always `false` in v1** (deferred); covers no-entitlement and illiquid-chain uniformly.
- `news` = ≥1 `bc_news_events` row for `app` with `published_at` in the `l4.news_window_days` window.

**Documented here so the synthesis author implements `streams_available` as a presence-check, not a flag
lookup** — keeps the stream↔synthesis contract to just the two tables (no third coordination surface). The
contract's options degradation (action cap `monitor`) keys off `streams_available.options=false`.

### 5.5 Idempotency + re-runs

- `bc_market_signals`: `ON CONFLICT (application_number, signal_date, signal_type) DO UPDATE` (refresh).
- `bc_news_events`: RPC `ON CONFLICT (application_number, news_id) DO NOTHING` (capture is append-only; a
  re-run is a no-op for seen news).
- `bc_thesis_updates`: `ON CONFLICT (application_number, update_date) DO NOTHING` (one fire/day — owned by
  the synthesis persist RPC, synthesis-contract §3.4); the §2.5-step-4 `already_fired_today` guard makes a
  same-day re-run skip the Sonnet call entirely (no double spend).
- Net: the cron is **safe to run twice in a day** — signals refresh, news is idempotent, an
  already-synthesized name is not re-billed, and CIK→multi-app fan-out writes N distinct idempotent rows.

### 5.6 Flag routing until migration 005

`operator_flags` rejects `bc_*` sources today (005 not applied). The monitor **pre-flights** the live
`operator_flags` source CHECK once at start (re-introspect, per `migration_drift_sweep` discipline) and, if
`bc_*` sources aren't allowed, routes all flag intents (`options_degraded_fleetwide`, `stream_hard_error`,
budget-kill — the latter owned by the synthesis layer) to `bc_pipeline_runs.log`. Do **not** crash the
monitor for a flag-sink gap. Applying 005 is a cross-cutting prerequisite, not this component's deliverable.

---

## 6. PERSIST PATH + ROLE DECISION (the `bc_scanner` capture-only constraint)

`bc_scanner` has **zero table grants** and EXECUTE on `bc_news_event_upsert` only (§1, verified). So it
**cannot** INSERT `bc_market_signals` (insider/options) nor open `bc_pipeline_runs` rows. Two clean
resolutions (plan **defaults to (a)**):

- **(a, recommended)** Add **two SECURITY DEFINER RPCs** — `bc_market_signal_upsert(p_app, p_date, p_type,
  p_payload jsonb)` and `bc_pipeline_run_open(p_name, p_snapshot_date) RETURNS uuid` +
  `bc_pipeline_run_close(p_id, p_status, p_n_processed, p_n_failed, p_cost, p_log jsonb, p_reason)` — granted
  to `bc_scanner`, mirroring the news RPC's trust-boundary discipline. The **entire fetcher worker runs as
  one least-priv role** behind a uniform RPC boundary (no service-role anywhere in `bc_monitor/streams/`).
  `bc_pipeline_run_close` must validate `p_status ∈ {running,succeeded,partial,failed}` to match the table
  CHECK (§0.1.2). **Neither RPC exists yet** (verified) — they ship in this doc's migration (§7).
- (b) Run the insider + options streams under **service-role** directly. **Rejected for news** (it ingests
  web text — must stay least-priv behind the capture RPC). **Acceptable for insider + options** *if* Pedro
  prefers fewer RPCs (they consume only structured EDGAR/Polygon data, no free-text ingestion) — but the
  plan still recommends (a) for a uniform least-priv worker + single trust boundary. **Resolve before
  building persist.**

---

## 7. CRON TOPOLOGY — where this runs (Modal's 5-cron cap is the binding constraint)

`modal_workers/app.py` is **at/near Modal's documented 5-cron workspace cap** (the comments call this out
repeatedly: `dispatch_3h`, `dispatch_release_times`, `dispatch_weekly`, `dispatch_observability`, +
folded-in price tracker/reporting). **Adding a new `@modal.Cron` will block deploy.** Sequencing across the
BC crons (Phase 0 owns its own cron; this doc owns the monitor):

1. **`bc_universe_pdufa`** (Phase 0) — builds `bc_applications`/`bc_application_features`/
   `bc_company_tradeable`. **~11 UTC** (after US 8-Ks settle), per Phase 0.
2. **`bc_daily_monitor`** (THIS doc) — loads the universe, runs the streams, hands to synthesis. **Runs
   after Phase 0.**

**Placement (avoid a new cron):**
- **(7a, recommended) Fold the monitor into `dispatch_release_times`** at a dedicated US-morning hour. Add a
  `bc_daily_monitor` spawn in the hour-routed block (mirror `_SCANNERS_SECONDARY_HOUR` / `_FETCHERS_AT_HOUR`,
  which already use the **13 UTC** US pre-open bucket): `me.bc_daily_monitor.spawn()`. The monitor function
  is a normal `@app.function` named `bc_daily_monitor`, with `secrets=[scanner_secrets, supabase_secrets,
  anthropic_secrets]` and a generous timeout (the synthesis pass over ~20 names can take minutes; ~600s).
  **Timing note (matters once options is live in v1.1):** the straddle wants a live (post-open) chain — a
  15–17 UTC tick is better than 13 UTC for IV freshness. For **v1 (band-only) 13 UTC is fine** (no options).
  When options lands, either add a 17 UTC tick to `dispatch_release_times`' hour list (a schedule-string
  edit, **not** a new cron) or accept 13 UTC + document that the straddle uses the prior session's settled
  chain (acceptable — PDUFA straddles move slowly).
- (7b) Replace/retire a lower-value cron for a dedicated `@modal.Cron("0 17 * * *") bc_daily_monitor`.
  Cleaner schedule but spends the scarce slot; only if (7a)'s spawn-coupling is undesirable. **Default 7a.**

**New secret (a deliberate first):** the monitor needs Anthropic creds for the handoff. app.py today
**intentionally does not attach `anthropic-secrets`** (comment: "anthropic-secrets intentionally NOT
referenced here" — thesis_writer/candidate_aging are Cowork). The BC monitor **breaks that assumption
deliberately** (it is the metered-worker replacement for Cowork): add `anthropic_secrets =
modal.Secret.from_name("anthropic-secrets")` and attach it to **`bc_daily_monitor` only**. **Flag to the
lead:** first Modal function to call Anthropic directly, by design (high-level plan: "replace Cowork with
deterministic fetch + metered Haiku/Sonnet"). *(If Pedro wants a hard DAG instead of the time-gap,
`bc_daily_monitor` already chains streams→synthesis internally; the Phase-0→monitor edge can become a
`.spawn()`-on-success — noted as an option, not the default.)*

**On-demand entrypoint:** add `bc_daily_monitor_once()` (manual `modal run …::bc_daily_monitor_once`) for
the dry-run warm-up (§9.6) and the integration gate.

**DB registry row** (memory `scanner_registry_vs_db`): INSERT `public.scanners` row for `bc_daily_monitor`
(`cadence='daily'`, `status='operational'`, `scheduled_hour_utc=13`).

---

## 8. FILES TO CREATE / MODIFY

```
modal_workers/bc_monitor/__init__.py                      # (shared with synthesis-contract doc)
modal_workers/bc_monitor/streams/__init__.py
modal_workers/bc_monitor/streams/insider.py               # §3 — Form-4 adapter → bc_market_signals (3 insider types)
modal_workers/bc_monitor/streams/options.py               # §2 — v1.1 DEFERRED: PolygonOptionsData wiring + IV math
modal_workers/bc_monitor/streams/news.py                  # §4 — EFTS 8-K + documents merge → bc_news_event_upsert
modal_workers/bc_monitor/options_math.py                  # §2.4 — PURE: iv30/60/90 interp, term slope, unusual_vol (v1.1)
modal_workers/bc_monitor/universe.py                      # §5.3 load_universe + build_cik_to_apps
modal_workers/bc_monitor/run_daily.py                     # §5 — the daily cron entrypoint (no anthropic import)
modal_workers/bc_monitor/persist.py                       # bc_pipeline_run_open/close wrappers; bc_market_signals upsert
                                                          #   (shared w/ synthesis-contract; thesis/classify RPCs live there)
modal_workers/scanners/insider_form4_core.py              # §3.1 — EXTRACTED pure parse+cluster core (shared w/ v3 scanner)
modal_workers/app.py                                      # MODIFY: add bc_daily_monitor + _once fn; attach anthropic
                                                          #   secret; spawn from dispatch_release_times (no new cron)
supabase/migrations/<ts>_bc_monitor_rpcs_and_config.sql   # THIS doc: bc_market_signal_upsert + bc_pipeline_run_open/close
                                                          #   (SECURITY DEFINER, grant bc_scanner; status CHECK-validated),
                                                          #   + the §2.5/§4.4 config keys ADDED to the synthesis-contract's
                                                          #   single config-seed migration. DISK-FIRST then `supabase db push`
                                                          #   (NOT MCP apply_migration — feedback_mcp_apply_migration_discipline).
public.scanners (DB row, not code)                        # INSERT bc_daily_monitor: cadence='daily',
                                                          #   status='operational', scheduled_hour_utc=13 (scanner_registry_vs_db)
```
Reuse (do **not** modify): `modal_workers/scanners/insider_form4_scanner.py` (→ extract core to
`insider_form4_core.py`; the v3 scanner then calls the shared core too),
`modal_workers/providers/polygon/{base,market_data,options_data,news_data}.py`,
`modal_workers/sub_agents/options_microstructure.py` (IV-math reference),
`modal_workers/shared/edgar_efts.py` (extend with a CIK-scoped variant **in bc_monitor**, not in the shared
module), `modal_workers/scanners/fda_signal_bridge.py` (`_build_polygon_providers` as the construction
pattern), `modal_workers/fetchers/universe/edgar_8k_pdufa.py` (discovery/dedup/partial-failure idiom),
`modal_workers/shared/supabase_client.py` (`_rest_with_retry`, `on_conflict` upserts),
`orchestrator_runtime/client.py` (via the synthesis layer), the deployed `bc_news_event_upsert` RPC.

**Shared-module note:** `bc_monitor/__init__.py`, `config.py`, `persist.py` and the **single** config-seed
migration are referenced by BOTH this doc and the synthesis-contract doc. To avoid a two-PR collision, land
the stream PR **after** (or merged with) the synthesis-contract PR, or agree the shared files up front.
`persist.py` ownership: synthesis-contract owns the thesis/classify/`bc_news_event_classify`/failed RPCs;
this doc owns `bc_pipeline_run_open/close` + the `bc_market_signal_upsert` helper. Both small, no conflict if
split by function. Put **all** config seeds in the single synthesis-contract migration.

---

## 9. TEST PLAN

Tests under `modal_workers/tests/` (pytest; convention confirmed — `test_edgar_8k_pdufa.py`,
`test_fda_event_features.py`, `test_polygon_providers.py`, etc. exist). **No live Anthropic/Polygon/SEC
calls** — fakes/fixtures only. The synthesis-contract doc owns `test_bc_threshold.py`, `test_bc_contract.py`,
`test_bc_llm.py`; this doc adds the stream + orchestration + options-math tests.

### 9.1 Unit — insider stream — `test_bc_insider_stream.py`
- Feed a fixture XML (reuse `test_insider_form4` fixtures if present, else a minimal Form-4) for a CIK in a
  2-CIK universe ⇒ a `insider_cluster_buy` row with **exactly** the §2.1 keys + correct `net_buy_usd_30d`,
  `n_insiders`, `roles`, `cluster=true`, `has_10b5_1_only=false`, `signal_date=latest_txn_date`.
- **CIK→application fan-out:** a CIK mapped to **two** in-window applications ⇒ **two** `bc_market_signals`
  rows (same payload, different `application_number`).
- All-10b5-1 cluster ⇒ **no row** (post-filter empty). Solo C-suite ⇒
  `signal_type='c_suite_open_market_buy'`, `cluster=false`, `roles=['csuite']`. Sell cluster ⇒
  `insider_cluster_sell`, `net_sell_usd_30d>0`.

### 9.2 Unit — options stream + math (v1.1) — `test_bc_options_stream.py`, `test_bc_options_math.py`
*(Built dormant; runs against fixtures, never live.)*
- **Math (pure):** `iv30` √T-interpolation brackets T+30 → exact weighted result; single-side bracket →
  nearest-expiry fallback + flag; `iv60`/`iv90` null when no expiry ≥ horizon; term slope + `slope_inverted`
  at the configured pp cutoff; `unusual_volume=false` + `unusual_volume_unavailable=true` when no day-volume.
- **Straddle-reuse parity:** `options_math` implied-move == `get_straddle_implied_move` on the same fixture
  (guards drift between new code and the existing method).
- **`iv30_dod_pp`:** seed yesterday's `options_iv` row (same `straddle_expiry`) ⇒ delta computed; change
  `straddle_expiry` ⇒ `iv30_dod_pp=null` (expiry-roll guard); no prior ⇒ `null`.
- **`get_straddle_implied_move` returns `None`** (illiquid <5) ⇒ **no row**, `streams_available.options`
  false for the name. `options_chain_exists=false` ⇒ Polygon **not called** (mock got 0 calls).
- **Entitlement degradation** (`test_bc_options_entitlement.py`): fake a 403 on the preflight ⇒ the options
  stream writes a `bc_fetch_options` row with **`status='partial'`** + the 403 in `reason` (NOT
  `skipped_no_entitlement` — §0.1.2 regression guard), writes **no** `options_iv` rows, **does not raise**.
  With `l4.options_enabled=false` ⇒ doesn't even probe.

### 9.3 Unit — news stream — `test_bc_news_stream.py`
- Fake EFTS hits + `fetch_filing_text` ⇒ one `rpc/bc_news_event_upsert` call per (application, filing) with
  `p_source='edgar:8-K'`, `p_source_tier='primary'`, `p_published_at=file_date`, raw_text capped at
  `l4.news_raw_text_cap_chars`. (Polygon-news rows, if wired, carry `source_tier='low'`.)
- Merge de-dup: the same accession in both EFTS and `documents` ⇒ exactly **one** upsert call.
- RPC returns the same uuid twice (ON CONFLICT) ⇒ no error, idempotent.

### 9.4 Unit — orchestration — `test_bc_run_daily.py`
- Empty universe ⇒ `status='succeeded'`, `n_processed=0`, `log.notes` has `empty_universe`, **and a
  `bc_pipeline_runs` row exists**.
- A stream raising mid-run ⇒ run still closes with a `bc_pipeline_runs` row (finally-block); status reflects
  the failure (`partial` if isolated per-name, `failed` if fatal). **The load-bearing fail-loud test.**
  Per-stream isolation: forcing one stream's failure still runs the other two.
- `bc_pipeline_runs.status` is always one of the **4 CHECK-allowed values** — assert the budget-kill path
  writes `failed` (not `killed_budget`) and the options-skip writes `partial` (not `skipped_no_entitlement`):
  the §0.1.2/§8.1 regression guard that would have caught both source briefs' bugs.
- Idempotent re-run: run twice ⇒ `bc_market_signals` refreshed (not duplicated), news upserts no-op.

### 9.5 Integration — seeded-delta end-to-end (the Phase-2 exit-gate proof) — `test_bc_monitor_seeded_delta.py`
The **shared** gate with synthesis-contract §6.4 (one test, both halves; meets the fetcher half at
`bc_market_signals` + `bc_news_events`). Against a transactional fixture: seed `bc_applications` +
`bc_application_features` (pdufa ~41d) + `bc_company_tradeable` (ticker, `options_chain_exists=true`) +
`bc_rubric_scores` (`risk_band='elevated'`, pct 78) → `bc_refresh_candidates()` → assert the name is
`tier='active'`. Then run `run_daily` with **options disabled (v1 default)**, **fake** EFTS (one Form-4
cluster net +$2.1M; one manufacturing-buildout 8-K), and **fake** Anthropic (synthesis-contract §1.4
example). Assert:
- exactly one `insider_cluster_buy` `bc_market_signals` row (valid §2.1 payload) + ≥1 `bc_news_events` row
  (tier `primary`); **no `options_iv` row** (deferred);
- `bc_fetch_insider` + `bc_fetch_news` rows `status='succeeded'`; `bc_fetch_options` row `status='partial'`
  (options disabled);
- exactly **one** `bc_thesis_updates` row for `(app, today)`, `synthesis` validates against
  `bc_synthesis_v1.json`, `streams_available.options=false` in the persisted `provenance`,
  `risk_vs_market.stance='indeterminate_no_options'`, **`recommended_action='monitor'`** (band-only ceiling
  held);
- one outer `bc_daily_monitor` `bc_pipeline_runs` row, `status='succeeded'`, `cost_usd>0`.
- **v1.1 arm (skip unless options entitled):** with `l4.options_enabled=true` + fake Polygon
  (`implied_move_pct=14`) ⇒ one `options_iv` row, `streams_available.options=true`,
  `stance='market_underpricing_risk'` referencing ±14%, action still `monitor` (positive-only corroboration
  ceiling).

### 9.6 Dry-run / warm-up (ops, not CI)
Once the Phase-0 universe is live, run `bc_daily_monitor_once` with `l4.synthesis_dry_run=true`
(synthesis-contract §2.6/§6.5) for ~7 days to observe real `trigger_reasons` + stream coverage, then tune
the `l4.*` thresholds. The streams run for real (cheap, deterministic, write
`bc_market_signals`/`bc_news_events`); only the Sonnet fire is suppressed. *(When options lands in v1.1, the
DoD series also needs ~2 days to warm before `iv30_dod_pp`/`implied_move_shift` can fire — §2.4.)*

---

## 10. COST

| item | cost |
|---|---|
| Insider (EDGAR Form 4, ~20 CIKs × few filings) | **~$0** (SEC EFTS free; `SEC_USER_AGENT` required) |
| News (EDGAR 8-K, ~20 CIKs) + optional Polygon news (reference tier) | **~$0** |
| Options (v1 — current tier) | **$0 — not run (deferred, 403)** |
| Options (v1.1 — if entitled) | **+$29–$199/mo** Polygon tier upgrade (or ORATS ~$199–$600/mo) — the only non-trivial cost in the monitor, gated on §2.1 |
| Compute (Modal) | negligible (minutes/day, existing image) |
| LLM (Haiku classify + Sonnet synth) | owned by synthesis-contract; bounded by `l4.daily_budget_usd=5` |

Steady-state marginal cost of the **v1 (buildable-now) streams ≈ $0**, consistent with the monitor-first
"near-zero marginal cost" thesis. The options moat is the sole paid line, **opt-in** behind
`l4.options_enabled` + a Pedro decision.

---

## 11. RISKS

1. **Options moat deferred (the big one).** Polygon snapshot = 403 NOT_AUTHORIZED at the current tier
   (proven §2.0); v1 ships band-only. The "vs market-implied move" edge **does not exist until this
   clears** — say so to Pedro before positioning the product around it. *Mitigation:* build the math +
   config dormant so the flip is one switch (§2.1/§2.6); gate on a $/mo tier decision.
2. **Empty universe (hard upstream dep).** All `bc_*` tables are empty; Phase 2 produces nothing until Phase
   0/1 populate them. Buildable + unit-testable now against fixtures; the integration gate (§9.5) + any live
   value wait on Phase 0. *Mitigation:* sequence 0 → 1 → 2; land streams behind the universe.
3. **`bc_pipeline_runs.status` CHECK = `{running,succeeded,partial,failed}`.** Both source briefs proposed
   invalid tokens (`ok`/`killed_budget`/`error`/`skipped_no_entitlement`) that would be rejected on INSERT.
   *Mitigation:* §8.1 mapping is authoritative; the `bc_pipeline_run_close` RPC + a status-enum test (§9.4)
   enforce it; reconcile synthesis-contract §4.1 to it.
4. **`bc_scanner` cannot write `bc_market_signals`/`bc_pipeline_runs`** (verified zero grants; the
   `bc_market_signal_upsert` + `bc_pipeline_run_*` RPCs don't exist yet). *Mitigation:* the §6 RPCs
   (decision (a)); resolve the role decision before building persist.
5. **`documents` is a 424-row supplement, not the 8-K feed.** Treating it as authoritative silently misses
   most filings. *Mitigation:* §4.1 makes the fresh EFTS count-by-CIK authoritative; `documents` only a
   de-duped supplement.
6. **CIK→application fan-out cardinality / double-counted conviction.** A multi-application sponsor
   multiplies signal rows and a prolific 8-K filer multiplies news rows × applications; the same insider
   cluster lights up every app under the sponsor. Intended (synthesis is per-application), flagged so it's
   not mistaken for a bug. *Mitigation:* ~20-name universe bounds cardinality; `l4.max_events_per_candidate_day=40`
   bounds classify cost; news capture is idempotent.
7. **`insider_form4_core` extraction risk.** Pulling the parse+cluster core out of the v3 scanner could
   regress it. *Mitigation:* behavior-preserving refactor; both callers use the shared fn; parity test (§9.2)
   + the scanner's existing suite must stay green.
8. **`iv30_dod_pp` proxy noise + warm-series (v1.1).** ATM-IV-at-event-expiry proxy + DoD delta is coarse
   (expiry rolls, thin chains, stale pre-open snapshots); and the first ~2 post-entitlement days have null
   DoD. *Mitigation:* the expiry-roll null guard (§2.4), post-open run timing (§7), dry-run tuning of
   `l4.iv30_dod`; escalate to constant-maturity IV30 (2b) if still noisy. Null-on-cold-start is correct, not
   a defect.
9. **Modal 5-cron cap + first Anthropic-in-Modal function.** A new cron blocks deploy; attaching
   `anthropic-secrets` is a first for this app (deliberate departure from "LLM only via Cowork").
   *Mitigation:* spawn from `dispatch_release_times` (no new cron, §7a); scope the Anthropic secret to
   `bc_daily_monitor` only; flag the architectural change to Pedro.
10. **Shared-module collision with the synthesis-contract PR** (`bc_monitor/__init__.py`, `config.py`,
    `persist.py`, the single config-seed migration). *Mitigation:* land in one sequence; split `persist.py`
    by function ownership (§8); all config seeds in the one migration.
11. **Insider universe skew.** Form 4 covers only US-listed Section-16 filers — same large/mid-cap skew as
    the 8-K stream. For a ~20-name $250M-floored NDA/BLA universe largely self-correcting, but micro-caps get
    thin coverage. Acceptable for v1; documented.
12. **Polygon news entitlement unconfirmed.** Likely OK (reference-class endpoint returned 200), but probe
    at build; if 403, drop it silently — 8-K alone satisfies the news GO (deferred for v1 regardless, §4.1).
13. **No hard DAG between streams and synthesis across crons.** Within `run_daily` the order is enforced;
    the Phase-0→monitor edge is a time-gap. *Mitigation:* ~minutes runtime for ~20 names; a `.spawn()`-on-
    success DAG is the stricter alternative if Pedro wants it (§7).
14. **Stale-fact propagation.** Several "VERIFIED FACTS" in the briefs were wrong/contradictory (§0.1).
    *Mitigation:* §0.1 is the correction-of-record; the build PR must cite it so the next agent doesn't
    re-introduce the stale options "blocker," the invalid status tokens, the `documents`-as-feed error, or
    the matview-`ticker` assumption.

---

## 12. Reconciliation notes (how the two drafts were merged — conflicts + decisions)

This canonical doc merges `bc_v4_phase2_monitor_streams.md` (the "streams draft" — better overall structure:
corrections block, pinned-schema, per-stream specs, orchestration skeleton, cron topology, failure-mode
tables) with `bc_v4_phase2_fetchers.md` (the "fetchers draft" — superior on the live entitlement probe, the
options-math interpolation table, the IV-source cost comparison, the `insider_form4_core` extraction, the
per-stream `bc_pipeline_runs` rows, and the `bc_scanner` role/RPC analysis). Structure = streams draft;
content = UNION of both, conflicts tie-broken by the authoritative corrections + a live DB re-verification
(2026-06-04). The fetchers draft is deleted.

**A. Universe loop source — DIRECT CONFLICT, resolved to `bc_candidates`.** The streams draft loops
`bc_candidates` (`tier IN ('active','watchlist')`); the fetchers draft argued the matview's `tier` would be
`gate1_failed` at build time (Phase-1 scores absent) and therefore iterated `bc_application_features`
directly. **Resolution: `bc_candidates` is the loop source** (authoritative correction + the high-level
plan). The fetchers draft's concern is a *Phase-0/1 sequencing* fact, not a Phase-2 design change — once
Phase 0/1 populate scores, in-window names surface as `active`/`watchlist` correctly; building Phase 2 to
read a different table would diverge from the synthesis contract's universe. Live-verified: `bc_candidates`
exposes `tier` (and **no `ticker`** → join `bc_company_tradeable` on `sponsor_cik`). §5.3.

**B. `bc_pipeline_runs.status` — DIRECT CONFLICT, resolved by live DB.** Streams draft: CHECK exists,
`{running,succeeded,partial,failed}`, map budget-kill/crash → `failed`. Fetchers draft: "no CHECK," used
`ok|partial|error` + `skipped_no_entitlement`. **Live-verified 2026-06-04:** the CHECK **exists** =
`{running,succeeded,failed,partial}`. The streams draft is correct; **all** of the fetchers draft's tokens
(`ok`, `error`, `skipped_no_entitlement`) are **invalid** and would be rejected on INSERT. The deferred
options skip maps to **`partial`** with the 403 in `reason`. §0.1.2, §5.2, §8.1, §9.4 regression guard.

**C. The classify RPC — clarified per the authoritative correction.** Neither draft named a
`bc_news_event_classify` RPC; both said `bc_news_event_upsert` is capture-only. **Live-verified:** only
`bc_news_event_upsert` + `bc_refresh_candidates` exist among `bc_*` RPCs — **no** classify RPC, **no**
`bc_market_signal_upsert`, **no** `bc_pipeline_run_open/close`. Recorded that the classify UPDATE needs a
separate `bc_news_event_classify` RPC **owned by the synthesis-contract doc** (this doc writes only
`verdict=NULL` rows). §0.1.5, §4.

**D. Options stream framing — UNIFIED to "code exists, key 403, v1.1-DEFERRED."** Streams draft led with
"options is mostly wiring, build it first" (its §0.1/§2 said the moat is *not* blocked). Fetchers draft led
with the **live 403 proof** and "NO-GO at current tier." Both are true at different layers (code vs
entitlement). **Resolution per Pedro's 2026-06-03 decision:** SHIP BAND-ONLY; options is **v1.1, deferred —
entitlement-gated 403**; its full design (the streams draft's wiring + the fetchers draft's math/cost/probe)
is **retained in §2** marked as such, as a one-switch drop-in. The streams draft's "build stream 2 first"
ordering is **reversed** — insider/news are built first; options is dormant. §0.0, §2.

**E. `documents` — UNIFIED.** Both drafts correctly flagged no `entity_id` + ~424 rows + supplement-not-feed;
the streams draft's authoritative-EFTS-pull + `documents`-as-de-duped-supplement is kept; the fetchers
draft's `edgar_8k_pdufa` partial-failure-tracking reuse is folded in. §0.1.3, §4.1.

**F. Insider reuse — fetchers draft's `insider_form4_core` extraction adopted.** The streams draft said
"import the scanner's internals"; the fetchers draft's **extract a shared pure
`insider_form4_core.cluster_form4_for_cik`** (both v3 scanner + this stream call it) is the cleaner
anti-drift resolution and is adopted, with the parity test. §3.1, §8, §9.2.

**G. Per-stream `bc_pipeline_runs` rows — fetchers draft's design adopted, layered onto the streams draft's
outer envelope.** The streams draft had one outer `bc_daily_monitor` row; the fetchers draft had three
per-stream rows (`bc_fetch_insider/options/news`). **Both kept:** three per-stream rows (per-stream
liveness) **plus** the outer `bc_daily_monitor` orchestration row (the finally-block fail-loud guarantee).
§5.2.

**H. Role/persist RPCs — fetchers draft's analysis adopted (default (a)).** The streams draft assumed
`bc_pipeline_run_open/close` helper functions without noting `bc_scanner` can't write the tables. The
fetchers draft's verified "zero grants → need SECURITY DEFINER `bc_market_signal_upsert` +
`bc_pipeline_run_*` RPCs, or service-role for the structured streams" is the load-bearing constraint and is
adopted as §6 (default (a)). §1, §6, §7 (files), §11.4.

**I. Config keys — UNION, single migration.** Streams draft keys (`news_window_days`, `insider_lookback_days`,
`insider_cluster_window_days`, `news_raw_text_cap_chars`, `options_min_liquid_contracts`) ∪ fetchers draft
keys (`options_enabled`, `news_lookback_days`, `slope_inversion_pp`, `news_raw_text_cap`). De-duped:
`news_window_days` == the fetchers' `news_lookback_days` (kept `news_window_days`, default 7);
`news_raw_text_cap_chars` (16000) chosen over the fetchers' `news_raw_text_cap` (20000) — note the cap
value is a minor open choice, 16k is the documented default here. Added `options_enabled` (the v1.1 master
switch) + `slope_inversion_pp`. All folded into the **single** synthesis-contract config-seed migration.
§2.5, §4.4.

**J. Cron hour — UNIFIED to 13 UTC for v1.** Streams draft leaned 15–17 UTC (post-open, for the options
straddle); fetchers draft used 13 UTC (pre-open bucket). Since v1 is band-only (no options), **13 UTC**
(matching `_FETCHERS_AT_HOUR`) is the v1 placement; the 15–17 UTC post-open tick is documented as the v1.1
adjustment when options lands. Both drafts agreed on **spawn from `dispatch_release_times`, no new cron**
(the 5-cron cap), and on attaching `anthropic-secrets` to the monitor function only. §7.

**K. `signal_type` enum — UNIFIED.** Both drafts independently concluded: keep the three insider types +
`options_iv` (not a `form4`/`options` rollup) so the threshold predicates read unchanged. §1.2.

**L. `p_crl` never displayed — added per authoritative correction.** Neither draft addressed it explicitly;
recorded that `bc_candidates.p_crl` is carried but rendered nowhere downstream (CRL score demoted to a
ranking input per the v4 redesign) — not selected into `load_universe`, no payload/digest field. §0.1.6,
§5.3.

---

## 13. Open dependencies / hand-offs (to the Phase-2 lead)

1. **Universe (Phase 0/1)** must be live before the integration gate / any real run. *(Critical path; §11.2.)*
2. **Reconcile the briefs to §0.1:** the options "blocker" is an *entitlement* 403 (code exists) → v1.1
   deferred; `bc_pipeline_runs.status` enum is `{running,succeeded,partial,failed}` (synthesis-contract §4.1
   must adopt §8.1); `documents` has no `entity_id` and is a 424-row supplement, not the feed; `bc_candidates`
   is the loop source with no `ticker`; `p_crl` is never displayed.
3. **Role decision (a)/(b)** for the insider/options/pipeline-run writes (§6) — default (a): the
   `bc_market_signal_upsert` + `bc_pipeline_run_open/close` SECURITY DEFINER RPCs granted to `bc_scanner`
   (status-CHECK-validated). **These RPCs do not exist yet** — they ship in this doc's migration.
4. **`bc_news_event_classify` RPC** (the classify UPDATE) is **owned by the synthesis-contract doc** — confirm
   it ships there; this doc writes only `verdict=NULL` capture rows.
5. **`signal_type` enum**: keep the three insider types + `options_iv` (§1.2). One-line change in both places
   if Pedro wants a rollup.
6. **Migration 005** (operator_flags bc_ sources) before any bc_ flag write; until then flags →
   `bc_pipeline_runs.log` (§5.6).
7. **Cron placement**: default 7a (spawn from `dispatch_release_times`, 13 UTC for v1 / a 15–17 UTC post-open
   tick once options lands) + attach `anthropic-secrets` to `bc_daily_monitor` only — the first metered-LLM
   Modal function, by design. INSERT the `public.scanners` registry row (§7).
8. **Shared `bc_monitor` modules + the single config-seed migration** are co-owned with the synthesis-contract
   doc; coordinate landing order (§8, §11.10).
9. **OPTIONS v1.1 (deferred, when Pedro greenlights the moat):** approve a Polygon options-snapshot tier
   (~$29–$199/mo) or ORATS; **re-probe IV presence** on the chosen tier with one live `get_chain` call; flip
   `l4.options_enabled=true`. Until then the monitor ships options-blind / band-only (honest degradation
   already handled). *(§2.0–§2.2.)*
