# DECISIONS

Append-only log of material decisions affecting Conan's plan, schema, scoring, or operational discipline. Each entry has Context (what prompted it), Decision (what was chosen), Consequences (what changes downstream). Replaying entries in reverse must reconstruct prior state.

Numbering: legacy v1/v2 decisions are recorded inline in `spec.md` and `CONAN_SCORING_METHOD.md` (D-001 through D-052). v3 starts at **D-100** to leave a clean gap; it is the durable record going forward. Memory entries remain ephemeral session state.

Format: `## D-NNN — Title (YYYY-MM-DD)` with three sub-sections.

---

## D-100 — Initialize v3 DECISIONS.md (2026-05-06)

**Context.** Investment_engine_v2 export bundle review surfaced an "append-only DECISIONS.md (D-NNN log) + Plan discipline" gap in the v3 plan. The legacy CONAN_SCORING_METHOD.md and spec.md track D-001..D-052 inline; v3 needed a single durable file at repo root rather than scattered references.

**Decision.** Create `/DECISIONS.md` as the single append-only decision log going forward. Every material plan change (scoring rubric, schema, prompt revision, sub-agent skill swap, calibration refit, ingestion pattern) appends a D-NNN entry before merge. Legacy decisions stay where they are; v3 starts fresh at D-100 to avoid renumbering.

**Consequences.** No more "where was this decided?" hunts. Reversibility becomes mechanical: replay D-NNN in reverse to reconstruct prior state. Memory system stays for ephemeral context; DECISIONS.md is the durable record. Closes recommendation R6 from `~/.claude/plans/in-the-export-skills-glittery-whisper.md`.

---

## D-101 — Accept Investment_engine_v2 export bundle integration (2026-05-06)

**Context.** Pedro dropped a 66 MB reference snapshot at `~/Downloads/_EXPORT_skills_scoring_methodology/` — a predecessor system distilled to 2 profiles, 3 scanners, 18 skills, 1502-event labeled binary_catalyst ledger, L1/L2/L3 calibration framework. Comparison + recommendation plan written at `~/.claude/plans/in-the-export-skills-glittery-whisper.md` with 9 recommendations R1–R9.

**Decision.** Accept all 9 recommendations. Each gets its own D-NNN entry below for the record. v3 architecture (probabilistic conviction_pct + isotonic calibration) is preserved; the export's contributions are doctrinal (DECISIONS log, rollback monitor, survivorship rule, numeric confidence requirement), data (1502-event ledger as eval_harness seed candidate set), methodology (paired-bootstrap promotion gate, L3 rollback semantics), and scaffolding (Tier-1 skill methodology for v3 sub-agents).

**Consequences.** v3 plan (`~/.claude/plans/confirm-orchestrator-cuddly-bubble.md`) gets inline amendments per R3, R4, R5, R9 with D-NNN tags. Sub-agent skill stubs in `conan-fda-orchestrator-plugin/skills/` get pre-populated per R7. R1 (eval_harness seed from binary_catalyst.json) and R8 (EDGAR checkpoint patch + sponsor resolver helper) are queued as separate work items because they require Phase 1 dependencies + non-trivial implementation lift.

---

## D-102 — Keep conviction_pct architecture, do not adopt categorical-band rubric (R2) (2026-05-06)

**Context.** Export bundle ships a categorical-band rubric (`weighted_total = Σ(dim × weight)`; bands hard-coded `Immediate ≥30 / Watchlist 20–29 / Archive 10–19 / Discard <10`). v3 plan specifies probabilistic `conviction_pct` from ensemble (N=7) + isotonic calibration; bands derived from percentiles. Comparison forced the explicit question: should v3 adopt the export's substrate?

**Decision.** No. Probabilistic conviction preserves calibration signal that binning destroys; isotonic regression operates on percentiles, not bin labels. The export itself parks L1/L2/L3 because it cannot empirically justify weight changes — v3's framework (ensemble + isotonic) does not have that bottleneck. Categorical bands stay as derived outputs of `conviction_pct`, never inputs.

**Consequences.** No code change. v3 plan §Design Principles "Probabilistic + calibrated, not categorical" stands. Legacy `modal_workers/shared/rubric_engine.py` (6-profile categorical scorer) is not on the v3 critical path; left intact for v2-substrate compatibility.

---

## D-103 — Adopt paired-bootstrap promotion gate for eval_runs.passed_gate (R3) (2026-05-06)

**Context.** v3 plan defines `eval_runs.passed_gate boolean` but leaves the gate logic TBD. Export `methodology/calibration_methodology.md` §L2 specifies a paired-bootstrap criterion that has been through real eval discipline.

**Decision.** A candidate prompt/version passes `eval_runs.passed_gate=true` iff ALL of:
- Brier score delta vs production is positive
- Paired-bootstrap p < 0.05 on Brier delta
- n ≥ 200 resolved cases in the eval set
- Calibration AUC delta ≥ 0.05
- No single asset contributes > 5% of the win

On pass: snapshot the previous prompt + calibration_curve before the new one writes (per D-104). Promotion is automatic when `ENABLE_PROMOTION=true`; otherwise the row records `passed_gate=true` with a manual dispatch flag.

**Consequences.** v3 plan §Phase 3 amended inline. eval_runs row schema gets explicit gate-criterion fields when the migration lands. Lucky-batch promotions are blocked structurally, not by reviewer discipline. Closes R3.

---

## D-104 — Add rollback monitor + snapshot-before-mutation policy (R4) (2026-05-06)

**Context.** v3 has no rollback story today. Export learned this the hard way (D-002 weights frozen until Phase 13 specifically because no rollback monitor existed early in v2). Export's L3 = daily Spearman correlation monitor with snapshot-restore on drift.

**Decision.** Two parts:
1. **Daily rollback cron** (Modal scheduled function): compute Spearman(realized_return_30d, conviction_pct_calibrated) over the last 30 days of resolved `post_mortem_queue` rows. If correlation < 0.20 OR drops ≥ 0.15 from the prior window, restore the last `calibration_curves` snapshot and alert.
2. **Snapshot-before-mutation policy**: every prompt version stored append-only in a `prompt_versions` table (never overwrite); calibration_curves already keyed by `version text PRIMARY KEY` (additive). Sub-agent skill versions (`literature_reviewer_v1.md`, `_v2.md` etc.) are git-tracked which gives natural reversibility.

**Consequences.** v3 plan §Phase 3 + §Schema amended inline. New Modal function spec `modal_workers/scripts/rollback_monitor.py` queued as a Phase 3 work item. Schema migration adds `prompt_versions` table when Phase 5 (sub-agents) lands. Closes R4.

---

## D-105 — Add survivorship rule + tradeable filter + numeric confidence to eval_harness/extracted_facts schema (R5) (2026-05-06)

**Context.** Export `methodology/methodology_spec.md` documents two rules v3 plan was missing for eval_harness curation: (1) include delisted/acquired/bankrupt issuers (no survivorship bias — the negative tail is essential for calibration), (2) tradeable filter at event date (mcap ≥ $215M, public exchange, 90d ADV ≥ $500K). Export OBJECTIVE.md principle 5 also requires "confidence-annotated outputs everywhere" — v3 has Citations API but `extracted_facts.confidence` is nullable with no documented [0,1] semantics.

**Decision.** Schema amendments to land in the next migration:
- `eval_harness`: add `tradeable_filter_pass boolean NOT NULL DEFAULT false` + curation rule documented in v3 plan §Schema (include delisted/acquired/bankrupt; tradeable filter applied at reference_assessment_date).
- `extracted_facts`: change `confidence numeric(3,2)` from nullable to `NOT NULL`; document [0,1] semantics in v3 plan §Schema (1.0 = primary-source verbatim, 0.7 = LLM-extracted from primary source, 0.5 = derived/inferred, < 0.5 = speculative — flagged for review).

**Consequences.** v3 plan §Schema sections for both tables amended inline. Migration file queued (separate work item — needs backfill plan for any existing rows). Without these rules, eval_harness will have a silent positive-tail bias that kills calibration. Closes R5.

---

## D-106 — Append-only DECISIONS.md substrate (R6) (2026-05-06)

**Context.** See D-100. R6 from the export bundle plan was the substrate that lets every other R land with a D-NNN tag.

**Decision.** Implemented in this file. Format locked: `## D-NNN — Title (YYYY-MM-DD)` with Context / Decision / Consequences. New decisions append at the bottom. Edits to historical entries are forbidden (use a new D-NNN that supersedes — entry text says `Supersedes D-XXX`).

**Consequences.** Closes R6. Enables all other accepted Rs to record their decision trail.

---

## D-107 — Pre-populate sub-agent skill stubs from export Tier-1 methodology (R7) (2026-05-06)

**Context.** v3 plan calls for 4 sub-agents in Phase 5 (literature, competitive, regulatory_history, options) with skill files in `conan-fda-orchestrator-plugin/skills/`. Files are currently empty. Export ships 3 Tier-1 (live-source-validated, confidence ≥0.70) skills: P1 (analyze-fda-approval-prospects, AXSM worked output), P2 (research-clinical-class-precedent), P3 (research-activist-filer). P1/P2 map directly to v3's regulatory_history + literature sub-agents.

**Decision.** Pre-populate three sub-agent skill files with input/output schemas + methodology sections lifted from export Tier-1 SKILL.md content (NOT helper code — v3 sub-agents are Claude Agent SDK subagents driven by MCP tools, different runtime). Skip `sub_agent_options_microstructure.md` — no export analog. Worked outputs (AXSM probability_estimate.json + verification_report.md) copied to `orchestrator_runtime/eval_harness/fixtures/` as A/B test fixtures for sub-agent versioning.

**Consequences.** v3 Phase 5 starts with non-empty skill files that have already been through real eval. Faster than building from scratch. Closes R7. Skill file content treated as `_v0` (pre-A/B-test starting point); first eval-gated revision becomes `_v1`.

---

## D-108 — Document FDA-only trade in v3 plan §Context (R9) (2026-05-06)

**Context.** Export's most expensive lesson was the 7→2 profile reset: concentrate edge by cutting breadth. v3 went one step further (effectively 1 profile — FDA asset orchestrator with EDGAR as paired source). The export's RPAY-pattern convergence (13D + 8-K rights agreement, composite +8 → dispatch_now at score 33 even though individual signals scored in the 20s) is exactly the kind of multi-source pattern v3 claims as edge. By cutting EDGAR-style activist signals, v3 forfeits this class of opportunity.

**Decision.** Hold the line on FDA-only — do not reopen activist_governance now. But document the trade explicitly in v3 plan §Context: by going from 2 profiles to 1, v3 forfeits the RPAY-class 13D+8-K convergence pattern. The decision stands (FDA depth > breadth) but the cost is acknowledged. Re-evaluate at Phase 6 retrospective: if FDA conviction quality justifies the focus, stay narrow; if not, reopen activist_governance using the export's labeled events as bootstrap.

**Consequences.** v3 plan §Context paragraph added inline. Closes R9. Phase 6 retrospective gets activist_governance reopen as a standing agenda item.

---

## D-109 — Queue R1 eval_harness seed as Phase 1-blocked work item (2026-05-06)

**Context.** R1 proposes ETL of export's `data/v2_data/historical_events/binary_catalyst.json` (1502 events) into v3's `eval_harness` table — 30× the Phase 0 target. Blocking dependencies: (a) v3 `documents` table must be live before `document_set uuid[]` can be populated (Phase 1 work); (b) forward-return labeling pass via yfinance per export methodology_spec.md §forward-return-windows; (c) re-fetch of primary documents per export CLAUDE.md "do not trust live data — re-harvest"; (d) ticker-resolution backlog must be addressed first (see D-110 queue item, which addresses the same wedge as the export's `bc_ticker_resolution_required` postmortem).

**Decision.** Queue as Phase 1 follow-on work item: `scripts/seed_eval_harness_from_export.py`. Steps documented in plan R1. Not started in this turn — depends on (a) and (d) above. R1 status remains "accepted, queued" until those land.

**Consequences.** v3 Phase 0 eval harness operates with smaller hand-curated set in the meantime. Phase 1 delivery includes documents table → R1 ETL becomes unblocked → Phase 3 backtest gets 1500-event base. No code written this turn; tracked here so the work item is not lost.

---

## D-110 — Queue R8 ingestion-resilience patches (EDGAR checkpoint + sponsor resolver) (2026-05-06)

**Context.** R8 audit on 2026-05-06 found:
- **EDGAR checkpoint+retry (export WI-3-A-2)**: v3's `modal_workers/ingestion/edgar_ingest.py` has timeouts + 429-backoff + 500-fast-fail via delegation, but **lacks** per-bucket try/except wrapping the hit-loop (lines 99–101), incremental persist after each completed hit, and finally-block `_persist(final=True)`. A crash mid-run loses all in-memory results.
- **openFDA sponsor→ticker resolution (export `bc_ticker_resolution_required`)**: v3's `modal_workers/ingestion/openfda_ingest.py` extracts `sponsor_name` (line 193) but never resolves to ticker; same wedge as the export pre-fix. Resolution exists fragmented across `scripts/curate_eval_harness.py` (Supabase entities ILIKE + Jaccard) and a hardcoded SQL migration (~34 sponsors); no reusable Python helper.
- **8-K primary-document picker (export `pick_primary_8k_document_FP`)**: NOT applicable to v3 today (EFTS one-hit-per-file approach sidesteps the AXGN pattern). Noted for future Phase 1 extracted_facts work.

**Decision.** Two queued work items:
1. **EDGAR checkpoint patch** — wrap `_ingest_one_hit` loop with per-hit try/except; add `_persist(final=False)` after each hit + `_persist(final=True)` in finally. Bound the per-query wall-clock budget. Owner: Phase 1 ingestion hardening.
2. **Sponsor resolver helper** — new `modal_workers/shared/sponsor_resolver.py` consolidating the curated pharma-name dict + Jaccard fallback against `entities` table. Called by `openfda_ingest.py` post-extract; writes resolved ticker to documents.extensions or queues for downstream batch resolve. Owner: Phase 1 (blocker for R1).

**Consequences.** Closes R8 audit phase. Patches not implemented this turn — they're substantive code work. Until patched, EDGAR ingestion is fragile to mid-run failures and openFDA-sourced events lack tickers. R1 is blocked on the sponsor resolver. Tracked here so neither patch is lost.

---

## D-111 — v3 Dashboard Visual & UX Language Lock (2026-05-07)

**Context.** Phase 0+1 of the v3 schema landed 2026-05-06 (`supabase/migrations/20260506000010_v3_phase_0_1_schema.sql`): new tables (`documents`, `asset_documents`, `extracted_facts`, `fda_asset_parties`, `memory_files`, `reference_class_base_rates`, `calibration_curves`, `eval_harness`, `eval_runs`), extended `fda_assets` (`program_status`, `is_active`, `watch_priority`, `reviewer_panel_id`, `reference_class_signature`, `indication_normalized`, `memory_path`), extended `fda_event_features` (shadow_*, score, band, EV%, pricing_edge, evidence_confidence, options_liquidity_score, raw_inputs), new view `dashboard_signal_rows`, plus convergence fields. The v3 orchestrator runtime (Tier 1/2/3 pipeline producing `conviction_pct`, `ensemble_dispersion`, sub-agent intel from literature/competitive/regulatory/microstructure agents, structured citations via Anthropic Citations API) is not yet shipped — plugin skeleton exists. The dashboard repo (`marazuela/conan-dashboard`) carried zero references to any v3 field at session start. The dashboard upgrade plan (`~/.claude/plans/plan-a-dashboard-upgrade-effervescent-avalanche.md`) calls for a Phase A foundation lift that scaffolds v3 UI surfaces against an orchestrator output proposal so Phase B/C/D pages can light up later without rework.

**Decision.** Lock the visual/UX language for v3 dashboard outputs. Scope: information architecture and signaling conventions, not pixel styling. Supersedes ad-hoc patterns in `/fda` and `/signals` where they conflict.

**§0 Cross-cutting conventions**

| Convention | Lock |
|---|---|
| Density target | Signal cards readable 3-up at 1440px viewport. Card max-height 320px. |
| Color signaling | Conviction and tier MUST NOT rely on color alone. Always pair with text or icon. |
| Print/export | Detail pages print legibly with citations resolved as numbered footnotes. |
| Mobile | List views (`/fda`, `/assets`, `/`) mobile-first. Sub-agent panels and `<CitationPanel />` are desktop-primary; collapse on mobile. |
| Tier action gating | Tier 2 events: state-mutation RPCs allowed; promotion / IC-memo RPCs disabled. Tier 3: read-only. |
| v2/v3 coexistence | Single slot per metric. v3 field NULL → render v2-derived fallback with `v2-derived` badge. No side-by-side. |
| Token reuse | New components reuse existing `--band-*` CSS variables and the `.text-[10px] uppercase tracking-wide` chip pattern from `dashboard/components/ui/band-chip.tsx`. |

**§1–12 Question / Answer matrix**

1. **Conviction display** — point + bracket (`58% [49–67]`); ensemble strip on hover. Trade-off: compresses bimodal ensembles into one interval.
2. **Sub-agent panels** — vertically stacked collapsibles, fixed order (lit → competitive → regulatory → medical → microstructure → IC memo). IC memo always-expanded; rest collapsed. Trade-off: tabs hide volume and break Cmd-F across agents.
3. **Citations UX** — inline `[n]` markers with hover popover; click opens persistent right-side `<CitationPanel />` (hybrid). Trade-off: side-panel-only eats horizontal real estate; pure inline doesn't survive print.
4. **Tier 1/2/3 visual** — badge in card and detail header **plus** global tier filter in nav. Tier 3 hidden from main views (lives in `/eval`). Action gating per §0. Trade-off: watermarks cause trust-by-noise fatigue; borders collide with band colors.
5. **Calibration transparency** — tooltip on conviction display shows curve version + Brier; full history at `/calibration`. Trade-off: per-card footers add visual weight nobody reads.
6. **Eval surface** — top-level `/eval` (Cases + Runs tabs); admin-gated writes. Trade-off: folding into `/reports` blurs ops vs system-health views.
7. **Asset state** — BOTH a `/assets` browser keyed on `watch_priority` AND inline asset header on `/fda/[event]` pages. Multi-sponsor `fda_asset_parties` render as chip row in both places. Trade-off: two surfaces means two truth points to keep aligned.
8. **Extracted facts** — searchable table on the asset page (canonical) + top-N inline chips on signal cards (chip click expands to table row). Trade-off: pure tab hides corpus; pure chips overwhelm cards.
9. **Document lineage** — canonical "Source documents" panel on asset page; signal cards show compact `Sources: 3 primary, 2 safety` summary linking through. Trade-off: duplicating to cards risks divergence with the asset view.
10. **Reference class anchor** — one-liner under conviction (`Reference class: oncology_hematologic (n=124, base 41% [38–44%], median move ±9%)`); click expands cohort detail. Trade-off: anchoring too prominently pulls operators away from signal-specific evidence.
11. **Migration path (v2 ↔ v3)** — computed-from-v2 fallback with explicit provenance. `conviction_pct` NULL → render v2-derived value with `v2-derived` badge in same slot. `band` and `band_with_bonus` continue to drive sort/filter as fallback until parity. Trade-off: more complex render path and one badge state to QA.
12. **Authoring stance** — REAFFIRM D-008 / AG1 read-only with existing carve-out: operators may author rationale notes (`dashboard_rationale_upsert`) and state transitions (`dashboard_candidate_set_state`, `dashboard_thesis_*`, `dashboard_failure_resolve`, `dashboard_flag_resolve`, `resolved_at` dismissal). Operators may NOT author conviction, citations, sub-agent outputs, or extracted facts. v3 introduces no new authoring affordances.

**§13 Orchestrator output contract** (proposal — backend track ratifies)

```ts
// On signals (and/or fda_event_features mirror):
{
  conviction_pct: number | null         // 0-100, isotonic-calibrated
  ensemble_dispersion: number | null    // σ across ensemble members, in pct points
  ensemble_members: number[] | null     // raw member estimates (hover strip)
  tier: 1 | 2 | 3 | null                // 1=full pipeline, 2=bulk, 3=backtest
  calibration_curve_id: uuid | null     // FK calibration_curves.id active at scoring
  reference_class_signature: text | null // FK reference_class_base_rates
}

// On fda_agent_reviews — extend agent_kind enum:
agent_kind: 'medical' | 'regulatory' | 'microstructure'
          | 'literature' | 'competitive' | 'ic_memo'  // NEW

structured_output: {
  summary: string                       // <=300 char human-readable
  key_findings: Array<{ text: string; citation_ref: number }>
  uncertainties: string[]
  // agent-specific keys below summary
}

citations: Array<{
  ref: number                           // matches citation_ref in structured_output
  document_id: uuid                     // FK documents.id
  span_start: number                    // char offset in documents.raw_text
  span_end: number
  snippet: string                       // <=300 char excerpt
  source_url: text | null
}>
```

**§14 Layout sketch — `/fda/[id]` v3 detail page**

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ [TIER 1] AVNS · ABC-123 (oncology, hematologic) [program: phase3] [active]   │
│ Sponsors: Avenas Bio (sponsor 60%) · Roche (licensee 40%)                    │
├──────────────────────────────────────────────────────────────────────────────┤
│ ConvictionDisplay (hero)        │  ReferenceClassAnchor                      │
│ ┌───────────────────────────┐   │  oncology_hematologic                      │
│ │ 58% [49–67]               │   │  n=124 · base 41% [38–44%]                 │
│ │ ensemble: ▂▃▆█▇▅▃▂        │   │  median realized move ±9%                  │
│ │ [v2-derived] (when NULL)  │   │  [expand cohort →]                         │
│ └───────────────────────────┘   │                                            │
│ Calibration: curve v17 · Brier 0.17 · pinned 2026-05-04 [/calibration]       │
├──────────────────────────────────────────────────────────────────────────────┤
│ EV waterfall (existing 16-metric panel; values from dashboard_signal_rows)   │
├──────────────────────────────────────────────────────────────────────────────┤
│ ▾ Lit Reviewer            (3 sources · conf 0.78)                            │
│ ▾ Competitive Landscape   (2 sources · conf 0.72)                            │
│ ▾ Regulatory History      (5 sources · conf 0.81)                            │
│ ▾ Options Microstructure  (1 source  · conf 0.65)                            │
│ ▴ IC Memo Polish          (always expanded, structured_output rendered)      │
│   "Catalyst is PDUFA 2026-09-12 [1]. Phase 3 hit primary endpoint [2]..."    │
├──────────────────────────────────────────────────────────────────────────────┤
│ Extracted Facts (top 5 · search · → /assets/[id]#facts for full table)       │
├──────────────────────────────────────────────────────────────────────────────┤
│ Source Documents (top 5 · → /assets/[id]#docs for full lineage)              │
├──────────────────────────────────────────────────────────────────────────────┤
│ Operator Actions  [Approve] [Suppress] [Pin] [Refresh] [Override] [Mark bad] │
│ Tier 2 only:      [Set watch priority] [Toggle active] [Pin reference class] │
└──────────────────────────────────────────────────────────────────────────────┘
                                                   ┌───────────────────────────┐
                                                   │ CitationPanel (right rail)│
                                                   │ [1] Federal Reg. 2026-04-22│
                                                   │ [2] 8-K, 2026-05-01       │
                                                   └───────────────────────────┘
```

**Open questions** (NOT decisions; resolve during implementation):
- Tier 2 RPC surface beyond IC promotion gating.
- `/calibration` per-profile reliability plots vs single global curve.
- Dedicated `/orchestrator` page for run inspection vs folding into `/eval`.

**Consequences.**
- Phases B/C/D of the dashboard upgrade plan cannot fully ship until the orchestrator output contract (§13) is ratified by the backend track. Phase A (foundation: types regen, `dashboard/lib/api/`, v3 components) scaffolds against the proposal; if the contract diverges, the four v3 components consuming it (`<ConvictionDisplay />`, `<SubAgentPanels />`, `<CitationViewer />`, `<TierBadge />`) and the typed fetchers in `dashboard/lib/api/` are the only touchpoints to update.
- Backend track owes new RPCs to support dashboard mutations: `fda_asset_set_watch_priority`, `fda_asset_set_active`, `fda_asset_pin_reference_class`, `eval_case_open`, `eval_case_resolve`. Calibration mutations reuse existing `fda_calibration_activate` / `fda_calibration_rollback` (no new RPC needed). All write to `operator_actions` for audit. SQL stub at `supabase/migrations/20260506000020_v3_dashboard_rpcs.sql`.
- Non-FDA pages (`/profiles`, `/decisions`, `/scanners`, `/convergence`, `/flags`, `/reports`, `/archive`, `/`) are out of scope for redesign; they inherit shared `BandChip` and `ThesisView` upgrades automatically.
- D-111 is the canonical reference for v3 dashboard semantics. Future visual changes to conviction, citations, sub-agents, tiers, or asset state must amend this entry.
- Original draft was filed in error as `D-035` in `unified_system/unified_system/docs/DECISIONS.md` — reverted 2026-05-07. The legacy register stops at D-014; v3 entries live here from D-100 onward. The number `D-035` is taken in the legacy register by Pedro's band-threshold shift (`dashboard/content/decisions/d-035.md`).

---

## D-112 — Implement D-110 ingestion patches: sponsor_resolver + EDGAR per-hit try/except (2026-05-07)

**Context.** D-110 queued two ingestion-resilience work items: (a) sponsor_name → ticker resolver consolidating the existing fragmented logic, (b) EDGAR per-hit try/except + finally-block summary to prevent a single bad hit from aborting the loop. R8 audit on 2026-05-06 confirmed both were missing in v3.

**Decision.** Implemented both:

1. New module `modal_workers/shared/sponsor_resolver.py`. Two-pass resolver: Pass 1 = curated `CURATED_MAP` dict (46 entries — mirrors migration `20260430010000` plus 13 commonly-seen openFDA sponsors not yet in the seed) + `PRIVATE_DISCARD` set (11 entries — Boehringer Ingelheim, Mallinckrodt, etc.); Pass 2 = `match_sponsor_to_ticker` Jaccard fallback against the `entities` table (lifted from `curate_eval_harness.py`, now the single source of truth). Returns `SponsorResolution` dataclass with `match_method ∈ {curated, private_discard, jaccard, unresolved}` and confidence ∈ [0, 1]. Wired into `openfda_ingest.py:_ingest_one_drugsfda_record` with `skip_jaccard=True` for hot-path; misses persist as `match_method='unresolved'` in `documents.extensions.sponsor_resolution` for an offline batch resolve pass to fill the tail. `curate_eval_harness.py` now imports the helpers + delegates `match_sponsor_to_ticker` to the shared module (no behavior change).

2. EDGAR patch in `modal_workers/ingestion/edgar_ingest.py` at both `ingest_keyword_search` (line 99) and `ingest_form_sweep` (line 138): per-hit try/except wrapping `_ingest_one_hit` so an unexpected exception (programming error / OOM / bug in `_accumulate`) on one hit no longer aborts the whole loop — failed hit logged with its `_id` for retry, outcome marked error, loop continues. Added try/finally so the summary log line emits even on abnormal exit, surfacing partial work. Per-hit DocumentWriter writes are already durable (DB inserts), so DB state is fine; the patch addresses in-memory loop fragility.

**Consequences.** Closes D-110 implementation. Smoke tests on `sponsor_resolver` pass curated/private_discard/case-insensitive/empty/subsidiary→parent paths. All four touched files compile cleanly. Hot-path ingest cost unchanged (curated lookup is dict-time; Jaccard is skipped). Unblocks D-109 (R1 eval_harness seed) — the script can now resolve sponsor → ticker reliably for the 1502 binary_catalyst events when it's built.

---

## D-113 — Implement D-105 schema amendments + D-103 eval_runs gate fields migration (2026-05-07)

**Context.** D-103 (paired-bootstrap promotion gate) and D-105 (survivorship rule + tradeable filter + numeric confidence NOT NULL) specified schema amendments. The relevant tables (`eval_harness`, `extracted_facts`, `eval_runs`) were already created in `20260506000010_v3_phase_0_1_schema.sql`. The amendments are additive ALTER statements + a NOT NULL change on `extracted_facts.confidence` (which needs an UPDATE backfill before the constraint).

**Decision.** New migration `20260507000000_v3_d105_eval_harness_extracted_facts_amendments.sql` wraps three changes in a single transaction:

- `eval_harness`: ADD `tradeable_filter_pass boolean NOT NULL DEFAULT false` + `issuer_status text` with CHECK ∈ {active|acquired|delisted|bankrupt}; partial index on `tradeable_filter_pass=true`; comments documenting D-105 curation rule (no survivorship bias, stratify on indication × phase × outcome).
- `extracted_facts`: UPDATE existing nulls to `0.50` (D-105 sentinel for "needs review"), ALTER COLUMN `confidence` SET NOT NULL, ADD CHECK confidence ∈ [0, 1]; comment documenting the [0,1] semantics (1.00 verbatim → 0.50 derived → <0.50 speculative).
- `eval_runs`: ADD `brier_delta_vs_prod`, `paired_bootstrap_p`, `ranking_auc_delta_vs_prod`, `n_eval_cases`, `max_single_asset_contribution_pct`, `gate_reason` with appropriate CHECK constraints + per-column comments. `passed_gate` boolean keeps semantics; the new fields record the *inputs* so failed gates are diagnosable.

All ALTERs are idempotent (`IF NOT EXISTS` / `DROP CONSTRAINT IF EXISTS`). Backfill is bounded (single UPDATE on `extracted_facts` rows where confidence IS NULL).

**Consequences.** Closes D-103 + D-105 schema work. Migration is safe to apply against the live Phase 0/1 schema since all tables are additive and the only NOT NULL change has a deterministic backfill. Once applied, the gate logic locked in D-103 has its on-disk record; the survivorship + tradeable-filter audit columns are ready for the D-109 eval_harness seed script to populate; numeric confidence becomes mandatory for new extractor output. No code change required for callers writing new `extracted_facts` rows that already populate confidence (per D-107 the v3 sub-agent output schemas all include confidence per-fact).

**Update 2026-05-07 — applied to production (project `xvwvwbnxdsjpnealarkh`).** Pre-flight: 177 extracted_facts rows / 0 nulls in confidence (backfill UPDATE was a no-op), 81 eval_harness rows, 1 eval_runs row, none of the D-105/D-103 columns existed. Applied via Supabase MCP `apply_migration("v3_d105_eval_harness_extracted_facts_amendments")`. Post-flight verification: all 5 columns present, `extracted_facts.confidence is_nullable='NO'`, all 81 existing eval_harness rows have `tradeable_filter_pass=false` (per DEFAULT — backfill is the responsibility of the curation pipeline). Local migration file at `supabase/migrations/20260507000000_v3_d105_eval_harness_extracted_facts_amendments.sql` matches what was applied; idempotent guards mean re-running it via `supabase db push` is a no-op.

---

## D-114 — Stage 4 reference-class anchoring + Stage 8 isotonic + compute_mcp wrapper (2026-05-07)

**Context.** Phase 2 orchestrator MVP (commit f9be94e) shipped Stages 1, 9, 10; commit cf04136 added Stages 6, 7. Stage 4 (reference-class anchoring) and Stage 8 (isotonic calibration) were left as TODOs. Without Stage 4, conviction_pct is generated purely from the asset-specific evidence with no empirical tether — Stage 7's base-rate divergence check is dead-coded (its `reference_class_base_rate` parameter was hardcoded `None`). Without Stage 8, `conviction_pct_calibrated` was set equal to the raw value, and the `calibration_curves` table is unused. Plugin README (Phase 4.7) lists `compute_mcp.py` exposing five tools (`base_rate`, `similar_resolved_cases`, `isotonic`, `brier`, `verify_claim`); the MCP server file did not exist yet.

**Decision.** Three deliverables:

1. New `modal_workers/shared/compute.py` as the canonical implementation of the compute-mcp tool surface — DB-aware helpers (`compute_base_rate`, `similar_resolved_cases`, `get_active_calibration_curve`, `build_stage_4_anchor`) + pure math (`fit_isotonic_curve` via PAV with no scikit-learn dep, `apply_isotonic_calibration` via linear interp between knots). Re-exports `brier_score` from `fda_calibration_math` so all five tools have a single import surface. `verify_claim` is a `NotImplementedError` stub until Phase 4.7 ships `internal_rag_mcp`; the FastMCP wrapper translates the exception to `{"status": "inconclusive"}` so callers degrade safely.

2. `orchestrator_runtime/runtime.py` wiring: new `stage_4_anchor()` runs after Stage 0 and writes the result onto `ctx["reference_class_anchor"]`. `_build_stage_1_user_content` now injects a `## Reference-class anchor` section (rendered via `format_anchor_for_prompt`) when the anchor has signal — base rate + CI + median move + up to 5 similar resolved cases + an explicit calibration-discipline instruction telling the model to stay within ~30 points of the base rate unless asset-specific evidence supports the divergence. The single-shot `stage_1_synthesize` now delegates to `_build_stage_1_user_content` (was duplicating the prompt-building inline) so the ensemble path and single-shot path share one prompt format. Stage 7 constitutional now receives the real `reference_class_base_rate` (was `None`). Stage 10 persists `reference_class`, `reference_class_base_rate`, `similar_resolved_case_ids`, and applies the active isotonic curve at insertion (Stage 8) — `conviction_pct_calibrated` and the band derive from the calibrated value; `calibration_curve_version` is recorded on the row. Cold start (no fitted curve) is identity, so behavior is unchanged until a curve lands. Orchestrator version bumped `orch-v0.1.0-mvp` → `orch-v0.2.0-mvp`.

3. `conan-fda-orchestrator-plugin/mcp_servers/compute_mcp.py` — FastMCP server (`from mcp.server.fastmcp import FastMCP`) exposing `base_rate`, `similar_cases`, `isotonic_calibrate`, `brier`, `verify_claim`. Imports its logic from `modal_workers.shared.compute` so the runtime and the plugin can never drift. The runtime does NOT call this MCP server — it imports the Python module directly to avoid subprocess overhead on the critical path; the MCP wrapper is for Cowork bulk and operator-triggered tool use (Phase 4.7).

**Consequences.** Closes Stage 4 and Stage 8 in the orchestrator runtime. Stage 7's base-rate divergence check is now active. Behavior is backward-compatible: when `reference_class_base_rates` has no row for the asset (eval_harness is still skeletal — D-109 pending), the prompt skips the anchor section entirely rather than emitting `(unknown)`, the constitutional check skips the divergence check, and Stage 10 writes nulls into the new columns. When an isotonic curve has not been fitted (no row in `calibration_curves` with `is_active=true`), `apply_isotonic_calibration` is identity. Smoke tests pass: PAV correctly pools the (0.4=1, 0.5=0) violator into a single block at x=0.45 y=0.5; cold-start returns input unchanged; anchor formatter omits the section when neither base rate nor similar cases are available; `_build_stage_1_user_content` produces the same output as before when `ctx["reference_class_anchor"]` is None. Unblocks: D-109 eval_harness population (which feeds both `reference_class_base_rates` refit and the `similar_resolved_cases` corpus) and the post_mortem nightly job that will refit the active calibration curve from logged outcomes. Open: the MCP server import-fails without `pip install mcp[cli]` (Phase 4.7 dep); requirements.txt is not yet updated since the runtime path doesn't need it.

---

## D-115 — Stage 2 hypothesis enumeration + Stage 3 pre-mortem (2026-05-07)

**Context.** Phase 2 v0.2 shipped Stages 0/1/4/6/7/9/10. Stages 2 and 3 were left as TODOs in `runtime.py:21-22`. Without them, Stage 1's cited prose flows directly into Stage 9 with no enumeration of competing hypotheses and no adversarial pre-mortem — the v2-era "ITRK archetype" failure mode (correct facts, no named asymmetry, no kill conditions) leaks into v3 outputs and gets caught only post-hoc by the v2 thesis_challenger pattern from `20260423010000_thesis_challenger.sql`. Anthropic-internal eval research and the prior-art cited in plan `/Users/Pico/.claude/plans/stage-2-3-robust-wolf.md` identify explicit hypothesis enumeration + pre-mortem with strict sourcing as a 10–25% conviction-calibration improvement on contested cases.

**Decisions locked (D1–D5 in plan):**

- **D1: Pipeline placement.** Stage 2/3 runs ONCE on the ensemble winner (Stage 6 `cited_prose_winner`) — single Sonnet call each, mirroring Stage 7's placement. Per-run hypothesis dispersion is deferred; the Stage 6 ensemble already provides direction-distribution dispersion.
- **D2: All-falsified handling.** Soft signal: pipeline continues, `parsed_json["conviction_pct"]` is capped at `ALL_FALSIFIED_CONVICTION_CEILING = 30.0` (post-hoc enforcement in the run_one wrapper, not relied on the model). `convergence_assessments.pre_mortem_verdict='all_falsified'` and Stage 7 emits `severity=error` findings on the missing-citation path. No hard abort; no retry loop.
- **D3: Hypothesis count.** Variable 3–5, with at-minimum {bull, base, bear} required. Validator (`hypothesis._validate_and_parse_hypotheses`) raises `severity=error` on `too_few_hypotheses` or `missing_required_label`; ≥2 kill_conditions per hypothesis enforced as `severity=error`.
- **D4: Citation tokens.** Stay on prompt-engineered `[F:short]` / `[D:short]` notation. Citations API migration is a separate cross-cutting workstream; Stage 7's regex resolver is extended (`check_hypothesis_premortem_citations`) to walk Stage 2 mechanism strings + supporting/contradicting fact_id arrays + Stage 3 failure_mode `evidence_fact_ids`.
- **D5: Model.** Both stages use `DEFAULT_MODEL` (Sonnet 4.5 today; flips with the env var when Tier-2+ permits Opus 4.7). One call each — no per-hypothesis fan-out — keeps incremental cost ≈2× Stage 1.

**Schema (additive, idempotent).** Migration `20260508000000_v3_stage_2_3_hypothesis_premortem.sql` adds:

- `hypothesis_enumeration` table (assessment_id FK CASCADE, hypothesis_id, label, claim, mechanism, direction, supporting_fact_ids uuid[], contradicting_fact_ids uuid[], kill_conditions jsonb, prior_estimate_pct).
- `premortem_assessments` table (assessment_id + hypothesis_id composite FK to `hypothesis_enumeration`, verdict, failure_modes jsonb, disconfirming_searches jsonb, update_triggers jsonb), DEFERRABLE INITIALLY DEFERRED so Stage 10 can insert the parent rows then the children inside one logical transaction.
- `convergence_assessments`: `pre_mortem_verdict` text CHECK ∈ {all_survive, partial, all_falsified, skipped}; `surviving_hypothesis_ids text[]` NOT NULL DEFAULT `'{}'`. The pre-existing placeholder columns `hypotheses jsonb`, `pre_mortem text`, `adversarial_challenges jsonb` are now populated with denormalized summaries for dashboard rendering.

**Code.** New modules `orchestrator_runtime/hypothesis.py` and `orchestrator_runtime/premortem.py` follow the `constitutional.py` shape (dataclasses + a `run_*` entry point + a pure-function `_validate_and_parse_*` for unit testing). `runtime.py` `run_one` gains `enable_premortem: bool = True` (default on, `--no-premortem` CLI flag for cost-bounded backtests / regression escape). Stage 2 → Stage 3 fire between the parsed JSON output and the constitutional check; the post-hoc cap on `all_falsified` is applied in code. `stage_10_persist` resolves 8-char short ids back to full UUIDs via the existing `short_to_full` map and writes both new tables. `modal_workers/orchestrator_app.py::orchestrator_run_one` exposes `enable_premortem` so a Modal-side regression can be triaged with a single flag flip. Orchestrator version bumped `orch-v0.2.0-mvp` → `orch-v0.3.0-mvp`.

**Strict-sourcing enforcement (the load-bearing part).** Every claim in Stage 2 mechanism + every Stage 3 failure_mode requires `[F:short]` or `speculative: true`. Two layers: (1) the per-stage validator emits findings on missing citations; (2) Stage 7's deterministic citation-resolution pass is extended to walk the structured outputs and re-raise as `severity=error` — escalating the per-stage warnings to constitutional gates. Speculative reasoning is allowed in Stage 3 (pre-mortem inherently reasons beyond observed evidence) but must be flagged for auditability.

**Consequences.** Closes Stages 2 and 3. Single-shot pipeline cost rises ~2× (two extra Sonnet calls); ensemble path is unchanged in N (Stage 2/3 runs once on the winner regardless of `ensemble_n`). Unit-test smoke battery (12 tests in `orchestrator_runtime/eval_harness/fixtures/AXS-05/stage_2_3_test.py`) covers: validator happy path on the curated AXS-05 fixture; missing-required-label / too-few-hypotheses / missing-kill-conditions / unresolved-fact-id error paths; Stage 3 local-rollup override of model overall_verdict; non-speculative-without-citation gate; speculative allowance; constitutional walk over hypothesis mechanism + failure_mode citations; the Stage 9 cap math for {already-below, lowered-from-78, partial-no-op}. AXS-05 fixture pair (`stage_2_hypothesis_expected.json` + `stage_3_premortem_expected.json`) seeds the live regression replay. Open: the live Modal dry-run + 5-asset before/after conviction comparison are pending until the API key is reseeded for an actual orchestrator_run_one call. Hooks for downstream work: Stage 4 reference-class anchoring will overwrite `Hypothesis.prior_estimate_pct` with a base-rate-anchored value (additive — no schema change); Phase 5 sub-agents read `PreMortemResult.disconfirming_searches[]` to dispatch literature/competitive/regulatory_history queries; Citations API migration replaces the regex resolver with structured document content blocks across Stages 1/2/3/7 in one cross-cutting pass.

---

## D-116 — Forward-return labeling helper for export-bundle events (2026-05-07)

(Originally drafted as D-114; renumbered to D-116 because a parallel session also took D-114 / D-115 for orchestrator stage work. Same content, different number.)

**Context.** D-109 queued `scripts/seed_eval_harness_from_export.py` as a Phase 1-blocked work item. One half of that ETL — the forward-return labeling pass per export `methodology_spec.md §forward-return-windows` — does NOT depend on the documents table and can ship independently. The export's `binary_catalyst.json` contains 1502 events with `(ticker, filed_at)` but no realized outcomes; the labeling pass turns each into a `(returns at T+30/60/90/180/360, HIT/MISS verdict)` row. With D-112 (sponsor_resolver) closed, the only remaining D-109 unblocker beyond this is the documents table being populated for the `document_set uuid[]` join.

**Decision.** New module `modal_workers/scripts/label_forward_returns.py` (~340 LOC). Public API:

- `label_event(ticker, filed_at, profile, *, prefetch_closes=None, spy_closes=None, event_id=None) -> ForwardReturnLabel`
- `label_ledger(events, profile, *, limit=None) -> List[Dict]` — batch path for the export ledger shape
- CLI: `python -m modal_workers.scripts.label_forward_returns --events <json> --profile <bc|ag> --output <out>`

Mechanics:
- Anchor = last close strictly BEFORE `filed_at` (no look-ahead).
- Forward closes = first trading-day close at or after `filed_at + N` calendar days.
- Reuses `backfill_realized_move.fetch_daily_closes` for Polygon-then-yfinance fallback, `find_anchor_close`, `find_first_close_at_or_after`, `compute_move_pct`. No duplication of price-fetch logic.
- HIT thresholds per export: `binary_catalyst` HIT iff T+30 absolute return ≥ +20%; `activist_governance` HIT iff T+180 SPY-relative return ≥ +15%. SPY is pulled lazily only for `activist_governance`.
- Diagnostic miss_reason fields surface why a label is None/MISS — `no_price_data`, `no_anchor`, `t30_invalidated`, `t180_no_spy_relative`, `unparseable_filed_at`, `unsupported_profile`, `unresolved_ticker_sentinel:?` etc.
- Sentinel ticker handling: `?`, `PRIVATE_DISCARD`, `UNRESOLVABLE`, None all skip the price fetch and emit `hit=None` cleanly — keeps the batch path safe to run on the raw export ledger before D-112 sponsor resolution finishes.
- Edge cases per export methodology: ticker delisted before window completes → status='invalidated' (an upstream pass can re-classify involuntary delists as -100%); halted >30 days → invalidated; M&A close → invalidated (an upstream deal-terms pass handles cash/stock-deal labeling).

**Consequences.** Closes the labeling-pass half of D-109. Smoke battery (9 cases via injected synthetic closes — BC HIT, BC MISS, AG HIT, AG MISS, no_price_data, no_spy, unsupported profile, sentinel routing, unparseable date) all pass. yfinance is a declared dependency in `modal_workers/requirements.txt` (>=0.2,<0.3) so production runs through Polygon-or-yfinance like the rest of the price stack. The CLI is ready to chew the export's 1502-event binary_catalyst.json and emit a labels ledger; running it does not modify production state and is safe to schedule independently of D-109's eval_harness ingestion (which still waits on Phase 1 documents).

---

## D-117 — Stage 2/3 gate correctness: pre-cap raw_conviction + structural-error gate + safer base-direction default (2026-05-07)

**Context.** Audit of the Stage 2/3 implementation (commits landing alongside D-115) surfaced three semantic gaps. (1) The all_falsified cap mutated `parsed["conviction_pct"]` in place at runtime; `stage_10_persist` then read the post-cap value into `raw_conviction_pct`, contaminating the column whose schema comment is "pre-calibration (Stage 5/6 output)". The original ensemble conviction was lost from the row, only visible inside `stage_3_premortem` metric notes. (2) Stage 2/3 emit severity=`error` findings (missing required label, <2 kill_conditions, missing verdict, parse failure, etc.) that flow into `stage_metrics.notes` but were NOT propagated into `constitutional_result.pass_` — Stage 7 walked citations only, so a Stage 2 with a missing required label or zero kill_conditions could still mark `constitutional_pass=true`. (3) When a `label='base'` hypothesis arrived with no valid `direction`, the validator silently coerced it to `'bullish'` — biasing downstream EV math.

**Decision.** Three local fixes:
- **Pre-cap raw_conviction:** before mutating `parsed["conviction_pct"]` on `all_falsified`, stash the pre-cap float on `ctx["pre_premortem_conviction"]` and set `ctx["conviction_capped_by_premortem"]=True`. `stage_10_persist` now reads this and writes the pre-cap value as `raw_conviction_pct` (the cap flows into `conviction_pct_calibrated` and `conviction_pct` only). Adds an audit boolean `evidence_ledger.conviction_capped_by_premortem` to the row's jsonb so the dashboard can surface "cap fired" without parsing stage metrics.
- **Structural-error gate:** `run_constitutional_check` now merges Stage 2/3 severity=`error` findings into its own `findings` list (renamed `stage_2_<check>` / `stage_3_<check>`) and includes them in the `pass_` computation. Warnings/info still don't gate.
- **Base-direction default:** for `label='base'` with invalid `direction`, default to `'event_specific'` (NOT `'bullish'`) and emit a `missing_direction_for_base` warning finding.

**Consequences.** Closes #1, #4, #9 of the Stage-2/3 review. `raw_conviction_pct` is now genuinely the Stage 5/6 output for every row; observers can tell from `evidence_ledger.conviction_capped_by_premortem` whether Stage 3 fired. Constitutional pass_ now reflects the assessment's actual deliverability — a Stage 2 that omitted `bear` or a Stage 3 that omitted a verdict cannot ship as `constitutional_pass=true`. Test coverage for these in D-121.

---

## D-118 — Stage 4 anchor → Stage 2 + post-output prior renormalization (2026-05-07)

**Context.** Stage 4 (D-114) populated the reference-class anchor and rendered it into the Stage 1 prompt, but Stage 2 was blind to it. The Stage 2 system prompt said `prior_estimate_pct` values would be "renormalized by Stage 4" — an unmet promise, since Stage 4 had already run by the time Stage 2 fired and no code touched priors after Stage 2. So the model picked priors with no empirical anchor.

**Decision.** Two-part:
- **Thread anchor into Stage 2 user content** — `_build_stage_2_user_content` now takes `anchor` (read from `ctx["reference_class_anchor"]`) and renders the same `format_anchor_for_prompt` block that Stage 1 sees. (Subsequently moved to the cached system prefix in D-119, but the threading lands here.)
- **Implement actual renormalization** — new module-scope `renormalize_priors(hypotheses, anchor, evidence_quality)` in `orchestrator_runtime/hypothesis.py`. Linear blend `final = (1 - w) * raw + w * target` where `w = max(MIN_ANCHOR_WEIGHT, 1.0 - evidence_quality)` (floor 0.20 so even high-evidence assets get some pull). `target` is `base_rate * 100` for bull, `(1 - base_rate) * 100` for bear, raw value for base/event_specific. Rescale post-blend so sum ≈ 100. Called from `run_one` immediately after Stage 2 returns; per-hypothesis pre/post values stashed in `stage_2` metric notes for observability.

**Schema:** new migration `20260509000000_v3_d118_hypothesis_prior_pre_anchor.sql` adds `hypothesis_enumeration.prior_estimate_pct_pre_anchor int` (nullable, CHECK 0..100). `Hypothesis` dataclass gains a matching field; the parser snapshots the model-emitted value into it during validation. The post-anchor value continues to live in `prior_estimate_pct`. A/B-able by reading `prior_estimate_pct_pre_anchor` instead.

**Consequences.** Closes #2, #3 of the Stage-2/3 review. Bull priors now anchor to base_rate × 100 weighted by `(1 - evidence_quality)`, bear priors to `(1 - base_rate) × 100`, base/event_specific to model output rescaled. Smoke: bull=70/base=20/bear=10 with rate=0.30 / eq=0.5 produces post-blend bull=45/base=18/bear=36 (sum=99). Cold start (no anchor / no base rate / empty hypotheses) is identity. Migration is additive + idempotent; safe to apply against live Phase 0/1 schema.

---

## D-119 — Cross-stage prompt caching via shared system prefix (2026-05-07)

**Context.** A single assessment with `ensemble_n=7` makes ~9 Sonnet calls (7× Stage 1 + 1× Stage 2 + 1× Stage 3 + 1× Stage 7 semantic), each sending the same ~10-30k tokens of asset preamble + Stage 4 anchor + structured fact layer. No prompt caching was wired, so input tokens were paid in full on every call.

**Decision.** Lift the asset preamble + anchor + fact layer into a **shared system prefix** sent as the FIRST system block of every stage in the assessment, with `cache_control: {type: "ephemeral"}`. Per-stage instructions (`STAGE_1_SYSTEM`, `STAGE_2_SYSTEM`, `STAGE_3_SYSTEM`, `SEMANTIC_SYSTEM_PROMPT`) become the SECOND system block — they differ across stages but come AFTER the cache marker, so they don't invalidate the cached prefix.

Implementation:
- New `runtime.build_shared_system_prefix(ctx) -> str` builds the cacheable content once per assessment.
- New `runtime.build_system_blocks(prefix, stage_system) -> List[Dict]` constructs the two-block system list with the cache marker.
- `_build_stage_1_user_content` no longer renders asset/anchor/facts (now in system); user content is docs + memory + "produce" instruction only.
- `_build_stage_2_user_content` and `_build_stage_3_user_content` similarly stripped of duplicated facts.
- `run_hypothesis_enumeration`, `run_premortem`, and `check_semantics` accept `system_blocks`/`semantic_system_blocks` kwargs; when provided, used as system; when None, fall back to the original string for backward compat.
- `ensemble.py` widens `stage_1_system: Any` so the same blocks list flows through the streaming + batch ensemble runners (the SDK accepts list-of-blocks anywhere it accepts a string).
- Orchestrator version bumped `0.3.0` → `0.4.0`.

**Consequences.** Closes #5. Within an assessment all stages run within ~1-2 minutes — well under the 5-minute cache TTL. Stage 1 ensemble run 1 pays cache-creation; runs 2-7 hit cache on system-prefix at 10% input cost. Stage 2/3/7 also cache-hit because their first system block is byte-identical. Expected savings on a typical 15k-token shared prefix: ~80% input-token reduction across the assessment. Backward compatible: omit `system_blocks` and behavior is unchanged. Test `test_shared_prefix_is_byte_identical_across_stages` locks the cache invariant — any drift in the prefix builder fails CI.

---

## D-120 — Stage 2/3 polish: raw_response audit head + pre_mortem text caps (2026-05-07)

**Context.** Two minor polish items from the Stage 2/3 review: (#6) Stage 2's `_build_stage_2_user_content` accepted `docs` but never used it (stale parameter); (#7) `HypothesisResult.raw_response` and `PreMortemResult.raw_response` captured the full model text but were never persisted, so when validators rejected parts of the response the raw text was lost; (#8) `pre_mortem_summary` text rendered into `convergence_assessments.pre_mortem` was not bounded — a verbose Sonnet response could produce a multi-MB row.

**Decision.**
- (#6 — already eliminated as a side effect of D-119; the unused `docs` param disappeared when user content was simplified.)
- (#7) Stash `raw_response[:4000]` onto `stage_2.notes.raw_response_head` and `stage_3.notes.raw_response_head`. No schema change — `assessment_stage_metrics.notes` is jsonb. 4kb is enough to debug parser disagreements without bloating the metrics table.
- (#8) Cap each pre_mortem failure-mode line at 500 chars and the total `pre_mortem_summary` at 8000 chars before insertion.

**Consequences.** Closes #6, #7, #8. Audit/debug paths now have model-output context for failed Stage 2/3 calls without unbounded row sizes. No row format change; existing dashboard reads are unaffected.

---

## D-121 — Stage 2/3 test coverage: validators + renormalizer + cache-prefix invariants (2026-05-07)

**Context.** Stage 2/3 are heuristic-heavy (JSON validators, label coercion, citation walking, local rollup) and prompt-design-sensitive (cache invariants depend on byte-identical shared prefix). Without tests, regressions land silently — especially the local-rollup discipline at premortem.py:298-327 (the model is observed, not trusted) and the cache-prefix invariant from D-119.

**Decision.** Three new test files under `orchestrator_runtime/tests/`:

- `test_hypothesis.py` — 19 tests for `_validate_and_parse_hypotheses` (parse failures, missing required labels, <2 kill_conditions, OOB priors clamped, D-117 base→event_specific coercion, unresolved fact_id warning, 5-cap, pre-anchor prior snapshot) and `renormalize_priors` (pulls bull down on low base rate, pulls bull up on high base rate, sum stays near 100, MIN_ANCHOR_WEIGHT floor, no-anchor identity, no-base-rate identity, evidence_quality None default, evidence_quality invalid fallback, empty-hypotheses safe).

- `test_premortem.py` — 11 tests for `_validate_and_parse_verdicts` (parse failures, model-claimed `all_survive` overridden by local rollup when one verdict is falsified, surviving_ids mismatch emits info finding, non-speculative failure mode without evidence is severity=error, speculative-without-evidence is allowed, unresolved evidence_fact_id warns, missing verdict for known hypothesis raises error, unknown hypothesis_id skipped, invalid verdict defaults to weakened).

- `test_runtime_stage_2_3.py` — 9 integration tests: cache-prefix byte-identity across Stage 1/2/3/7 system blocks (the D-119 invariant); cache prefix contains facts/anchor/asset; Stage 1/2/3 user content omits facts; D-117 Stage 2/3 structural errors flip `constitutional_pass_` to False; D-117 warnings don't gate; constitutional walks hypothesis mechanism citations.

**Consequences.** 39 tests, all passing. Locks the structural invariants behind regressions: any future drift in the renormalize formula, the local-rollup discipline, the structural-error gate, or the cached prefix's byte-shape will fail in CI before it ships. Tests are self-contained — no DB / API calls — and run in <1 second.

---

## D-122 — Stream 1: operator delivery rebind (reactor + fanout for v3) (2026-05-07)

**Context.** The Tier 0 gap audit (this session) confirmed that even after Stream 3's Stage 10 produces `convergence_assessments` rows with `band='immediate'`, no email reaches operators: the reactor still ran v2 `classifyGroup` / bonus stamping / `clearDisplacedWinners` against the legacy `signals` flow only, and fanout subscribed only to `alerts.INSERT` / `candidate_events.INSERT`. v3's alert path was silent end-to-end. Pre-flight verification also surfaced live-DB drift: the 5 D-111 RPCs in `20260506000020_v3_dashboard_rpcs.sql` were authored but **not applied** to production (only `fda_calibration_activate` / `fda_calibration_rollback` were live).

**Decision.** Three local edits + two migrations + two edge-function deploys, all behind a coexistence rule that preserves v2 traffic for non-FDA verticals:

1. **Reactor refactor** ([supabase/functions/reactor/index.ts](supabase/functions/reactor/index.ts)) — top-level dispatch on `payload.table`. `signals` keeps the legacy v2 path with one short-circuit added: `binary_catalyst` and `fda_event` profile signals return `{skipped: "fda_profile_routed_to_orchestrator"}` instead of running classifyGroup, because their orchestration runs through ingestion → `documents` → `asset_documents` → orchestrator queue. New `asset_documents` branch calls `processAssetDocument()` which derives `trigger_type` (`cross_source` if a sibling primary doc exists in the prior 24h, else `new_doc`) and inserts a row into `orchestrator_runs` via `buildOrchestratorRunInsert()` (extracted into [orchestrator-enqueue.ts](supabase/functions/reactor/orchestrator-enqueue.ts) as a pure helper for testability — Contract C1 lock).

2. **Fanout extension** ([supabase/functions/fanout/index.ts](supabase/functions/fanout/index.ts)) — new fourth entry point D for `convergence_assessments` INSERT or UPDATE-into-immediate. `dispatchAssessmentImmediate()` loads the asset + entity, renders a v3 HTML/text template (`[IMMEDIATE] TICKER · DIRECTION conviction% [low–hi] · trigger`, conviction display + ensemble dispersion + reference class + base rate + EV + thesis + top 5 cited blocks + dashboard link), uploads the audit body to Storage `reports/assessments/YYYY/MM/<id>.html`, and dispatches via Resend. Realtime broadcasts on `assessments` and `asset:<id>` channels. v2 `alerts.INSERT` (audit-only) and `candidate_events.INSERT` (pre-edge promotion) paths preserved unchanged for non-FDA verticals. `deliveries.ts` extended with `assessment` subject kind + `assessment_id` field on `DeliveryRow`; mutual-exclusion preserved (one parent column populated per row).

3. **Migration `v3_alert_triggers`** — `alert_deliveries.assessment_id uuid REFERENCES convergence_assessments(id) ON DELETE CASCADE`, partial unique index on `(assessment_id, channel, target) WHERE assessment_id IS NOT NULL` for permanent dedup (band-flip re-emits don't re-email). `call_fanout_assessment()` + AFTER INSERT trigger `WHEN (NEW.band='immediate' AND NEW.superseded_by IS NULL)` + AFTER UPDATE companion `WHEN (NEW.band='immediate' AND OLD.band IS DISTINCT FROM 'immediate')` so band-flips into immediate fire once. `call_reactor_assetdoc()` + AFTER INSERT trigger `WHEN (NEW.link_type='primary' AND NEW.is_material=true)`. Both dispatchers use the established vault-secret + `net.http_post` 30s pattern from `call_reactor()` / `call_fanout()`. Plus partial unique index `orchestrator_runs_pending_dedup_idx (asset_id, trigger_type, COALESCE(trigger_doc_id, '00000000-...'::uuid)) WHERE status='pending'` so the reactor's `INSERT … ON CONFLICT DO NOTHING` collapses 10-min-bucket bursts.

4. **D-111 RPC re-apply.** Discovered the 5 dashboard RPCs (`fda_asset_set_watch_priority`, `fda_asset_set_active`, `fda_asset_pin_reference_class`, `eval_case_open`, `eval_case_resolve`) plus `eval_harness.opened_at/resolved_at/resolution_outcome` columns were absent live despite the file existing. Re-applied the migration as `v3_dashboard_rpcs_reapply` (idempotent CREATE OR REPLACE / ADD COLUMN IF NOT EXISTS).

**Original migration also surfaced an IMMUTABLE issue.** First attempt used `(date_trunc('day', created_at))` in the dedup index expression. `date_trunc(text, timestamptz)` is STABLE not IMMUTABLE — Postgres rejected with 42P17. Switched to a permanent (no-day-partition) dedupe; semantic difference is appropriate for v3 (`assessment_id` uniquely identifies one orchestrator pass; if a band-flip UPDATE fires the trigger again, no new email).

**Tests.** 25/25 green. New `deliveries.test.ts` cases for the assessment subject kind + mutual-exclusion + back-compat. New `orchestrator-enqueue.test.ts` (4 tests) pinning the C1 row-shape contract. Existing reactor/fanout tests untouched, all still pass.

**Consequences.**

- v3 alert delivery is end-to-end live: ingestion → `asset_documents` INSERT → reactor enqueues `orchestrator_runs` (Contract C1) → Stream 3's drainer picks up → assessment lands with `band='immediate'` → fanout fires email + Realtime broadcast.
- 5 D-111 dashboard RPCs are now callable; Stream 8 (dashboard wiring) can un-stub the `setWatchPriority` / `setActive` / `pinReferenceClass` / `eval_case_*` action handlers without further migration work.
- Coexistence rule preserved: 4 v2 operational scanners (`edgar_filing_monitor` activist_governance, `fda_pdufa_pipeline` binary_catalyst, `pre_phase3_readout_scanner` binary_catalyst, `takeover_candidate_scanner`) keep running; reactor's profile-branch routes their FDA-typed signals to the orchestrator queue while non-FDA verticals run the legacy convergence flow unchanged.
- Edge function versions: reactor v11→v12, fanout v7→v8 (both `verify_jwt=false` per memory `reactor_deploy_no_verify_jwt.md`).
- Rollback path: DROP the two new triggers + functions + the `assessment_id` column. Legacy paths unaffected.

---

## D-123 — Stream 2: closed feedback machinery (post-mortem runner + rollback monitor + isotonic refit) (2026-05-07)

**Context.** Post Tier 0 audit, Phase 8 (closed feedback loop) was the largest remaining substrate gap: `post_mortem_queue` rows accumulated from Stage 10 but had no drainer; D-104's rollback monitor was a queued spec item with no code; D-103's paired-bootstrap calibration gate was schema-only. Without these, the v3 thesis ("system improves over time") cannot compound. Pre-flight verification also surfaced: `signal_price_snapshots` is empty (price_tracker may itself be broken — separate issue), 0 `fda_assets` in resolved program states (so FDA-status outcome resolution is moot for current assets — fall back to forward-return verdict via D-116).

**Decision.** Three new Python modules (post-mortem runner, rollback monitor, calibration refit) + one Modal app file (`feedback_loop_app.py`) + one migration (`v3_feedback_loop`) + three test suites (65 tests, all green).

1. **`modal_workers/shared/post_mortem_runner.py`** — drains `post_mortem_queue` rows where `outcome_window_end < now()` and `status='pending'`. Per row: looks up the assessment + `fda_asset` for ticker/filed_at/reference_class, calls D-116's `label_event(ticker, filed_at, profile='binary_catalyst')` to get the realized outcome (T+30/60/90/180 returns + HIT/MISS verdict + miss_reason). When `hit is None` (delisted/halted/no_anchor/sentinel ticker): persists `status='no_outcome'` and skips. Otherwise: computes `prediction_error = predicted_conviction_pct − realized_outcome_score` (signed pp delta where `realized_outcome_score` maps `(direction × hit) → 0|50|100`), invokes Haiku 4.5 for a 200-word retrospective, and writes back `status='post_mortem_complete'`, `realized_outcome jsonb`, `post_mortem_text`, `prediction_error`. Then refits `reference_class_base_rates` UPSERT for the assessment's class (Wilson 95% CI from successes/n, median T+30 return from the resolved cohort), and appends a "Resolved post-mortems" entry to the per-asset memory file at `memory_files/asset_<id>.md` per Contract C5 (idempotent on `<!-- assessment:<id> -->` marker; injects the section if missing).

2. **`modal_workers/scripts/nightly_calibration_refit.py`** — pulls `(raw_conviction_pct/100, direction_aligned_outcome, asset_id)` triples from every `post_mortem_complete` row, fits a fresh isotonic curve via `compute.fit_isotonic_curve` (PAV, no scikit-learn dep), and evaluates the D-103 5-condition gate by computing both prod-curve and new-curve predictions on the same set: `n ≥ 200`, `brier_delta_vs_prod > 0`, `paired_bootstrap_p < 0.05` (10k resamples by default), `ranking_auc_delta_vs_prod ≥ 0.05`, `max_single_asset_contribution_pct ≤ 5.0`. Always writes the candidate curve (`is_active=false` initially) plus an `eval_runs` row with the gate decision + inputs (D-104 snapshot policy: prior curve stays in `calibration_curves` so rollback can flip back). Auto-promotes to `is_active=true` only when both `gate.passed=true` AND env `ENABLE_PROMOTION=true`; otherwise leaves the candidate dormant for manual operator promotion via the existing `fda_calibration_activate(p_version, p_note)` RPC.

3. **`modal_workers/scripts/rollback_monitor.py`** — D-104 daily Spearman drift check. Fetches every `post_mortem_complete` row's `(realized_30d_return_pct, conviction_pct_calibrated)` pair within the last 30 days; computes Spearman correlation (with average-rank tie handling, no scipy dep). Compares vs the previous monitor pass's correlation from `calibration_drift_log`. Fires rollback iff `n ≥ 30` AND (`corr < 0.20` OR `Δcorr ≤ −0.15`); when triggered, finds the most recently fitted prior curve via `calibration_curves.fitted_at DESC` and atomically flips `is_active`, then inserts an `operator_flag(severity=critical, source=rollback_monitor)` and a `calibration_drift_log` row. Conservative defaults: `n < 30` short-circuits with `rollback_reason='below_min_n'`; if no prior curve exists, demote-only (deactivate current, no new active curve).

4. **Migration `v3_feedback_loop`** — `prompt_versions` table (D-104 append-only with partial unique `is_active per stage` index), `calibration_drift_log` table for the monitor's audit trail, plus the `memory_files` Storage bucket + RLS policies (service-role full access, authenticated read) so the post-mortem memory writes have a backing store. Bucket creation idempotent via `INSERT ... ON CONFLICT DO NOTHING`.

5. **Modal app `conan-v3-feedback-loop`** ([feedback_loop_app.py](modal_workers/feedback_loop_app.py)) — single chained function `daily_feedback_loop` that runs drain → monitor → refit in order, each step caught so a failure in one doesn't gate the others. Two operator dry-run callables (`post_mortem_drain_dry_run`, `rollback_monitor_dry_run`) for live-state inspection without writes.

**Modal cron limit hit.** Free tier caps cron jobs at 5; conan-v2 already uses all 5. Initial deploy with three `@modal.Cron` decorators failed; collapsed into a single chained function still hit the cap because v2 has 5/5. Final deploy ships `daily_feedback_loop` as on-demand callable (no `@modal.Cron`). External scheduling options: (a) upgrade Modal plan and re-add the schedule one-liner, (b) Supabase pg_cron via `_conan_modal_post`, (c) Pedro's Cowork scheduled tasks. Functions are deployed and callable today; scheduling is a configuration follow-up.

**Anthropic secret coexistence.** `anthropic-orchestrator` Modal secret was authored in `orchestrator_app.py` but never created in the workspace (Modal CLI confirms only `compute-auth` / `courtlistner` / `supabase-secrets` / `scanner-secrets` exist). Stream 2 wires Haiku post-mortem text generation through `scanner-secrets` (which v2's thesis-writing functions already use, so it most plausibly contains an `ANTHROPIC_API_KEY`). If absent, post_mortem_runner's text-generation try/except catches the failure and falls through to a deterministic `[auto-fallback]` narrative; outcome resolution + prediction_error + base-rate refit + memory file write all still complete. Stream 3 will create the dedicated secret; this stream's `secrets=[...]` lists swap to it then.

**Tests.** 65 / 65 green across three suites: 23 for the post-mortem runner (outcome score matrix, Wilson interval edges, median, memory-file merge idempotency + section injection), 24 for calibration refit (paired-bootstrap edge cases, AUC monotonicity + ties, all 5 D-103 gate failure modes + pass path, per-asset Brier contribution), 18 for rollback monitor (Spearman correctness on monotonic / inverted / random / tied / degenerate inputs, drift classification across all branches). All pure-helper tests — no DB / network — run in <0.2s.

**Consequences.**

- Phase 8 substrate is live: when Stream 3 starts producing closed-out predictions (assessment + window pass + outcome resolution), the daily chain refits the curve, gates via D-103, and rolls back on drift.
- Until ≥30 resolved signals exist, the rollback monitor short-circuits with `below_min_n`; until ≥200, the calibration refit gate fails with `n_too_low` (both correct behaviors — system stays on cold-start identity curve).
- Memory file format (Contract C5) is pinned in `_merge_memory_file()` — Stream 3's Stage 10 owns `## Active hypotheses`, `## Open uncertainties`, `## Recent assessments` sections; Stream 2 owns `## Resolved post-mortems` (append-only newest-first, idempotent on assessment_id marker).
- Modal app `conan-v3-feedback-loop` deployed; secrets configured; functions callable. Scheduling is the only remaining step before automation.
- Rollback path: DROP `prompt_versions` + `calibration_drift_log` tables, DELETE the `memory_files` Storage bucket, stop the Modal app. Existing `calibration_curves` / `post_mortem_queue` / `reference_class_base_rates` rows untouched.
