# DECISIONS SEED — Silence Scanner (Tool 4)

These are the founding architectural decisions. The new session copies them **verbatim** into `DECISIONS.md` on day one. Once there, they are settled per PROJECT_TEMPLATE Part 3.9. If new evidence invalidates one during build, the new session appends a later-numbered decision overriding it (never edit the original).

---

## D-000 — Founding architecture: silence-domain instantiation of PROJECT_TEMPLATE

Date: <instantiation date>
Context: Tool 4 is the fourth tool in the family. Tools 1/2/3 scan event-driven firehoses. Tool 4 detects absences — a fundamentally inverted epistemology. The bootstrap conversation established four load-bearing deviations from PROJECT_TEMPLATE that must be codified as founding decisions before any code is written.
Decision: Instantiate the full PROJECT_TEMPLATE architecture (two-folder split, ten relay files, four-task topology, Tool Validation Protocol, cold-start and shutdown protocols, overwrite-only lock semantics) for the silence-as-signal domain. Non-negotiables from PROJECT_TEMPLATE apply verbatim. Domain adaptations: (a) baseline persistence as first-class dataset (D-003); (b) mandatory warm-up phase (D-004); (c) probabilistic signal semantics (D-005); (d) 60-day convergence window (D-006); (e) new Baseline Validity scoring dimension replacing Catalyst Timeline (D-007); (f) Russell 1000 universe (D-002); (g) 12-hourly operational cadence (D-008).
Alternatives considered:
  - Extend Tool 1 with silence detection. Rejected: violates structural independence; silence's baseline model is incompatible with Tool 1's event-driven scanners.
  - Bespoke architecture disconnected from the template. Rejected: loses cross-tool convergence compatibility and the proven session-continuity machinery.
Implications: All downstream work assumes PROJECT_TEMPLATE discipline. Scheduled tasks use SESSION_LOCK. SESSION_STATE rewritten every session. Archive-only, never delete.

---

## D-001 — Structural independence from Tools 1, 2, and 3

Date: <instantiation date>
Context: Tools 1/2/3 all enforce "own folder, own lock, own candidates, own reports; cross-tool signal merging happens only via a separate analyzer project." Tool 4 must choose whether to follow.
Decision: Tool 4 is structurally independent from Tools 1, 2, and 3. Separate working folder (`silence_system/`), separate `SESSION_LOCK.md`, separate candidate files, separate reports. Cross-tool convergence (Tool 4 + Tool 1/2/3) happens in a separate analyzer project reading from all four tools' candidate folders but never writing back.
Alternatives considered:
  - Share a lock across tools. Rejected: couples failure modes.
  - Write into a shared candidates folder. Rejected: no way to attribute, dedup, or preserve per-tool audit trails.
Implications: Cross-tool convergence is always a downstream read, never upstream write. Any cross-tool analyzer is a fifth project, not a modification to Tool 4.

---

## D-002 — v1 universe: Russell 1000, US-listed, $2B market-cap floor (entry); $1.5B continuation

Date: <instantiation date>
Context: Silence detection requires calibrated per-issuer baselines; baseline quality is a direct function of observable-volume density per issuer. The universe must be bounded by issuers whose observable surface supports defensible baselines.
Decision: v1 universe = current Russell 1000 membership, US-listed only, market-cap ≥ $2B at entry, ≥ $1.5B for continued coverage (hysteresis band prevents forced eviction during transient drawdowns). Universe refreshed monthly via iShares IWB holdings CSV. Recent IPOs (< 18 months public) excluded even if Russell 1000 member.
Alternatives considered:
  - Full Tool 1 universe (≥ $300M, ~6,500 issuers). Rejected: baseline quality degrades sharply below $2B; false-positive rate becomes unmanageable.
  - S&P 500 only (~500 issuers). Rejected: too narrow; misses mid-cap sweet spot where silence signals have highest info asymmetry.
  - Union of Tools 1 and 2 universes (US + non-US). Rejected: non-US silence is confounded by timezone/holiday/regulatory-calendar effects; defer to Phase 8+.
Implications: v1 candidate universe is ~1,500 issuers. Baseline warm-up for the full universe takes ~18 months if started from scratch; backfill via EDGAR history shortens this to the data available in archives.

---

## D-003 — Baseline persistence is a first-class dataset, not a cache

Date: <instantiation date>
Context: Tools 1/2/3 treat internal caches as incidental — if a cache is lost, scanners rebuild on next pass with minimal cost. Tool 4's baselines ARE the tool's memory of normal; loss of baselines is loss of signal-detection capability for months.
Decision: Per-issuer baseline files live at `baselines/issuer_<cik>.json`. A SQLite index at `baselines/_index.sqlite` enables cross-issuer queries. Baselines are checkpointed into archive every week (full snapshot) and are treated as the tool's durable state — more durable than candidate files, which are derivative. Corruption of baselines triggers an immediate kill of the operational task and escalation via OPEN_QUESTIONS.
Alternatives considered:
  - Rebuild baselines on every scan. Rejected: computationally prohibitive, and some baseline inputs (historical conference presence, old news volume) are not re-fetchable at scale.
  - Keep baselines in SQLite only (no per-issuer JSON). Rejected: JSON is human-readable for audit; SQLite is for query performance. Both layers serve different purposes.
Implications: Baseline integrity monitoring is a maintenance-task responsibility. Baseline corruption is the most serious failure mode; backup discipline is mandatory.

---

## D-004 — Mandatory warm-up phase per issuer; no signals emitted before baseline eligibility

Date: <instantiation date>
Context: A baseline built from < 12 months of observations produces z-scores with wide confidence intervals; silence signals from such baselines are statistically indistinguishable from random variance. Emitting them pollutes the candidate pipeline and degrades user trust.
Decision: An issuer is eligible to produce silence signals only when ALL of the following hold: (1) issuer has been in universe ≥ 12 months; (2) dimension has ≥ 12 months of observations; (3) dimension has met its per-dimension minimum-observations threshold (per `SILENCE_DIMENSIONS.md`); (4) no major corporate event (merger, spin-off, IPO, major restructuring) in the last 180 days that would reshape cadence. Issuers failing any condition are flagged `warm_up_complete: false` in the baseline and excluded from signal emission per-dimension.
Alternatives considered:
  - Shorter warm-up (6 months). Rejected: insufficient for year-over-year seasonal validation.
  - Emit signals with wide confidence intervals and let scoring downweight. Rejected: adds noise; pollutes candidate pipeline; degrades trust.
Implications: At universe entry, a new issuer enters a 12-month warm-up before producing signals. Historical backfill during Phase 1 seeds warm-up for most of the universe; expect ~80% of Russell 1000 to exit warm-up at Phase 1 completion, remainder during Phase 2 operation.

---

## D-005 — Signals are probabilistic; every signal carries z-score, p-value, and alternative-hypothesis list

Date: <instantiation date>
Context: Event-driven signals are declarative ("this happened"). Silence signals are inferential ("this didn't happen; under our baseline, the probability of such absence under null is X"). Mixing paradigms corrupts the candidate pipeline.
Decision: Every silence signal carries `raw_data.per_dimension_scores.*.z_score`, `raw_data.per_dimension_scores.*.p_value_one_sided`, `raw_data.per_dimension_scores.*.n_observations_in_baseline`, and `raw_data.alternative_hypotheses` (list of 3–6 plausible non-silence explanations, each with a brief rule-out note in the deep-dive brief). The scoring rubric's Catalyst Clarity dimension directly penalizes signals whose alternative hypotheses cannot be ruled out.
Alternatives considered:
  - Declarative silence signals matching Tool 1/2/3 format exactly. Rejected: loses the probabilistic information; silent signals are epistemically different from event signals and pretending otherwise corrupts candidate evaluation.
Implications: Scoring rubric has explicit Baseline Validity dimension (D-007). Deep-dive briefs have mandatory alternative-hypothesis section. Candidates without rule-outs score lower.

---

## D-006 — 60-day convergence window (wider than Tools 1/2's 14, Tool 3's 30)

Date: <instantiation date>
Context: Silence signals precede material disclosures on longer lead times than event signals. Empirically: restatement announcements follow 30–60 days of pre-disclosure quiet; SEC investigations cluster with 60–90 day pre-disclosure quiet; major M&A follows 21–45 days of confidentiality window. The convergence window must accommodate the longest typical lead.
Decision: Convergence window for multi-dimensional silence is 60 days (rolling). For cross-tool convergence (Tool 4 + Tool 1/2/3), the window is the longer of (60 days, 30 days before the Tool 1/2/3 event) — i.e., a silence signal 45 days before a Tool 1 8-K is a valid convergence match.
Alternatives considered:
  - 30-day window (match Tool 3). Rejected: cuts off the long lead-time silence-to-disclosure pairs.
  - 90-day window. Rejected: stale silences with decayed edge are admitted.
Implications: Operational task must maintain a 60-day rolling window of baseline observations in hot storage. SQLite index has `last_scan_timestamp` column for efficient windowed queries.

---

## D-007 — 7th scoring dimension replaced: Baseline Validity (×1.5) in place of Catalyst Timeline

Date: <instantiation date>
Context: Tools 1/2 use Catalyst Timeline as 7th dimension; Tool 3 replaced it with Party-Resolution Confidence. Tool 4's defining risk is baseline corruption (silent false-positives from thin or compromised baselines). Scoring must directly penalize low-baseline-validity signals.
Decision: Tool 4's 7th scoring dimension is **Baseline Validity** (weight ×1.5 — elevated from Tool 1/2's ×1 and Tool 3's ×1). Anchors defined in `SILENCE_SCORING.md`. The elevated weight reflects that baseline-validity failures produce the most insidious failure mode: a silence that looks real but isn't. Catalyst Timeline is not a Tool-4-relevant dimension because silence signals are defined by absence of an event, not the countdown to one.
Alternatives considered:
  - Retain Catalyst Timeline. Rejected: meaningful for events, undefined for silences.
  - Baseline Validity at ×1 weight (match Tool 3 Party-Resolution Confidence). Rejected: undershoots the importance; baseline-validity failures are the domain's highest-cost errors.
Implications: Max raw score is 45.0 (vs. 42.5 for Tools 1/2/3). Thresholds scale: 30+ candidate, 23–29 watchlist, 15–22 archive, <15 discard.

---

## D-008 — 12-hourly operational cadence; maintenance 50 min offset; weekly baseline refresh

Date: <instantiation date>
Context: Silence is a slow signal. Sub-12-hour scans add compute without information. Baselines must refresh periodically or drift dominates; too-frequent refresh introduces its own noise.
Decision: Four scheduled tasks:
  1. `silence-operational` — cron `0 */12 * * *` (00:00, 12:00). Write scope `silence_system/`. SESSION_LOCK.
  2. `silence-maintenance` — cron `50 */12 * * *` (00:50, 12:50). Write scope `silence_system/` (audit only). SESSION_LOCK.
  3. `silence-baseline-refresh` — cron `0 3 * * 0` (Sunday 3am, weekly). Write scope `silence_system/baselines/`. SESSION_LOCK.
  4. `silence-performance-report` — cron `30 1 * * *` (daily 1:30am). Write scope `reporting_layer/performance_reports/`. Independent.
  5. `silence-deep-dives` — cron `30 */12 * * *` (00:30, 12:30 — between operational completion and maintenance start). Write scope `reporting_layer/silence_briefs/`. Independent.
Five tasks total. Operational/maintenance/baseline-refresh share a lock; performance-report and deep-dives are independent.
Alternatives considered:
  - 6-hourly operational (match Tool 3). Rejected: doubles compute cost for minimal information gain; silence timescales are days, not hours.
  - Daily baseline refresh. Rejected: forces the weekly-full-refresh pattern into daily partial refreshes, adding noise and lock contention.
Implications: Five scheduled tasks (one more than Tools 1/2/3's four). Baseline refresh is the additional task — it's long-running (~2 hours for full universe) and needs its own slot to avoid colliding with operational scans.

---

## D-009 — Free-sources-only; no paid feeds in v1

Date: <instantiation date>
Context: PROJECT_TEMPLATE mandates free sources. Silence detection is tempting to outsource to paid behavioral-analytics providers (Quiver Quantitative, Sentieo alt-data, Bloomberg Corporate Calendar). Must choose.
Decision: v1 is free-sources-only. Approved sources: EDGAR, Business Wire / GlobeNewswire / PR Newswire public archives, issuer IR RSS where published, GDELT, public Reddit API, StockTwits public API, Finviz, Yahoo! Finance, Benzinga RSS, iShares IWB holdings CSV. No Bloomberg, Refinitiv, FactSet, S&P Global, Quiver, Sentieo, or Wall Street Horizon paid tiers.
Alternatives considered:
  - Allow a small paid-data budget (e.g., Wall Street Horizon Pro at $200/mo for conference calendar). Rejected: violates mandate; opens door to unbounded cost growth; defer to Phase 8+ reconsideration.
Implications: Conference-presence dimension has uncertain data coverage (D-013). Some sources may require polite-scraping with session management; all scrapers respect robots.txt and rate limits.

---

## D-010 — Baseline refresh cadence: weekly full, daily incremental

Date: <instantiation date>
Context: Baselines must stay current but must not introduce scan-to-scan noise from refresh recomputation.
Decision: Weekly full baseline refresh (Sunday 3am) recomputes all dimensions for all in-universe issuers from source data. Daily incremental updates during the operational scan append the day's observations to the observation window and recompute rolling statistics without re-pulling historical data. This preserves scan-to-scan comparability within the week and prevents retroactive signal reshuffling.
Alternatives considered:
  - Monthly full refresh. Rejected: 4-week drift is too long; baseline drift becomes a source of false positives.
  - Daily full refresh. Rejected: compute cost unjustified; scan-to-scan baseline changes would mask genuine silences.
Implications: `silence-baseline-refresh` task is distinct from `silence-operational` (D-008). Baseline archive snapshots occur post-refresh for rollback capability.

---

## D-011 — Signal dedup: per-issuer per-dimension rolling suppression

Date: <instantiation date>
Context: Without dedup, a persistent silence (issuer silent for 45 days) would emit a signal on every scan cycle — 90+ duplicate signals in the candidate pipeline.
Decision: Per issuer, per dimension, a silence signal emitted in a 12-hour scan suppresses subsequent signals from the same dimension for 7 days UNLESS the z-score worsens by ≥ 0.5 (the silence is deepening). When multiple dimensions trigger simultaneously on the same issuer, emit as a single multi-dimension signal (not N separate signals). When the set of triggered dimensions changes, emit a new signal reflecting the new set.
Alternatives considered:
  - 24-hour dedup. Rejected: persistent silences still generate many duplicates within a rolling 7-day window.
  - 30-day dedup. Rejected: misses deepening silences.
Implications: Dedup keys on `(cik, dimension_set, week_number)` with z-score-delta override. The operational scanner consults a `signals/_emitted_index.sqlite` for dedup state.

---

## D-012 — Scheduled-task naming and cron offsets (reaffirmed per D-008)

Date: <instantiation date>
Context: PROJECT_TEMPLATE Part 4 specifies cron-offset logic. Adapt to 12-hourly operational cadence and additional baseline-refresh task.
Decision: Five tasks as specified in D-008. Prefix consistently `silence-`. Date suffix in in-file session identifier. Lock topology: operational/maintenance/baseline-refresh share `silence_system/SESSION_LOCK.md`; performance-report and deep-dives are lockless (reader tasks writing only to `reporting_layer/`).
Implications: Task registration is a Phase 0 milestone. All five tasks registered but left disabled at Phase 0; enabled incrementally as phases complete (operational at end of Phase 2, maintenance at end of Phase 2, baseline-refresh at end of Phase 1, deep-dives at end of Phase 5, performance-report at end of Phase 6).

---

## D-013 — Conference-presence dimension is provisional; downgraded if Phase 1 coverage is poor

Date: <instantiation date>
Context: Conference-presence data is not available from any single free source with universe-wide coverage. Phase 1 probes what's actually accessible.
Decision: Conference-presence dimension is built in Phase 5 (not Phase 3 or earlier) and only if Phase 1 endpoint probing demonstrates at least 50% universe coverage across the major conference categories (sell-side brand conferences, issuer-hosted investor days, earnings-call Q&A). If coverage is < 50%, the dimension is demoted to a "best-effort" status — signals from it are emitted but weighted 0.5 in convergence scoring, and the dimension cannot be the sole basis for a candidate-grade signal.
Alternatives considered:
  - Skip conference dimension entirely. Rejected: when available, it is one of the highest-signal dimensions; worth preserving at reduced weight.
  - Pay for Wall Street Horizon to get universe coverage. Rejected: violates D-009.
Implications: Phase 1 must produce a specific data-coverage report on conferences. Phase 5 decision to build or demote depends on that report.

---

## D-014 — Archive, never delete; overwrite-only lock semantics

Date: <instantiation date>
Context: PROJECT_TEMPLATE non-negotiables #1 and #7. No deviation.
Decision: Follow PROJECT_TEMPLATE non-negotiables verbatim. Archive path `archive/YYYY-MM-DD_<reason>/`. Lock file uses overwrite-only semantics; 4-hour stale-lock window. Baseline archive snapshots go to `archive/baselines_YYYY-MM-DD/` post weekly refresh.
Alternatives considered: None — template non-negotiable.
Implications: Deletion of stale baselines or superseded candidates requires manual user action outside the autonomous system. Accepted cost.
