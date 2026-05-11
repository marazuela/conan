# FDA scanners — deep audit (2026-05-11)

Per-scanner audit covering every FDA surface in the v3 stack. Grounded in live
Supabase data (30-day window) + code reads. Each scanner gets: purpose, runtime
state, signal/data flow, known issues with severity rankings (P0–P3), and
recommended next actions.

Companion to the 2026-05-11 scanners-layer audit ([audit/findings_2026-05-11.md](audit/findings_2026-05-11.md)
in memory; not present in this worktree). That audit was breadth-first across
all scanners; this one is depth-first on the FDA stack only.

---

## TL;DR — severity board

| Severity | Finding | Where |
|---:|---|---|
| **P0** | `fda_regulatory_events` is backfill-only. 35 rows total, all from 2026-05-04. **No continuous discovery mechanism for new FDA events.** The entire v3 agentic pipeline (auto-enqueue → 3 sub-agent reviews → IC memo → promote-to-thesis) is architecturally dependent on a static one-shot. | F-300 below |
| **P0** | `fda_signal_bridge` is in `mode='shadow'` indefinitely with no path to operational cutover. **Zero rows in `fda_calibration_runs` and `fda_model_versions`** — the calibration pipeline that should gate the cutover has never run. | F-310 below |
| **P1** | `openfda_corpus_ingest` was registered today 2026-05-11 10:29 UTC, **4 days after its migration filename timestamp (20260507120000)**. Has never run on its 06 UTC cron. First scheduled run is 2026-05-12 06 UTC. 5-day backfill gap on v3 RAG corpus. | F-301 below |
| **P1** | Microstructure sub-agent reviews fail with **`egress_blocked` for finance.yahoo.com, nasdaq.com, whalewisdom.com, dataroma.com, fintel.io, interactivebrokers.com**. Cowork allowlist too restrictive for the required citations; needs runtime move to Modal worker or allowlist expansion. | F-311 below |
| **P1** | `fda_agent_reviews` drain serially via Cowork — 30 completed / 64 queued in ~5 hours = ~10 min/review average. **Backlog will take ~10 more hours** to clear at this rate; not a worker bug, but throughput is the binding constraint. | F-312 below |
| **P2** | `pre_phase3_readout_scanner` `entity_hints.country='US'` is hardcoded ([line 641](modal_workers/scanners/pre_phase3_readout_scanner.py:641)). Foreign sponsors (Akeso HK, AriBio KR) get correct ticker/MIC via Phase 2A.3 alias index but country is wrong in signal payload. Downstream attribution OK; signal trace misleading. | F-302 below |
| **P2** | `fda_pdufa_pipeline` integration tests absent. 41 unit tests cover individual classifiers + EOP2; **no end-to-end `scan()` test, no adcom/readout subtype tests, no CRL false-positive test.** Regression risk on phase-ordering. | F-313 below |
| **P2** | `sub_agent_calls` table: 15 rows, all 2026-05-08, then silent. **`ORCH_ENABLE_SUB_AGENTS=0` default** — orchestrator Stage-1 sub-agents are opt-in and not currently firing. Separate path from `fda_agent_reviews`. | F-314 below |
| **P3** | `fda_event_features.BAND_THRESHOLDS_DEFAULT` (immediate≥30, watchlist≥20, archive≥10) is **hardcoded, not versioned in `fda_model_versions`**. Phase-6 calibration changes will invalidate historical scores. | F-315 below |
| **P3** | Catalyst-universe fetchers (`fda_adcomm_pdufa`, `sec_8k_mna`) bypass `scanner_runs` by design but **invisible to `scanner_probe` / `_scanner_liveness_watchdog`**. No observability for fetcher failures. | F-303 below |
| **P3** | `fda_pdufa_pipeline.DISQUALIFIED_TICKERS` (Pedro edits in-place) has no audit trail. Manual-curation drift risk. | F-316 below |

---

## Scanner-by-scanner audit

### 1. `pre_phase3_readout_scanner`

**Purpose:** Detect industry-sponsored Phase-3 trials with primary completion within ~90d. Score via base-rate × patterns × already-approved gate. Signal type: `pre_phase3_readout`.

**Live state (last 30 days):**
- 15 runs · 2 ok / 11 partial / 2 error
- 141 total signals; avg duration 110s, max 259s
- Last run 2026-05-11 17:58 UTC (post-Phase-2A.3 verification)

**Status:** ✅ Recently fixed in this PR ([conan#32](https://github.com/marazuela/conan/pull/32)). F-200 silent gap (2026-04-27 → 2026-05-11) closed. Watchdog added. Phase 2A.3 alias-index integration dropped unresolved warning count 55 → 36 (-35%).

**Open issues:**

#### F-302 (P2) — `entity_hints.country` hardcoded "US"

[pre_phase3_readout_scanner.py:641](modal_workers/scanners/pre_phase3_readout_scanner.py:641) sets `country="US"` regardless of resolved sponsor. With Phase 2A.3, `IssuerMatch` now carries `mic` (Akeso → XHKG, AriBio → XKRX, Camurus → XSTO etc.). Plumbing `country` through `IssuerMatch` and into `entity_hints` would fix the signal payload. Downstream entity attribution is already correct (the entity row has the right country); only the signal raw_payload is wrong.

**Fix:** ~15 LoC. Add `country: Optional[str] = None` to `IssuerMatch`; populate from `entities.country` in `_load_entity_aliases`; pass through in `_build_signal`.

---

### 2. `fda_pdufa_pipeline`

**Purpose:** Detect PDUFA-related catalysts (approvals, CRLs, date changes, advisory committee, clinical readouts, EOP2 / Type B meetings) primarily from SEC 8-Ks + openFDA. Maintains a long-lived watchlist in Supabase Storage.

**Live state (30d):**
- 33 runs · 31 ok / 2 partial / 0 error
- 45 total signals across 6 signal types
- avg 33s, max 83s (well under 240s soft / 300s hard)
- Schedule: daily 13 UTC + secondary 21 UTC (per [_SCANNERS_SECONDARY_HOUR](modal_workers/app.py:740))

**Signal type breakdown:**

| signal_type | n | first | last | avg score | strategic role |
|---|---:|---|---|---:|---|
| eop2_meeting | 19 | 2026-05-11 | 2026-05-11 | 25 | **NEW** (commit `3d363bc`, 2026-05-10); long-fuse (365d to readout) |
| pdufa_watchlist | 15 | 2026-04-21 | 2026-05-08 | 31 | 30–90d window |
| pdufa_approaching | 6 | 2026-04-21 | 2026-05-08 | 32 | 7–30d window |
| pdufa_imminent | 2 | 2026-04-22 | 2026-05-08 | 32 | ≤7d window |
| pdufa_date_advanced | 2 | 2026-04-29 | 2026-05-08 | 37 | Date moved earlier in last 14d (highest scores) |
| fda_decision | 1 | 2026-05-08 | 2026-05-08 | 32 | Approval / CRL / presumed_crl resolution |

**Status:** ✅ Well-engineered. No critical bugs. Wall-clock budget healthy. EOP2 path verified (19 signals on first run, deterministic regex + sentiment + drug extraction).

**Architecture quality:**
- Subtype-keyed `source_content_hash` dedup ([fda_pdufa_pipeline.py:1423](modal_workers/scanners/fda_pdufa_pipeline.py:1423)) → per-subtype convergence buckets, prevents replay duplicates
- CRL detection via EFTS full-text `"complete response letter"` + ±30d PDUFA match window ([fda_pdufa_pipeline.py:469-520](modal_workers/scanners/fda_pdufa_pipeline.py:469))
- Presumed-CRL fallback at T+3d if no openFDA AP in 30d window ([fda_pdufa_pipeline.py:523-573](modal_workers/scanners/fda_pdufa_pipeline.py:523))
- Budget-aware phase ordering: discovery → enrichment → CRL → signal build, each guarded ([:1501, :1560, :1574, :1629, :1674](modal_workers/scanners/fda_pdufa_pipeline.py:1501))

**Open issues:**

#### F-313 (P2) — No end-to-end test of `scan()`

41 unit tests cover classifiers, EOP2 internals, designation modifiers. Gaps:
- No integration test exercising the full `scan(cfg)` pipeline (discovery → enrichment → dedup → signal build → budget exit)
- No adcom_scheduled / clinical_readout subtype tests (Federal Register adapter could break silently)
- No CRL false-positive test (e.g., "complete response letter" in an unrelated 10-Q risk factor)
- No corrupt-watchlist recovery test ([:1491-1495 MIN_EXPECTED_ENTRIES guard](modal_workers/scanners/fda_pdufa_pipeline.py:1491))

#### F-316 (P3) — `DISQUALIFIED_TICKERS` manual curation drift

[fda_pdufa_pipeline.py:100](modal_workers/scanners/fda_pdufa_pipeline.py:100) comment says "Pedro edits this in-place." No audit trail when an entry is added/removed. Recommendation: move to `internal_config` table with a typed `pdufa_disqualified_tickers` key, log changes via standard schema_migration audit.

**Strategic gap (informational):** Detects PDUFA/EOP2/AdCom/Readout. Missing: Type A meetings, EUA pathway, SAE/REMS post-market signals. These are out-of-band data sources (FDA dockets, not SEC) — appropriate Phase 4–5 additions, not blocking.

---

### 3. `fda_signal_bridge`

**Purpose:** Read pending `fda_regulatory_events`, score them through the FDA-event rubric (6 dims: probability + pricing_edge + magnitude + EV + timeline + liquidity), emit signals when score ≥ 30 (Immediate) or 20 (Watchlist). Three-mode progression: `shadow` → `shadow_with_emit` → `operational`.

**Live state (30d):**
- 34 runs every 3h since 2026-05-07 · 32 ok / 1 partial / 1 error
- **0 total signals** (by design — mode='shadow')
- Each run processes the same 32 stale events (`events_pending=32, events_processed=32, events_skipped=0`)
- `scanners.config = {"mode":"shadow", "block_resolution_events":true, "block_immediate_without_market_p":true}`

**Status:** 🔴 **CRITICAL — operating on stale data with no path to cutover.**

#### F-300 (P0) — No upstream writer for `fda_regulatory_events`

`fda_regulatory_events` table has 35 rows total, **all created 2026-05-04** (zero new rows in 7 days). The bridge has been recycling the same 32 events for a week.

Investigation:
- The only writer is [modal_workers/scripts/fda_backfill_watchlist.py:1-212](modal_workers/scripts/fda_backfill_watchlist.py:1) — a **one-shot manual Python script** that transforms `pdufa_watchlist.json` → `fda_assets/fda_regulatory_events/fda_event_evidence`.
- The `auto_seed_fda_asset_from_pre_phase3` trigger ([migration 20260511130528](supabase/migrations/20260519000000_auto_seed_fda_asset_from_pre_phase3.sql)) creates `fda_assets` rows, **not** `fda_regulatory_events` rows.
- No scanner monitors openFDA / Federal Register / FDA AdCom calendar for new regulatory events.
- `fda_pdufa_pipeline` discovers events from SEC 8-Ks but writes to `signals` table, **not** `fda_regulatory_events`. The two pipelines are architecturally disconnected.

**Action paths (need Pedro decision):**
1. **Bridge `fda_pdufa_pipeline` → `fda_regulatory_events`**: add a SQL AFTER INSERT trigger on signals that maps high-confidence PDUFA/CRL signals to `fda_regulatory_events` rows. Lowest-effort, but couples the v2 (signals) and v3 (events) pipelines.
2. **New discovery scanner**: `fda_regulatory_events_discovery` polls openFDA `/drug/drugsfda.json` (new submissions) + Federal Register API (AdCom schedule changes) and INSERTs. Cleanest, but full new scanner build.
3. **Continuous watchlist refresh**: rerun `fda_backfill_watchlist.py` daily via Modal cron. Quick-and-dirty; doesn't solve event-state-change detection.

#### F-310 (P0) — Calibration pipeline never ran

`fda_calibration_runs`: **0 rows ever**. `fda_model_versions`: **0 rows ever**. `fda_shadow_compare`: 35 rows (auto-derived view, not populated by a job).

The cutover criterion documented in [migration 20260505000025:72](supabase/migrations/20260505000025_fda_event_rubric_and_bridge_scanner.sql:72) ("Flip to shadow_with_emit then operational at cutover") presupposes a calibration pass: hold-out brier / recall / realized EV metrics committed to `fda_calibration_runs`, then `fda_model_versions.activated=true`.

**Neither RPC** (`fda_calibration_execute`, `fda_calibration_activate`) **has ever been invoked.** Even if Pedro chose to cut over today, there's no calibration data to gate it on; flipping mode would emit uncalibrated signals to production.

**Action:** Decide whether Phase 6 calibration is a blocker or whether to ship a "best-effort calibration" baseline. Either way, populate `fda_model_versions` with v1 thresholds before any mode flip.

#### F-315 (P3) — Hardcoded band thresholds

[fda_event_features.py:84-88](modal_workers/scanners/fda_event_features.py:84): `BAND_THRESHOLDS_DEFAULT = {immediate: 30, watchlist: 20, archive: 10}`. Comment says "Phase 6 calibration may move these." Currently:
- Not in `fda_model_versions.band_thresholds`
- Not versioned with the rubric_version
- If calibration changes them, historical scores are wrong

**Fix:** Read from `fda_model_versions` at scan start; fall back to defaults if no active version. Coordinate with F-310 calibration pipeline.

---

### 4. `openfda_corpus_ingest`

**Purpose:** Daily 6-UTC ingest of openFDA `/drug/drugsfda.json` + `/drug/label.json` into `documents` table for v3 RAG. Sunday auto-triggers deep mode (180d lookback); weekdays do 30d shallow.

**Live state:**
- Registry created **2026-05-11 10:29:14 UTC** (today, 4 days after migration filename `20260507120000`)
- 2 runs ever, both today (10:30 + 10:36 UTC). Both manual.
- Run 1: 2597 documents written. Run 2: 0 new / 2759 dedup-hit (re-running same window).
- `last_run_signals=0` by design (`emits_signals=false` per config).
- Next scheduled run: **2026-05-12 06 UTC** (tomorrow = Sunday → first deep sweep).

**Status:** 🟠 Newly operational, 5-day backfill gap, first scheduled run pending.

#### F-301 (P1) — 4-day registration delay; 5-day backfill gap

Migration was committed `2026-05-07 23:49` (commit `58f1b8f`). Applied via Supabase MCP today as `20260511102914_add_openfda_corpus_ingest_scanner`. Schema_migrations shows the actual execution timestamp.

Coverage between 2026-05-06 and 2026-05-11 morning was provided by other adapters (Edgar, Federal Register, ClinicalTrials.gov) — `documents` table has 5,671 rows from last 7d, all from other writers until today's manual openFDA runs.

**Net impact:** 5 days of openFDA drug approvals + label revisions were missed during the registration delay. Today's 180d deep sweep (which today's run only got the partial 30d window of) plus tomorrow's Sunday auto-deep will catch corrections.

**Action:** Trigger one explicit `openfda_corpus_ingest_deep` ([app.py:638-645](modal_workers/app.py:638)) manually to backfill the 180d window deliberately. ~10 minutes runtime.

**Architecture quality:**
- Page-until-empty bounded by `MAX_PAGES_HARD_CAP=100` (~10k records/feed/run); fix in commit `58f1b8f` removed the older 5/10 page truncation
- SHA256(raw_text) dedup via `UNIQUE(source, source_content_hash)` ([document_writer.py:67-69](modal_workers/ingestion/document_writer.py:67))
- Risk: if `_format_drugsfda_record_as_text` formatting changes, same record gets new hash → logical duplicate

---

### 5. `fda_adcomm_pdufa` + `sec_8k_mna` (catalyst-universe fetchers)

**Purpose:** Write independent-truth catalyst rows to `catalyst_universe` for coverage_auditor recall measurement. NOT signal-emitting scanners.

**Live state:**
- `catalyst_universe`: 1,791 rows total, **553 in last 7d** (healthy throughput).
- Registered today via `20260511111639_register_universe_fetchers_as_scanners` — first time they appeared in `public.scanners` (with `config.probe_skip_reason='fetcher: duplicate endpoint with …'`).
- `last_run_utc` NULL by design — fetchers bypass `scanner_runs` ([app.py:474-487](modal_workers/app.py:474) `_run_fetcher`).

**Status:** ✅ Working. 553 rows/7d proves the 13 UTC bucket fires them.

**Architecture detail:** Fetchers are dispatched from `_FETCHERS_AT_HOUR = {13: [...]}` ([app.py:725-727](modal_workers/app.py:725)), additive to the registry lookup. They write to `catalyst_universe` directly; no `scanner_runs` row.

#### F-303 (P3) — Fetchers invisible to observability

Because fetchers don't write `scanner_runs`, they're invisible to:
- `_scanner_liveness_watchdog` (my F-200 fix) — I had to add a `tool_path NOT LIKE 'modal_workers/fetchers/universe/%'` exclusion or every fetcher row would false-positive
- `scanner_probe` (the probe endpoint that pings registered scanners)
- `convergence_qa` (which checks signal-emission patterns)

Their only liveness signal is `catalyst_universe` row freshness. If both fetchers silently failed (rate-limit, SEC user-agent rejected, openFDA endpoint changed), the only alarm is "catalyst_universe stopped growing" — which has no operational alert today.

**Fix:** Add a `catalyst_universe_freshness` check to `_scanner_liveness_watchdog`: if no rows for `source_feed='openfda_drugsfda'` in 36h (daily cadence), emit `operator_flags(source=scanner_liveness, kind=fetcher_overdue)`. Same for `edgar_8k_mna_search`. ~30 LoC.

---

### 6. v3 FDA agentic path (auto-seed + agent-review enqueue + IC-memo + promote-to-thesis)

This is the **post-signal agentic layer**, distinct from the scanners that emit signals. Audit covers:

#### 6a. `auto_seed_fda_asset_from_pre_phase3` trigger (migration 20260511130528)

**Trigger contract:** AFTER INSERT on `public.signals` where:
- `scoring_profile='binary_catalyst'`
- `signal_type='pre_phase3_readout'`
- `entity_id IS NOT NULL`
- `raw_payload->'auto_seed_fda_asset'` is present with `ticker + drug_name`

**Status:** ✅ Working. 47 fda_assets created in last 7d, matching pre_phase3 emission rate.

**Edge case:** If pre_phase3 signal has `universe_resolved=false` (entity_id NULL or no ticker hint), the trigger silently no-ops. With Phase 2A.3 alias index, this is mostly resolved for the seeded foreign names; still a silent gap for genuinely unmapped sponsors.

#### 6b. `enqueue_fda_agent_reviews` trigger (migration 20260520000000 / 20260511131845)

**Trigger contract:** AFTER INSERT on `public.fda_regulatory_events` where:
- `event_status='pending'`
- `event_type NOT IN ('approval','crl','presumed_crl','withdrawal')`

→ Inserts 3 rows into `fda_agent_reviews` (medical + regulatory + microstructure).

**Status:** ✅ Working — but **upstream-starved** (F-300). The 96 rows seen today (32×3) came from the migration's backfill statement when applied.

#### 6c. The Cowork drain for `fda_agent_reviews`

**Confirmed working.** 30 of 96 reviews completed today with high-quality structured outputs (citations + safety_concerns + fair_probability_modifier). Examples processed:
- Ionis olezarsen (TRYNGOLZA, sHTG indication) — medical agent confidence 0.82
- Arcutis Zoryve pediatric — medical 0.78
- Lantheus LNTH-2501 PET tracer — medical 0.82
- Achieve cytisinicline (smoking cessation) — medical 0.80
- Pfizer marstacimab pediatric (Hympavzi) — medical 0.72

**Drain mechanism:** Cowork skill (not in `conan-fda-orchestrator-plugin/skills/`; lives in Pedro's Cowork environment). Processes serially at ~10 min/review.

#### F-311 (P1) — Microstructure egress blocked

Both microstructure failures from today have identical error:

> `egress_blocked: cowork web_fetch allowlist denied finance.yahoo.com, nasdaq.com, whalewisdom.com, dataroma.com, fintel.io, interactivebrokers.com — could not obtain ≥3 primary citations required by schema. Re-claim from a runtime with broader egress (e.g., Modal worker) or after allowlist update.`

[conan-fda-orchestrator-plugin/skills/sub_agent_options_microstructure.md](conan-fda-orchestrator-plugin/skills/sub_agent_options_microstructure.md) declares only `mcp__polygon-mcp__*` as allowed-tools — no web_fetch. The error message explicitly recommends moving to a Modal worker runtime or expanding the Cowork allowlist.

**Decision needed:**
- (a) Move microstructure agent to Modal worker (full egress, but loses Cowork's per-skill iteration model). Risk: agentic loop on Modal needs different infra.
- (b) Expand Cowork allowlist to include the 6 sites. Lower risk; trust boundary widens slightly.
- (c) Rebuild microstructure agent to derive citations from Polygon MCP only (already an allowed tool). May lose data fidelity that needs the 3rd-party sites.

#### F-312 (P1) — Agent-review drain throughput

96 enqueued at 13:18:45 UTC; 30 completed at 17:24 UTC (~4h elapsed). Serial drain rate ≈ 10 min/review. 64 queued → ~10 more hours.

Not a bug — agents are doing real research with citations. But for a Phase 3 production system you'd want:
- Per-agent_kind parallelism (3× speedup if running medical, regulatory, microstructure concurrently per event)
- Higher per-event review parallelism (10× speedup if running 10 events concurrently)
- Cost ceiling per review (current agent calls cost ~$0.50–$1.50 each; no cap)

#### 6d. `fda_signal_promote_to_thesis` RPC (migration 20260508081043 / 20260511000000)

**Contract:** Operator-only RPC. Requires:
- A completed `fda_agent_reviews` row with `agent_kind='ic_memo'` and matching event_id
- Event still `event_status='pending'`, not a resolution type
- A non-superseded `convergence_assessments` row for the asset (from orchestrator Stage 10)

→ Inserts a signals row with `signal_id='v3:' + event_id`, `dimensions->>'_provenance'='dashboard_v3_promote'`.

**Status:** ⏸️ Never invoked. No `ic_memo` agent_kind exists in today's 96 enqueued reviews (only medical/regulatory/microstructure). The IC-memo synthesis step is separate from the 3 specialist reviews.

#### F-314 (P2) — Orchestrator sub-agents stalled

`sub_agent_calls`: 15 rows on 2026-05-08, then nothing. `ORCH_ENABLE_SUB_AGENTS=0` default in [orchestrator_app.py:286](orchestrator_runtime/orchestrator_app.py:286) — sub-agents are opt-in per run.

The orchestrator's Stage-1 sub-agents (literature, competitive_landscape, regulatory_history, options_microstructure) are **distinct from** the trigger-enqueued `fda_agent_reviews` (medical, regulatory, microstructure). Two parallel agentic systems with overlapping names. Confusing.

**Recommendation:** Document the relationship explicitly in DECISIONS.md. Decide whether `fda_agent_reviews` outputs should feed `sub_agent_calls.output` for the orchestrator (currently they don't).

---

## End-to-end data flow (corrected)

The earlier audit's flow diagram was wrong. Actual current state:

```
pre_phase3_readout_scanner emits signal
  ↓ (signals table INSERT)
  ↓ AFTER INSERT trigger: auto_seed_fda_asset_from_pre_phase3
fda_assets row created
  ↓ (asset_linker pg_cron every 5 min)
  ↓ Sonnet classifier links documents → asset_documents
  ↓ (fact_extractor pg_cron every 5 min)
  ↓ Sonnet structured-fact extraction → extracted_facts
  ↓
  ✗ DEAD END ← fda_regulatory_events is NOT populated from this path

fda_regulatory_events (35 rows, all 2026-05-04 backfill)
  ↓ AFTER INSERT trigger: enqueue_fda_agent_reviews
3× fda_agent_reviews (medical + regulatory + microstructure) queued
  ↓ Cowork skill drains (serial, ~10 min/review)
30 completed / 64 queued / 2 microstructure failed (egress)
  ↓
  ✗ MISSING ← no ic_memo synthesizer step today

  ↓ (operator clicks "promote to thesis" in dashboard)
  ✗ NEVER CALLED ← fda_signal_promote_to_thesis RPC unused
signals row + thesis_jobs row + operator_actions audit

In parallel:
  ↓ fda_signal_bridge reads pending fda_regulatory_events every 3h
  ↓ Scores via rubric (fda_event profile)
  ↓ mode='shadow' → writes fda_event_features.shadow_* only
  ✗ NEVER EMITS ← shadow mode forever
```

The two big architectural gaps:
1. **No bridge** from the v2 signals pipeline (`pre_phase3` / `fda_pdufa_pipeline`) to the v3 events pipeline (`fda_regulatory_events`).
2. **No path** for `fda_agent_reviews` outputs to influence anything — the 30 high-quality completed reviews sit in the table with no consumer.

---

## Recommended sequencing (Pedro decision)

**This week (P0):**
1. **F-300** — pick an upstream writer for `fda_regulatory_events`. My recommendation: option (1) above — SQL trigger on high-confidence `fda_pdufa_pipeline` signals (`pdufa_imminent` + `pdufa_date_advanced` + `fda_decision` types where score_with_bonus > 30). 1-day build.
2. **F-310** — even a baseline `fda_model_versions` row with the v1 thresholds, just to make the cutover gate non-trivial.

**Next week (P1):**
3. **F-311** — decide microstructure egress path. My preference: option (b), expand Cowork allowlist for the 6 finance sites. Lowest risk.
4. **F-312** — parallelize agent_kind drain (3× per-event speedup) once F-311 is unblocked.
5. **F-301** — manually trigger `openfda_corpus_ingest_deep` to backfill the 180d window.

**Soon (P2):**
6. **F-302** — plumb country through IssuerMatch + entity_hints
7. **F-313** — add end-to-end pipeline test for `fda_pdufa_pipeline`
8. **F-314** — DECISIONS.md entry clarifying `fda_agent_reviews` vs `sub_agent_calls` relationship

**Eventually (P3):**
9. **F-303** — catalyst-universe fetcher freshness in watchdog
10. **F-315** — move band thresholds to `fda_model_versions`
11. **F-316** — `DISQUALIFIED_TICKERS` to internal_config

---

## Coverage map — every FDA surface checked

| Surface | Audit depth | Severity |
|---|---|---:|
| `pre_phase3_readout_scanner` | Full code + 30d data | P2 (F-302) — earlier issues already fixed in this PR |
| `fda_pdufa_pipeline` | Full code + signal type breakdown | P2 (F-313), P3 (F-316) |
| `fda_signal_bridge` | Full code + 30d data | P0 (F-300, F-310), P3 (F-315) |
| `openfda_corpus_ingest` | Full code + registration history | P1 (F-301) |
| `fda_adcomm_pdufa` fetcher | Full code | P3 (F-303 shared) |
| `sec_8k_mna` fetcher | Full code | P3 (F-303 shared) |
| `fda_event_state` | Full code | covered (transform-only helper) |
| `fda_event_features` | Full code | P3 (F-315) |
| `auto_seed_fda_asset_from_pre_phase3` trigger | Migration read | covered |
| `enqueue_fda_agent_reviews` trigger | Migration read | covered (upstream issue is F-300) |
| Cowork drain skill | External; live data verified | P1 (F-311, F-312) |
| `fda_signal_promote_to_thesis` RPC | Migration read | covered (unused, depends on ic_memo) |
| `conan-fda-orchestrator-plugin/mcp_servers/*` | Full read | covered |
| `conan-fda-orchestrator-plugin/skills/*` | All 6 skills listed | P1 (F-311) |
| Orchestrator sub-agents (`sub_agent_calls`) | Live data + flag verified | P2 (F-314) |
| Calibration pipeline (`fda_calibration_runs`, `fda_model_versions`) | Live data verified empty | P0 (F-310) |
