# Light v4 (BC-FDA monitor) — high-level implementation plan

> High-level skeleton only. Each **Phase** below is a self-contained brief meant to be handed to a
> detail-planning agent (a "Detail-agent brief" is included per phase). Per-phase plans expand these.

## Context

`~/Downloads/BC_FDA_TOOL_PRODUCT_SPEC.md` (v1.1) specced a deterministic-first FDA CRL **scoring**
tool (7 layers, 18 tables, 6 sources, full feedback loop). Two things changed that:

1. **The review (2026-06-03)** proved the spec's single go/no-go gate is not executable: the
   `eval_harness` "81 regulatory rows" are quadruply disqualified (all `q1_verdict='discard'`; the 45
   CRLs are 8-K-mining tokens with no FDA application numbers — 26 of them one Axsome event; and the
   cohort sits entirely inside M14's 2018–2025 train/val/test windows). M14's headline AUC 0.810 rests
   on **9 CRLs**; I reproduced it at 0.809 with a bootstrap CI floor of **0.637 < 0.70**.
2. **Pedro's endorsed reframe (`v4_redesign_direction.md`, 2026-06-03):** the spec is over-built. Ship a
   **monitor-first, light v4**. The CRL score is demoted from moat/gate to a **ranking input** (a
   risk-band, not `p_crl=0.18`). The edge is **fast, trustworthy daily monitoring + synthesis on a
   focused universe, framed against what options are already pricing in.**

This plan implements the endorsed direction, not the original spec. The outcome we want: a daily digest
that, for ~20 in-window tradeable NDA/BLA names, shows a risk-rank beside the market-implied move, what
changed today, and the 1–2 names worth a look — at near-zero marginal cost, with no component that can
silently go dark.

**Not greenfield:** the heavy schema is already deployed — **19 `bc_*` tables + `bc_candidates` matview,
live & empty** on `xvwvwbnxdsjpnealarkh` (migrations 001–006). v1 **populates ~6–9** of them and leaves
the rest dormant. (Migration 005 — `operator_flags` bc_ sources — is intentionally **not yet applied**.)

## Guiding principles (every phase inherits these)

- **Monitor-first, score-as-input.** Build the daily monitor; the score is a weekly stub that emits a
  **rank/band**, never a calibrated probability. M14 backtest is **not** a project gate.
- **Digest-first.** The daily email IS the product. Dashboard is drill-down and may lag.
- **Zero Cowork / zero LLM in control flow on the daily path.** Replace Cowork with deterministic fetch
  + metered Haiku/Sonnet (single-host Mac is the top reliability liability). LLM only classifies/synthesizes
  behind a deterministic threshold.
- **Fail-loud.** Every cron writes a `bc_pipeline_runs` row; liveness = "did today's run write its row?"
  — not a watchdog meta-system. Components output-or-throw.
- **Minimize data sources.** Score spine ≈ Drugs@FDA + EDGAR 8-K count; ablate the marginal booleans.
- **Strangle, don't migrate.** Build the light core beside v4 in `bc_*`; disable v4's FDA path only once
  proven on resolved outcomes.

---

## Phase 0 — Pending-universe + PDUFA source  **(GATE — do this first)**

**Why first:** a monitor needs a reliably-enumerated in-window universe = trustworthy PDUFA dates for
**pending** NDA/BLA. The spec leaned on Drugs@FDA, which is an approved-products DB; live corroboration is
weak (`fda_assets.next_catalyst_date` 17/157; no `pdufa` catalyst type in `catalyst_universe`; the existing
`edgar_8k_pdufa.py` deliberately leaves `event_date = NULL`). **No source ⇒ no universe ⇒ no monitor.**

- **Goal:** prove we can produce, daily and reproducibly, the set of pending in-window tradeable NDA/BLA
  names with PDUFA dates + BT/FT/AA designations.
- **Approach (Pedro's call): compare all three head-to-head**, score on coverage / latency / cost:
  1. **EDGAR 8-K extraction** — extend `edgar_8k_pdufa.py` to parse the actual PDUFA date + designations
     from 8-K text (today it only flags the mention).
  2. **Third-party catalyst calendar** — a biopharma PDUFA feed/scrape (BioPharmaCatalyst / RTTNews / Evaluate).
  3. **FDA primary** — Drugs@FDA + Federal Register + AdComm calendars + inference.
- **Deliverable:** a benchmark report + a recommended source, plus a working enumeration that writes
  `bc_applications` (registry) and a `pdufa_date` into `bc_application_features`, and `bc_company_tradeable`
  (Polygon/market-data) for the tradeability filter.
- **Exit gate (GO/NO-GO):** ≥ ~15–20 pending in-window NDA/BLA names enumerated with PDUFA dates,
  cross-checked against a known recent-catalyst set; coverage/latency/cost documented per approach. If no
  approach yields a trustworthy universe cheaply, the monitor-first thesis is reconsidered here.
- **Reuse anchors:** `modal_workers/fetchers/universe/edgar_8k_pdufa.py`, `modal_workers/shared/openfda_client.py`,
  `modal_workers/ingestion/openfda_ingest.py` (`ingest_drugsfda_approvals`), `modal_workers/shared/supabase_client.py`
  (`_rest_with_retry`, ON CONFLICT idempotency); tables `bc_applications`, `bc_application_features`, `bc_company_tradeable`.
- **Risks:** 8-K coverage skews large/mid-cap; calendar ToS/cost; FDA-primary date inference is hard.
- **Detail-agent brief:** Design the three-way PDUFA-source spike: exact endpoints/parsers, the benchmark
  cohort + scoring rubric (coverage/latency/cost), the enumeration → `bc_applications`/`bc_application_features`/
  `bc_company_tradeable` write path, and the GO/NO-GO threshold. Confirm Polygon (or chosen provider) gives
  market cap + ADV for the tradeability filter.

## Phase 1 — Score-as-rank spine (weekly)  + A0 cohort confidence (parallel, non-blocking)

- **Goal:** a weekly cron that scores the Phase-0 universe with M14 and emits a **risk-band/rank** (not a
  probability) to `bc_rubric_scores`. Separately, build the A0 validation cohort to set *how prominently*
  to show the rank — **not a gate**.
- **In scope:** import `score_m14_adjusted.py` as-is; feed it via the existing feature assembler;
  emit `risk_band` + percentile only. **Ablate the feature set** — start from Drugs@FDA + 8-K count and
  test dropping inspections / warning-letters; **drop CT.gov booleans for v1** (whole integrations for one
  boolean each).
- **A0 (parallel):** ingest the **FDA CRL Transparency dump** (`api.fda.gov/download.json` →
  `results.transparency.crl`; 426 CRLs, 100% FDA-keyed, verified) + matched Drugs@FDA approvals; restrict
  to the out-of-sample slice (post-2025-test → ~40–55 clean OOS CRLs); score with M14; compute AUC/Brier
  with bootstrap CI. Output: a one-page **rank-confidence note**.
- **Deliverable:** weekly score cron writing `bc_rubric_scores`; `bc_application_features` populated;
  rank-confidence note.
- **Exit gate:** scores present for the whole universe; band rendering correct; A0 CI documented.
- **Reuse anchors:** `BC_scoring_rubrics_export/NDA_M14_adjusted/scripts/score_m14_adjusted.py` (import,
  pure stdlib); `modal_workers/shared/fda_crl/feature_assembly.py` (`assemble_nda_features` — already
  computes n_prior_filings, n_drug_inspections_5y, n_8ks_30_180, sponsor_has_warning, BT/FT/AA, **point-in-time
  via `ref_date`**); `orchestrator_runtime/eval_harness/metrics.py` (AUC/Brier/calibration);
  `modal_workers/shared/fda_calibration_math.py` (guardrail helper — adapt for the CI-floor report). New:
  one `openfda_crl_transparency.py` fetcher.
- **Detail-agent brief:** Plan the weekly scorer worker (feature assembly → M14 import → `bc_rubric_scores`
  as band), the feature-set ablation, and the A0 cohort build + CI computation. Verify the `feature_assembly`
  output dict matches the scorer's expected keys and that `ref_date` enforces no look-ahead.

## Phase 2 — Daily monitor: 3 deterministic streams + threshold + synthesis  **(the moat)**

- **Goal:** the daily engine. For each universe name: pull 3 deterministic streams, and **only on a
  deterministic threshold** fire 1 Haiku classify + 1 Sonnet synthesis that frames the name **vs the
  market-implied move**.
- **In scope:** streams = **insider/Form 4** (EDGAR), **options IV / straddle-implied move** (Polygon),
  **news/8-K** (EDGAR). Threshold check (Python). On trigger → 1 Haiku classify (verdict/topic) → 1 Sonnet
  synthesis (what-changed, rank vs implied move, action). Hard daily spend ceiling; near-dup dedup + per-name
  cap. **Synthesis JSON contract is the moat layer — design it carefully** (under-specified in the spec).
- **Out:** 13F, price-cohort streams, tier taxonomy, rollups, sNDA — all cut.
- **Deliverable:** daily monitor cron writing `bc_market_signals`, `bc_news_events`, `bc_thesis_updates`;
  cost controls; the synthesis contract.
- **Exit gate:** all universe names get daily signals; on a seeded delta, a schema-valid `bc_thesis_updates`
  row is produced framed vs implied move; daily spend ≤ ceiling; **zero Cowork / zero LLM in control flow.**
- **Reuse anchors:** `orchestrator_runtime/client.py` (Anthropic call + prompt caching + `attach_budget`
  ceiling + retry); deployed `bc_news_event_upsert()` RPC + `bc_scanner` least-priv role (migration 003) —
  but **called by the metered worker, not Cowork**; Polygon options (spec §B.5); `bc_config`
  (`l4.daily_budget_usd`, `l4.max_events_per_candidate_day`); tables `bc_market_signals`, `bc_news_events`,
  `bc_thesis_updates`, `bc_failed_synthesis_calls`.
- **Risks:** Polygon options tier/cost; synthesis quality; threshold false-positive rate (dry-run to tune).
- **Detail-agent brief:** Plan the 3 fetchers + threshold logic + the Haiku/Sonnet calls (model, caching,
  budget kill, JSON-schema validation, plausibility/corroboration rule so no single LLM verdict escalates),
  and — most importantly — **the synthesis output contract** (fields, the market-implied-move framing,
  recommended-action gating). Confirm Polygon options access.

## Phase 3 — Digest interface + outcome logging

- **Goal:** the daily email digest (the product surface) + the one surviving feedback element.
- **In scope:** daily email = risk-rank beside market-implied move + what changed + the 1–2 worth a look
  (reuse existing Resend path). **Outcome logging only** — record predictions + eventual regulatory/price
  outcomes to `bc_prediction_outcomes`; **no refit loop** (refit ≈ 1 CRL/yr).
- **Deliverable:** daily digest email; outcome log backfilled as catalysts resolve.
- **Exit gate:** a real digest renders end-to-end; resolved catalysts land in `bc_prediction_outcomes`.
- **Reuse anchors:** existing Resend/fanout path (**locate — not in the dashboard repo per exploration**;
  likely an edge fn / v4 fanout; the Phase-3 agent confirms); `modal_workers/ingestion/openfda_ingest.py`
  + the CRL Transparency fetcher for resolved outcomes; `bc_pipeline_runs`, `bc_prediction_outcomes`.
- **Detail-agent brief:** Locate and plan reuse of the Resend path; design the digest template + the
  outcome-labeler cron. No drift alarms, no gated refit.

## Phase 4 — Dashboard drill-down  *(can lag — usable from the digest alone)*

- **Goal:** a per-name detail view for when the digest flags something.
- **In scope:** a candidate list + per-name page (rank/band, the 3 streams, latest synthesis). Reuse the
  App-Router patterns; anon key + JWT (already safe).
- **Reuse anchors:** `dashboard/app/operator/flags/page.tsx` (list), `dashboard/app/operator/runs/[id]/page.tsx`
  (detail), `dashboard/lib/nav-config.ts` (nav registration), `dashboard/lib/supabase/server.ts`,
  `dashboard/lib/api/operator/`.
- **Detail-agent brief:** Plan `app/operator/bc-candidates/page.tsx` + `[appNumber]/page.tsx` + loaders,
  reading the populated `bc_*` tables.

## Cross-cutting (run alongside the phases)

- **Strangle v4:** build the light core beside v4; flip off v4's FDA path only after the monitor is proven
  on resolved outcomes (criterion TBD — see Open decisions).
- **Schema activation:** apply **migration 005** (`operator_flags` bc_ sources) *before* any bc_ flag write,
  re-introspecting the live CHECK constraint first (per migration-drift discipline). Leave L7/tier/rollup
  tables dormant.
- **Doc reconciliation:** the spec's printed §7.1.3 view SQL and §7.2 seed are **stale v1.0** and contradict
  the deployed migrations — reconcile or mark "migrations authoritative."

---

## Sequencing & dependencies

```
Phase 0 (universe/PDUFA)  ── GATE ──┐
                                    ├─→ Phase 1 live score ─→ Phase 2 monitor ─→ Phase 3 digest ─→ Phase 4 dashboard (lags)
A0 cohort (Phase 1, parallel) ──────┘   (A0 runs immediately; live score waits on Phase 0's universe)
Cross-cutting: continuous
```

Critical path = **0 → 1(live) → 2 → 3**. A0 cohort and Phase-4 scaffolding parallelize. Phase 0 is the
single fork: if the universe can't be sourced cheaply, stop and rethink before building the monitor.

## Explicitly CUT (from the endorsed direction)

sNDA entirely · paid analyst feed · 13F + price-cohort streams · L7 feedback/refit (keep only outcome
logging) · L3 active/watchlist/refused tier taxonomy · L5's 6 rollups · cold-start backfill as standing
infra · **all Cowork in the daily path** · calibrated `p_crl` display.

## Verification (per gate, end-to-end)

- **P0:** run the enumeration job → assert ≥ ~15–20 pending in-window names with PDUFA + tradeability;
  diff against a hand-checked recent-catalyst list; coverage/latency/cost table per source.
- **P1:** weekly cron writes `bc_rubric_scores` (band) for the universe; A0 prints AUC + bootstrap CI on
  the OOS Transparency cohort; assert `feature_assembly` keys == scorer inputs and no look-ahead.
- **P2:** daily cron writes `bc_market_signals` for every name; seed a delta → assert one schema-valid
  `bc_thesis_updates` row framed vs implied move; assert daily spend ≤ ceiling and no Cowork/LLM in control flow.
- **P3:** trigger a real digest email (rank + implied move + what-changed + picks); backfill a resolved
  catalyst into `bc_prediction_outcomes`.
- **P4:** dashboard list + detail render from `bc_*` via anon+JWT.

## Open decisions (resolve at/by the noted phase)

1. **PDUFA source** — chosen via the Phase-0 benchmark (Pedro: compare all three).
2. **Polygon options tier / market-implied data access** — confirm before Phase 2 (dependency for the moat).
3. **Resend path location** — Phase-3 agent to locate (not in the dashboard repo).
4. **v1 feature set** — ablation in Phase 1 (keep only the high-signal sources; CT.gov dropped for v1).
5. **v4 strangle switch criterion** — how many resolved outcomes before disabling v4's FDA path.
6. **Universe thresholds** — window-days + market-cap/ADV floor (Phase 0/1 tuning).

---

## Detail-planning status & findings (2026-06-03)

The 3 parallel de-risk tracks are detail-planned: `tasks/bc_v4_phase0_universe_spike.md`,
`tasks/bc_v4_a0_cohort_confidence.md`, `tasks/bc_v4_phase2_synthesis_contract.md`. Downstream build
phases (1-live, 2-streams, 3, 4) are held until the Phase-0 gate resolves. Load-bearing corrections:

- **Phase 0 is de-risked:** a working 8-K PDUFA-*date* extractor already exists
  (`modal_workers/scanners/fda_pdufa_pipeline.py::_parse_filing_for_pdufa` + `shared/edgar_efts.py`) —
  approach 1 is lift-and-harden, likely the winner. `bc_candidates` already encodes the G2/G3 universe
  gate; live `bc_config` = window 120d / min_mcap **$250M** / min_adv **$2M** — **$250M may be too strict
  for catalyst-stage biotechs; tune.** Polygon supplies mcap/ADV/options-boolean. (Calendar approach:
  BioPharmaCatalyst 404s unauth; FDA-primary can only infer a ±weeks date.)
- **Feature-substrate gap (revises "~70% reuse"; affects A0 + Phase 1):** `feature_assembly.py` reads
  `fda_application_submissions` + `fda_drug_inspections` which are **absent live** (`fda_warning_letters`
  empty). But the missing table's **DDL + writer already exist on disk** (`supabase/migrations/20260615000000_fda_application_submissions.sql`
  unapplied; `openfda_ingest.extract_submission_rows`) → apply + a targeted Drugs@FDA pull, not from-scratch.
  A shared offline point-in-time builder (parity-tested vs `feature_assembly`) serves both A0 and live.
- **Scorer already vendored (correction):** M14 lives in-repo at `modal_workers/shared/fda_crl/nda_scorer.py`
  (byte-identical model JSON) — import it; do NOT re-vendor from `~/Downloads` (drift hazard).
- **`p_crl` is persist-internal, never-display (cross-phase contract):** the `bc_candidates` matview still
  GATES NDA/BLA on `p_crl <= tau_nda` and joins `scorer_name='M14_adjusted'`, so Phase 1 MUST persist `p_crl`
  — but Phases 2/3/4 must NEVER render it (band/percentile only). Enforce via explicit `.select()` lists + a CI test.
- **A0 most immediately executable:** negatives partly ready-made (M14 `prospective_2026_predictions.csv`, 33
  first-cycle label-0 rows, zero CRL collision); OOS positives ≈ 35–45; the `Orig1s000` first-cycle heuristic
  is broken for 2025–26 (use Drugs@FDA submission history); expect AUC CI floor < 0.70 → "show band with a caveat."
- **Moat input — code built, data entitlement-gated (the one real Pedro decision):** `PolygonOptionsData`
  (`modal_workers/providers/polygon/options_data.py`, wired via `fda_signal_bridge`) provides chain / IV /
  straddle-implied-move — so it's not a from-scratch build — BUT the live Polygon key returns **403 on the
  options endpoints** (the subscription lacks the options entitlement). → **DECIDED 2026-06-03: ship band-only
  for v1.** The options/IV stream + the market-implied-move framing move to **v1.1** (deferred, entitlement-gated);
  v1 synthesis runs on Form 4 + news/8-K with `recommended_action` capped at `monitor`. Revisit the Polygon
  upgrade once the daily monitor proves out.
- **Dup detail-plans reconciled (2026-06-03):** Phase 1/2/3 each had two parallel drafts (mine + Pedro's);
  merged to one canonical each (`bc_v4_phase1_live_score.md`, `bc_v4_phase2_monitor_streams.md`,
  `bc_v4_phase3_digest.md`); redundant copies removed. Full set = 7 plans (Phase 0, A0, 1, 2-streams,
  2-synthesis, 3, 4) + this high-level plan.

### Build first: the shared foundation layer

Four phases assume the same small set of primitives that aren't built yet — landing these first unblocks
0/1/2/3 at once:
- `modal_workers/shared/bc_pipeline_runs.py` — open/close run helper (fail-loud liveness) — used by Phases 0/1/2/3.
- shared offline **point-in-time feature builder** (`feature_builder_pit.py`, parity-tested vs `feature_assembly`) — A0 + Phase 1.
- `openfda_crl_transparency.py` ingester (FDA CRL Transparency dump) — A0 + Phase 3 outcome-labeler.
- **apply** migration 005 (`operator_flags` bc_ sources) **and** the on-disk `20260615000000_fda_application_submissions` migration.
- `bc_news_event_classify` RPC (Phase 2 classify UPDATE) + the threshold `bc_config` key seed (synthesis-contract migration).

**Two flags from the merges (Pedro's call):** (a) `oof_percentile_rank` is persisted as the locked-2025
reference; within-snapshot ordering lives in the run log — if the digest must *sort* on a persisted rank,
add a persisted column. (b) `bc_candidates` `REFRESH … CONCURRENTLY` needs a unique index; the drafts
disagreed on whether one exists → Phase 1 re-introspects `pg_index` before wiring the refresh.
- **Smaller execution corrections:** `documents` has only ~424 8-K rows + no `entity_id` (route `n_8ks` via
  EFTS count-by-CIK); `bc_pipeline_runs.status` CHECK = {running,succeeded,partial,failed} (the synthesis-contract
  plan's `ok`/`killed_budget` values would be rejected — map budget-kill+crash → `failed`); threshold `bc_config`
  keys unseeded; migration 005 unapplied; a `bc_news_event_classify`/thesis-upsert RPC is needed.
