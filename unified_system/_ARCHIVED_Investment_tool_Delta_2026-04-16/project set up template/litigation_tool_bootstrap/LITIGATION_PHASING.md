# PHASING — Build Sequence and Milestones (Tool 3)

This file translates the priority queue into a concrete phase-by-phase plan. Each phase has: scope, success criteria, and the next-phase gating milestone. Phases are not rigidly time-boxed — they move when success criteria are met, not when a calendar says so. That said, rough duration estimates are included for planning.

Each phase's first action is **endpoint validation**: live-probe every source in scope and upgrade entries in `CONTEXT.md` from `⚠️ UNVERIFIED` to `✅ VERIFIED` per PROJECT_TEMPLATE Part 13. Never code against an assumed endpoint schema.

---

## Phase 0 — Project instantiation (Days 1–3)

### Scope

Stand up the project skeleton. No scanners. No signals. Every file the PROJECT_TEMPLATE demands, in its initial state.

### Actions

1. Create `litigation_system/` and `reporting_layer/` folders per PROJECT_TEMPLATE Part 2.
2. Populate the ten relay files per PROJECT_TEMPLATE Part 3 (README, PROJECT_INSTRUCTIONS, INSTRUCTIONS, OBJECTIVES, CONTEXT, SESSION_STATE, SESSION_LOCK, PROGRESS_LOG, INDEX, DECISIONS, OPEN_QUESTIONS). OBJECTIVES and CONTEXT copy verbatim from the bootstrap folder. DECISIONS copies D-000 through D-012 from the decisions seed.
3. Create `framework/scoring_system.md` from `LITIGATION_SCORING.md`.
4. Create `framework/candidate_template.md` — use Tool 1 / Tool 2 candidate template as the source, adapted with litigation-specific deep-dive sections from `LITIGATION_STRATEGIES.md`.
5. Create `strategies/` with six files: `strategy_federal_civil.md`, `strategy_itc_337.md`, `strategy_ptab_ipr.md`, `strategy_delaware_chancery.md`, `strategy_sec_enforcement.md`, `strategy_doj_ftc_antitrust.md`. Each file is extracted verbatim from `LITIGATION_STRATEGIES.md`.
6. Create empty `tools/`, `signals/`, `candidates/` (with `delivered/` and `archive/` subfolders), `reports/`, `working/`, `research/`, `archive/`, `baselines/`.
7. Create `reporting_layer/` substructure: `performance_reports/`, `litigation_briefs/docx/`, `litigation_briefs/pdf/`, `litigation_briefs/index.json` (empty array), `working/`, `archive/`.
8. Register scheduled tasks per D-012. **Do not enable them yet** — leave them scheduled but inactive.

### Success criteria

- [ ] All files exist and pass a structural sanity check (INDEX.md enumerates them all; every markdown file is valid; SESSION_LOCK.md reads "UNLOCKED"; SESSION_STATE.md accurately reflects Phase 0 build state).
- [ ] Cold-start read order works: a brand-new session can read SESSION_STATE + INSTRUCTIONS and correctly identify the next action as "begin Phase 1."
- [ ] DECISIONS.md contains D-000 through D-012 verbatim from the seed.

### Gating milestone to Phase 1

Manual cold-start test: open a fresh Cowork session, point it at `litigation_system/`, verify it correctly identifies Phase 1 as the next work block.

---

## Phase 1 — Endpoint validation + entity resolution scaffolding (Weeks 1–2)

### Scope

Validate every endpoint in `CONTEXT.md`. Build the two-stage party-resolution module. Build the executive-lookup table generator.

### Actions

1. Live-probe every endpoint in `CONTEXT.md`. Record status, response shapes, rate-limit observations. Upgrade table entries to `✅ VERIFIED`. Any that fail probe: open Q-001, Q-002, … in `OPEN_QUESTIONS.md` and continue with verified ones.
2. Build `tools/party_resolver.py` implementing D-003's two-stage protocol. Start with the internal cache empty.
3. Build `tools/executive_lookup_builder.py` — parses DEF 14A filings for in-universe companies, extracts named executive officers and directors, writes to `baselines/executive_lookup.json`. Run once to populate initial table.
4. Build `tools/build_exhibit21_map.py` — parses 10-K Exhibit 21 filings across the universe, builds subsidiary-name → parent-CIK mapping, writes to `baselines/exhibit21_map.json`. Run once to populate.
5. OpenFIGI resolver: reuse from Tool 1 if available; otherwise build as its own module.
6. Validation: run the party resolver against a manually-labeled test set of 100 case captions spanning all six channels. Target precision ≥ 80% at confidence ≥ 0.85. Target recall ≥ 70% overall.

### Success criteria

- [ ] All endpoints that will be used in Phase 2+ are VERIFIED.
- [ ] Party resolver passes precision/recall targets on the 100-case validation set.
- [ ] Executive lookup populated for all in-universe companies (≥ 6,500 entries).
- [ ] Exhibit 21 map populated (≥ 50,000 subsidiary→parent entries).
- [ ] `tools/` directory passes `py_compile` and Tool Validation Protocol on every file.

### Gating milestone to Phase 2

Party resolver demonstrated on live docket data: pull 20 real docket entries from the last 7 days across all six channels, manually verify the resolver's output on each. Precision ≥ 80%.

---

## Phase 2 — First scanner: PACER/RECAP Federal Civil (Weeks 2–4)

### Scope

Build the highest-volume, highest-signal channel first. It exercises party resolution, signal schema, scoring, triage, and pipeline integration end-to-end.

### Actions

1. Build `tools/pacer_recap_scanner.py` per `strategy_federal_civil.md`.
2. Wire into a minimal `tools/pipeline_runner.py` that: runs the scanner, passes signals through party resolution, through triage filters, through scoring, writes surviving signals to `signals/`. No convergence engine yet — single channel.
3. Register `litigation-operational` as ENABLED (every 6h). `litigation-maintenance` ENABLED (50 min after).
4. Run for 3 consecutive days, producing daily reports into `reports/`.
5. Manually review every scored signal. Calibrate triage and scoring thresholds.

### Success criteria

- [ ] Scanner runs end-to-end without crashes across 12 consecutive scan cycles.
- [ ] Signal quality: of ~100 raw signals produced in 3 days, ≥ 30% survive triage; ≥ 10% score 22+; ≥ 2 score 28+ with defensible deep dives.
- [ ] Daily reports readable and actionable.
- [ ] First real candidate writeup produced (if a 28+ signal appears).

### Gating milestone to Phase 3

Three consecutive days of autonomous operation without manual intervention, producing at least one validated 28+ candidate.

---

## Phase 3 — Add ITC 337 and PTAB IPR scanners (Weeks 4–5)

### Scope

Add the two patent-litigation channels. They share some party-resolution patterns (patents name assignees; assignees need to be resolved to issuers).

### Actions

1. Build `tools/itc_337_scanner.py` per `strategy_itc_337.md`.
2. Build `tools/ptab_ipr_scanner.py` per `strategy_ptab_ipr.md`.
3. Add both to `pipeline_runner.py`. Still no convergence engine.
4. Run for 3 consecutive days producing daily reports.

### Success criteria

- [ ] Both scanners run without crashes.
- [ ] Signal quality comparable to Phase 2.
- [ ] Zero cross-channel corruption (a PACER signal doesn't accidentally get a PTAB `signal_category`).

### Gating milestone to Phase 4

Both scanners in continuous operation for 3 consecutive days.

---

## Phase 4 — Convergence engine (Week 5)

### Scope

Build the convergence engine. It is meaningful only once there are multiple channels; Phase 3 sets up the conditions.

### Actions

1. Build `tools/convergence_engine.py`. Keys on `issuer_figi`. 30-day rolling window. Emits convergence-bonus annotations to the signal records.
2. Integrate into `pipeline_runner.py` — convergence check runs after all scanners have produced signals in a given cycle.
3. Add convergence detection to the daily report (its own top-priority section).

### Success criteria

- [ ] Convergence engine correctly identifies ≥ 2-channel overlap on test data.
- [ ] Zero false convergences (two signals attributed to the same issuer that are actually different entities).
- [ ] Convergence annotations appear in daily reports.

### Gating milestone to Phase 5

At least one genuine (manually-verified) convergence produced from live data.

---

## Phase 5 — Add Delaware Chancery, SEC Enforcement, DOJ/FTC scanners (Weeks 6–8)

### Scope

Add the remaining three channels. These are the hardest for entity resolution (Chancery captions often use individual plaintiffs; SEC enforcement names executives; DOJ/FTC announcements reference deal parties at varying specificity).

### Actions

1. Build `tools/delaware_chancery_scanner.py` per `strategy_delaware_chancery.md`. This is the hardest scanner because it's HTML-scraping-only and the site is slow. Build with retry and backoff.
2. Build `tools/sec_enforcement_scanner.py` per `strategy_sec_enforcement.md`. Piggyback on Tool 1's EDGAR infrastructure if available.
3. Build `tools/doj_ftc_antitrust_scanner.py` per `strategy_doj_ftc_antitrust.md`.
4. Integrate all three into the pipeline. Convergence engine now sees six channels.

### Success criteria

- [ ] All six scanners run in the same scheduled cycle without exceeding wall-clock budget (per-scanner 45s soft limit, 120s hard-kill).
- [ ] Party resolver handles the Chancery edge cases (caption with individual plaintiff, "In re" captions, "Board of Directors" as party).
- [ ] Executive-lookup-driven resolution working for SEC enforcement executive-respondent signals.

### Gating milestone to Phase 6

All six scanners in continuous operation for 5 consecutive days. Convergence engine producing meaningful cross-channel signals.

---

## Phase 6 — Reporting layer (Weeks 8–9)

### Scope

Build the `reporting_layer/` deliverables. These are read-only on the system folder and write to `reporting_layer/`.

### Actions

1. Register `litigation-performance-report` (daily 1:30am): build a PDF dashboard per PROJECT_TEMPLATE Part 5.3 — cover, exec summary with KPIs, per-channel signal production, API reachability heatmap, convergence activity, candidate pipeline, code health.
2. Register `litigation-deep-dives` (every 8h): for each 28+ candidate, produce a full docx brief and PDF, write to `reporting_layer/litigation_briefs/`. Use the dedup registry in `index.json` — regenerate only on source-hash change or material new finding.
3. PDF generation: use `reportlab` directly, not a docx→pdf chain (known failure mode per template Part 5.3).

### Success criteria

- [ ] Daily performance report generated and readable.
- [ ] Deep-dive briefs produced for every 28+ candidate; dedup registry works (no duplicates; regenerations only when warranted).
- [ ] Reporting-layer tasks never write into `litigation_system/` (write-scope isolation preserved).

### Gating milestone to Phase 7

Reporting layer in continuous operation for 3 consecutive days.

---

## Phase 7 — Autonomous operation validation (Weeks 9–10)

### Scope

Let the full system run for 7 consecutive days with zero manual intervention. Observe failure modes. Fix.

### Actions

1. Stop manual-run mode. Let all four scheduled tasks fire on schedule.
2. Each day, review but do not modify. Log any observed failure in `OPEN_QUESTIONS.md` (via maintenance session or at next interactive session).
3. At end of day 7, review `PROGRESS_LOG.md`, `OPEN_QUESTIONS.md`, and the maintenance task's audit records. Triage any open issues.

### Success criteria

- [ ] 7 consecutive days, zero manual interventions.
- [ ] No session runs past 4 hours.
- [ ] No lock failures.
- [ ] At least 5 candidates produced (any score); at least 1 at 28+.
- [ ] All four scheduled tasks' write-scope discipline maintained.
- [ ] Self-review checklist (PROJECT_TEMPLATE Part 12) passes on every candidate.

### Gating milestone to Phase 8

7-day autonomous run succeeds. Tool 3 v1 is **done**.

---

## Phase 8+ — Post-v1 evolution (open-ended)

Only opened after Phase 7 succeeds.

**Candidate scopes for Phase 8+:**
- Bankruptcy courts (v2 scope — see D-007).
- State courts beyond Delaware (TX, CA, NY as starting points).
- Federal criminal (re-evaluate signal-to-noise once DOJ/FTC channel is mature).
- Non-US litigation (UK Commercial Court first; Phase 10+).
- Cross-tool analyzer project that reads Tool 1, Tool 2, Tool 3 candidates and emits cross-tool convergence.
- Sealed-filing detection (identifying docket entries marked sealed — the fact of sealing is itself sometimes a signal).
- Judge-effect modeling (per-judge motion grant rates as a prior).

None of these is prioritized yet. Priority is set by observed Phase 7 candidate quality — whichever scope extension would have produced the most missed candidates wins.

---

## Checkpoints across phases

After every phase:
1. Update `SESSION_STATE.md` with phase transition.
2. Append a phase-completion block to `PROGRESS_LOG.md`.
3. Update `INDEX.md` for any new files.
4. Run the full 10-point self-review from PROJECT_TEMPLATE Part 12 on the phase's deliverables.
5. Consider whether any D-0XX decision should be appended (never edit prior ones).
