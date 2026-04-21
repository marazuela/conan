# PHASING — Build Sequence and Milestones (Tool 4)

This file translates the priority queue into a phase-by-phase plan. Each phase has: scope, success criteria, gating milestone. Phases are not rigidly time-boxed — they move when success criteria are met, not when a calendar says so. Rough duration estimates included for planning.

Each phase's first action is **endpoint validation**: live-probe every source in scope and upgrade entries in `CONTEXT.md` from `⚠️ UNVERIFIED` to `✅ VERIFIED` per PROJECT_TEMPLATE Part 13. Never code against an assumed endpoint schema.

---

## Phase 0 — Project instantiation (Days 1–3)

### Scope

Stand up the project skeleton. No scanners. No baselines. No signals. Every file the PROJECT_TEMPLATE demands, in its initial state.

### Actions

1. Create `silence_system/` and `reporting_layer/` folders per PROJECT_TEMPLATE Part 2.
2. Populate the ten relay files per PROJECT_TEMPLATE Part 3. OBJECTIVES and CONTEXT copy verbatim from the bootstrap folder. DECISIONS copies D-000 through D-014 from the decisions seed.
3. Create `framework/scoring_system.md` from `SILENCE_SCORING.md` verbatim.
4. Create `framework/candidate_template.md` — adapted from Tool 1/2/3 candidate template with silence-specific deep-dive sections: alternative-hypothesis ranking, baseline-validity audit, seasonality check, disclosed-alternative-explanation rule-out.
5. Create `framework/dimensions_specification.md` from `SILENCE_DIMENSIONS.md` verbatim.
6. Create `baselines/` directory (empty at Phase 0; populated Phase 1). Create `_index.sqlite` with schema per `SILENCE_CONTEXT.md`.
7. Create `universe/` directory with an empty `russell_1000.json` placeholder and a `_refresh_log.md`.
8. Create empty `tools/`, `signals/` (with `_emitted_index.sqlite` empty), `candidates/` (with `delivered/` and `archive/` subfolders), `reports/`, `working/`, `research/`, `archive/`.
9. Create `reporting_layer/` substructure per PROJECT_TEMPLATE Part 5.
10. Register all five scheduled tasks per D-008/D-012. **Leave all disabled**. Phase 0 is skeleton-only.

### Success criteria

- [ ] All files exist; INDEX.md enumerates them; every markdown file is valid; SESSION_LOCK.md reads UNLOCKED.
- [ ] Cold-start read order works: a brand-new session reads SESSION_STATE + INSTRUCTIONS and correctly identifies "begin Phase 1" as the next action.
- [ ] DECISIONS.md contains D-000 through D-014 verbatim.
- [ ] All five scheduled tasks exist in the scheduler, disabled.

### Gating milestone to Phase 1

Manual cold-start test: open a fresh Cowork session, point it at `silence_system/`, verify it identifies Phase 1 as the next work block.

---

## Phase 1 — Endpoint validation + universe definition + baseline backfill (Weeks 1–4)

### Scope

Validate every endpoint. Define the Russell 1000 universe. Backfill baselines for every dimension that can be backfilled (EDGAR-based dimensions can go back 10+ years; GDELT goes back to 2015; others vary). By end of Phase 1, most of the universe should be warm-up-eligible.

### Actions

1. **Live-probe every endpoint** in `CONTEXT.md`. Record status, response shapes, rate-limit observations. Upgrade entries to `✅ VERIFIED`. Failed probes → OPEN_QUESTIONS Q-001, Q-002, …
2. **Conference-data coverage report** per D-013: enumerate conference-data sources, measure universe coverage, decide whether to build dimension fully or demote to 0.5 weight.
3. Build `tools/universe_builder.py` — pulls iShares IWB holdings CSV, filters by market cap $2B+ and IPO-age ≥ 18 months, writes `universe/russell_1000.json`. Run once to populate.
4. Build `tools/baseline_builder.py` — parameterized by dimension; pulls historical observations, computes baseline statistics (mean, stddev, seasonal multipliers), writes per-issuer JSONs to `baselines/issuer_<cik>.json`, updates `_index.sqlite`.
5. Run baseline backfill for each dimension in priority order:
   - EDGAR filing cadence (fastest, cleanest).
   - Insider transaction cadence (same source, different parser).
   - Press-release cadence (multi-source, slower).
   - News/social mention volume (GDELT + public APIs; slow due to rate limits).
   - Analyst-note cadence (Finviz / Yahoo scraping; slow).
   - Conference presence (only if D-013 decision is "build").
6. Validate baselines: on a held-out 90-day window (most recent 90 days before the scan start date), verify z-score distribution approximates standard normal (sanity check; skewness < 0.5, kurtosis reasonable).
7. Seasonal adjustment fitting: for each dimension, fit quarter/summer/holiday multipliers on historical data, lock parameters into each issuer's baseline. Multi-year data is used for cross-validation.
8. Enable `silence-baseline-refresh` scheduled task (the others remain disabled).

### Success criteria

- [ ] All endpoints that will be used in Phase 2+ are VERIFIED.
- [ ] Russell 1000 universe cached with ≥ 1,400 eligible issuers (allowing for IPO-age and recent-restructuring exclusions from the ~1,500 universe).
- [ ] Per-dimension coverage: EDGAR ≥ 99%, press-release ≥ 85%, insider ≥ 99%, news/social ≥ 90%, analyst-notes ≥ 80%, conference dimension coverage known (Build or Demote decision made).
- [ ] Baseline validation passes (held-out z-score distribution is approximately standard normal per dimension).
- [ ] At least 70% of Russell 1000 marked `warm_up_complete: true` across all dimensions.
- [ ] `tools/` directory passes `py_compile` and Tool Validation Protocol.

### Gating milestone to Phase 2

Backfill complete, validation passes, baseline refresh task runs for one full weekly cycle without error.

---

## Phase 2 — First scanner: EDGAR Filing Cadence (Weeks 4–6)

### Scope

Build the highest-reliability, lowest-complexity scanner first. It exercises the full pipeline: baseline read → observation → z-score → signal emission → triage → scoring → pipeline integration.

### Actions

1. Build `tools/edgar_filing_scanner.py` — reads current observations, appends to baseline window, computes z-score and p-value, emits signals above threshold.
2. Build minimal `tools/pipeline_runner.py` — runs the scanner across the universe, passes signals through triage, through scoring, writes surviving signals to `signals/`.
3. Build `tools/triage_filter.py` — applies warm-up check, baseline-validity check, dedup (per D-011), minimum-z threshold.
4. Build `tools/scoring_engine.py` — applies the 7-dimension rubric per `SILENCE_SCORING.md`, writes scored signal records.
5. Enable `silence-operational` and `silence-maintenance` scheduled tasks.
6. Run for 7 consecutive days (longer than litigation tool's 3 days because silence signals are slower to accumulate).

### Success criteria

- [ ] Scanner runs end-to-end across all warm-up-complete issuers without crashes for 14 consecutive scan cycles.
- [ ] Signal volume reasonable: for EDGAR-only, expect ~10–30 raw silence signals/day across 1,000+ warm-up-complete issuers; 2–5 surviving triage; 0–2 reaching scoring stage.
- [ ] First candidates produced (if any score ≥ 30).
- [ ] Dedup working: persistent silences don't generate duplicate signals on subsequent scans.
- [ ] Wall-clock budget respected: operational pass completes within 45 minutes for the full universe.

### Gating milestone to Phase 3

Seven consecutive days of autonomous operational runs, zero lock failures, baseline refresh task's weekly run succeeds cleanly.

---

## Phase 3 — Add press-release and insider-transaction scanners (Weeks 6–8)

### Scope

Add the next two highest-signal dimensions. These share pipeline machinery with Phase 2's EDGAR scanner; added complexity is in multi-source press-release dedup and insider-transaction earnings-blackout masking.

### Actions

1. Build `tools/press_release_scanner.py`. Includes cross-source dedup (Business Wire + GlobeNewswire + PR Newswire + IR RSS, dedup on title+date).
2. Build `tools/insider_transaction_scanner.py`. Includes earnings-blackout mask (consult earnings calendar cache to exclude pre-earnings windows from silence attribution).
3. Build `tools/earnings_calendar_cache.py` — small service that maintains a rolling per-issuer earnings-date cache (scraped from issuer IR or Yahoo! Finance earnings calendar).
4. Add both scanners to `pipeline_runner.py`. No convergence engine yet.
5. Run for 7 consecutive days.

### Success criteria

- [ ] Both scanners run without crashes.
- [ ] Press-release dedup working (a single release cross-posted across sources counts once).
- [ ] Insider-transaction blackout masking working (no false silences during pre-earnings blackouts).
- [ ] Signal quality similar to Phase 2 single-dimension baseline.
- [ ] Operational wall-clock still within budget (60 min target with 3 scanners).

### Gating milestone to Phase 4

Three scanners in continuous operation for 7 consecutive days.

---

## Phase 4 — Convergence engine (Week 8)

### Scope

Build the multi-dimensional convergence engine. This is where the tool's real power emerges.

### Actions

1. Build `tools/convergence_engine.py`. Within the 60-day window (D-006), for each issuer, checks whether multiple dimensions are simultaneously in anomaly state. Combined anomaly score = weighted sum of |z| values across triggered dimensions (weights per D-013 and `SILENCE_DIMENSIONS.md`). Emits a multi-dimensional silence signal distinct from single-dimension signals.
2. Update `pipeline_runner.py` — convergence check runs after all scanners have produced signals in a given cycle.
3. Update dedup logic for multi-dimensional signals (per D-011: when the triggered dimension set changes, new signal; otherwise 7-day suppression with z-delta override).
4. Update daily report to include a "multi-dimension convergence" top-priority section.

### Success criteria

- [ ] Convergence engine correctly identifies ≥ 2-dimension overlap on test cases.
- [ ] Zero false convergences (two signals on the same issuer that are not actually the same period).
- [ ] Multi-dimensional signals appear distinct from single-dimension in the signal record and the report.
- [ ] First multi-dimensional candidate produced (if any score ≥ 30).

### Gating milestone to Phase 5

At least one genuine (manually-verified) multi-dimensional silence detected in production data.

---

## Phase 5 — Add news/social, conference (if built), analyst-notes scanners (Weeks 8–11)

### Scope

Add the remaining three dimensions. These are the hardest because sources are varied, noisier, and have more seasonality.

### Actions

1. Build `tools/news_social_scanner.py` — GDELT + Reddit + StockTwits. Includes weekend/holiday mask.
2. If D-013 decision is "build": build `tools/conference_scanner.py` — scrapes major sell-side conference agendas, compares to issuer's historical conference participation. If decision is "demote": build a reduced-weight version that uses only whatever coverage is available.
3. Build `tools/analyst_note_scanner.py` — Finviz + Yahoo + Benzinga scraping with per-source dedup.
4. Integrate all three into `pipeline_runner.py`. Convergence engine now sees six dimensions.

### Success criteria

- [ ] All six scanners run in a single operational cycle within wall-clock budget (90 min target with 6 scanners).
- [ ] Conference dimension delivers (at the level D-013 decision specified).
- [ ] News/social mega-cap downweighting working correctly (mega-caps don't flood the pipeline with low-info signals).
- [ ] Baseline refresh task handles all six dimensions within its 2-hour weekly window.

### Gating milestone to Phase 6

All six scanners in continuous operation for 7 consecutive days. Multi-dimensional convergence producing at least 3 candidates/week across the universe.

---

## Phase 6 — Reporting layer (Weeks 11–12)

### Scope

Build the `reporting_layer/` deliverables. Read-only on `silence_system/`, write to `reporting_layer/`.

### Actions

1. Enable `silence-performance-report` (daily 1:30am) — PDF dashboard: KPI cards (active signals, warm-up-complete %, per-dimension health, API reachability), universe stats, convergence activity, candidate pipeline, baseline drift monitoring, code health.
2. Enable `silence-deep-dives` (every 12h, at :30) — for each 30+ candidate, produce a full docx brief and PDF. Brief must include: alternative hypotheses enumerated, baseline validity audit, seasonality rule-out, disclosed-alternative-explanation rule-out, inferred catalyst hypothesis ranking, catalyst-window estimate, entry/monitoring notes. Dedup registry in `silence_briefs/index.json` prevents regeneration unless baseline-validity changes, new dimensions trigger, or the z-score meaningfully deepens.
3. PDF generation via `reportlab` directly (known failure mode per template: docx→pdf chain is unreliable).

### Success criteria

- [ ] Daily performance report generated and readable.
- [ ] Deep-dive briefs produced for every 30+ candidate; dedup registry works; regenerations only when warranted.
- [ ] Reporting-layer tasks never write into `silence_system/`.

### Gating milestone to Phase 7

Reporting layer in continuous operation for 3 consecutive days.

---

## Phase 7 — Autonomous operation validation (Weeks 12–14)

### Scope

Let the full system run for 14 consecutive days with zero manual intervention. Longer than the litigation tool's 7-day validation because silence signals take longer to resolve (silence → disclosed catalyst is a weeks-long cycle).

### Actions

1. Stop manual-run mode. Let all five scheduled tasks fire on schedule.
2. Each day, review but do not modify. Observed failures go into `OPEN_QUESTIONS.md` via the next maintenance session.
3. At end of day 14, review `PROGRESS_LOG.md`, `OPEN_QUESTIONS.md`, maintenance audit records. Triage open issues.
4. Run backtest against 20 known pre-disclosure windows from 2022–2025. Verify the tool fires appropriate silence signals with ≥ 14 day lead in ≥ 60% of cases; false-positive rate < 25%.

### Success criteria

- [ ] 14 consecutive days, zero manual interventions.
- [ ] No session runs past 4 hours.
- [ ] No lock failures.
- [ ] At least 3 validated candidates produced in production (any score); at least 1 at 30+.
- [ ] Backtest thresholds met (60%+ recall at 14-day lead; <25% false-positive rate).
- [ ] Write-scope discipline maintained across all five tasks.
- [ ] Self-review checklist (PROJECT_TEMPLATE Part 12) passes on every candidate.

### Gating milestone to Phase 8

14-day autonomous run succeeds. Tool 4 v1 is **done**.

---

## Phase 8+ — Post-v1 evolution (open-ended)

Only opened after Phase 7 succeeds.

**Candidate scopes for Phase 8+:**
- Non-US universe (Tool 2 + silence convergence) — requires timezone/holiday/regulatory-calendar modeling.
- Additional dimensions: patent-filing cadence (USPTO), FDA-correspondence cadence, FCC-filing cadence (sector-specific), supplier/customer network dynamics, social media employee mentions (Glassdoor/LinkedIn public profiles).
- Paid data sources (Wall Street Horizon Pro, Quiver, Sentieo) if free-source coverage proves insufficient for specific dimensions.
- Sector-level cross-issuer silence (multiple sector peers going silent together → sector-wide M&A / regulatory event).
- Cross-tool analyzer project reading Tool 1/2/3/4 candidates and emitting cross-tool convergence signals (the "Tool 4 + Tool 3 convergence" case — a silence preceding a litigation filing is a strong joint signal).
- Time-series ML for baseline modeling (state-space models; Prophet).
- Sentiment-aware mention dimension (currently volume-only).
- Real-time streaming operational cadence (if latency sensitivity emerges).

None of these is prioritized yet. Priority is set by observed Phase 7 candidate quality and backtest recall — whichever scope extension most improves either is next.

---

## Checkpoints across phases

After every phase:
1. Update `SESSION_STATE.md` with phase transition.
2. Append phase-completion block to `PROGRESS_LOG.md`.
3. Update `INDEX.md` for any new files.
4. Run full 10-point self-review from PROJECT_TEMPLATE Part 12 on phase deliverables.
5. Consider whether any D-0XX decision should be appended (never edit prior ones).
6. Baseline archive snapshot (post-phase, in addition to weekly).
