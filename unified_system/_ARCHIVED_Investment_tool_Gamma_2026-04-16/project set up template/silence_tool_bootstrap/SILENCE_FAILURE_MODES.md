# FAILURE MODES — Silence Scanner (Tool 4)

Domain-specific failure modes, additive to the PROJECT_TEMPLATE Part 17 catalog. Each has a mitigation already wired into the design (via DECISIONS seed) or assigned to a specific phase/scanner to implement.

Read this before the first scanner is written. By the time any of these surfaces in production, silent corruption has probably already begun.

Silence-specific failure modes are particularly dangerous because the tool's signal *is* absence of signal — a failure that suppresses real silences (false negatives) looks exactly like a quiet period. And a failure that injects false silences (false positives) looks like productive work.

---

## F-01 — Baseline corruption from a single mis-parsed filing

**Symptom:** During weekly baseline refresh, a malformed EDGAR response causes one issuer's filing count to be recorded as 0 for a period when it should have been 4. The baseline now has an artificial dip. Subsequent scans use the corrupted baseline and fail to emit a real silence signal (because the baseline now says "low activity is normal").

**Why it happens:** Parsing edge cases, transient network failures logged as empty responses, EDGAR schema changes.

**Mitigation:**
- Weekly baseline refresh always diffs against the prior baseline snapshot. Any dimension value that drops > 40% in a single refresh cycle is flagged for manual review in `OPEN_QUESTIONS.md` before the refreshed baseline is written.
- Baseline archive snapshots (D-014) enable rollback.
- Per D-003, baseline corruption triggers an immediate kill of the operational task.

---

## F-02 — Warm-up false eligibility from data-source backfill gaps

**Symptom:** An issuer's GDELT mention history appears complete but is actually missing data from late 2023 due to a GDELT coverage gap. Baseline looks populated (≥ 12 months of observations) and passes warm-up gate (D-004). Subsequent scans emit silence signals based on a baseline that systematically understates historical activity — producing false silences.

**Why it happens:** Data sources have coverage gaps that are invisible to a naive "count observations" check.

**Mitigation:**
- Baseline builder records `data_density` per dimension — observations per unit time, not just total count. If observations-per-week drops to 0 for any 7+ day period inside the baseline window, flag as "gap suspected" and require manual review before warm-up eligibility.
- Phase 1 validation checks baseline z-score distribution against standard normal. Systematic deviation is a F-02 tell.

---

## F-03 — Seasonal adjustment over-fitting

**Symptom:** Phase 1 fits quarter-end/summer/holiday multipliers on 2022–2025 data. The model memorizes 2022's one-off COVID-adjacent patterns; 2026 Q2 behaves differently; z-scores come out systematically too extreme. Either false-positive flood or false-negative drought.

**Why it happens:** In-sample seasonal fitting on limited history.

**Mitigation:**
- Phase 1 seasonal-adjustment fitting uses a train/holdout split: 2022–2024 train, 2025 holdout. Adjustment parameters are accepted only if they generalize to the holdout (z-score distribution matches standard normal on held-out data).
- D-007 Baseline Validity dimension anchors specifically penalize signals from baselines where seasonal adjustment was not out-of-sample validated (score 3 or 4 rather than 5).

---

## F-04 — Scheduled maintenance window looks like silence

**Symptom:** Issuer announces a planned multi-week period of no public communications ahead of a major product launch or capital markets day. This is disclosed in a prior 8-K or conference remark. The scanner sees the subsequent silence and emits a high-confidence signal. The "silence" is fully expected.

**Why it happens:** Scanners are blind to forward-looking disclosures that pre-announce silence.

**Mitigation:**
- Deep-dive brief template includes "disclosed alternative explanation" rule-out: reviewer must check the last 90 days of 8-Ks, conference transcripts, and IR communications for language announcing an upcoming quiet period.
- Phase 8+ enhancement: automate the rule-out by NLP-scanning recent filings for "we do not plan to provide further updates until X" phrasing.

---

## F-05 — Mid-period universe exit looks like silence

**Symptom:** A Russell 1000 issuer is acquired, delists, or drops below the market-cap floor mid-scan-cycle. The issuer stops producing filings and press releases (because it's no longer a public company or is pending closing). Scanner emits silence signals right up until the universe is refreshed.

**Why it happens:** Universe refresh is monthly; scans are 12-hourly.

**Mitigation:**
- Operational scanner consults a "universe exit candidates" flag: issuers with (a) announced merger agreement, (b) pending delisting notice, or (c) market-cap below continuation floor for ≥ 5 consecutive trading days. Flagged issuers produce signals at reduced weight (max score 3 on Baseline Validity) and are flagged "pending universe exit" in the signal record.
- Universe refresh reviews exit candidates and promotes to actual exits; archived baselines are retained in `archive/baselines_exited_YYYY-MM-DD/`.

---

## F-06 — Baseline drift from successful acquisition integration

**Symptom:** Issuer completes a major acquisition. Post-acquisition, the combined entity's press-release cadence doubles (two pre-existing IR functions merge into one). Baseline is out of date; expected cadence is much higher than historical. Scanner emits false silences because the new "normal" is higher than the old baseline.

**Why it happens:** Baselines lag structural change.

**Mitigation:**
- Corporate-event detector (looks for 8-K item 2.01 "Completion of Acquisition" filings) flags issuers for baseline rebuild. Flagged issuers enter a 180-day warm-up re-lock (D-004), during which no silence signals are emitted.
- Baseline rebuild discards pre-event observations and waits for 180 days of post-event data before re-eligibility.

---

## F-07 — Mention-volume manipulation (pump/dump, coordinated bots)

**Symptom:** A penny-stock-adjacent issuer gets hit by a coordinated Reddit pump; mention volume spikes 10× for a week, then collapses. Baseline re-weights to include the spike. When mention volume returns to pre-spike normal, the scanner reads this as a silence (observed < expected) and emits a false signal.

**Why it happens:** Non-fundamental attention spikes distort baselines.

**Mitigation:**
- News/social mention scanner maintains a "outlier day" detector: any day with mention volume > 5 standard deviations above the rolling median is flagged as outlier and excluded from baseline updates (but included in the current-scan observation — so a single pump day does not generate a silence signal on that day).
- Issuers with > 3 outlier days in a rolling 30-day window are flagged "retail-attention-dominated" and their news/social dimension is demoted to weight 0.3.
- Russell 1000 universe floor ($2B market cap) already excludes most pump-target issuers.

---

## F-08 — Earnings-blackout leak into silence signals

**Symptom:** Insider-transaction dimension mis-attributes the pre-earnings blackout window as a silence. Every issuer generates false signals for 14 days before every earnings date.

**Why it happens:** Earnings-calendar cache is incomplete, stale, or fails to load.

**Mitigation:**
- `tools/earnings_calendar_cache.py` maintains redundant sources (Yahoo! Finance earnings calendar + issuer IR page scrape). Disagreement between sources triggers manual review.
- Insider-transaction scanner double-checks: if signal would emit but the scan date is within 14 days of an upcoming earnings (per cache), signal is suppressed and logged as "earnings-blackout-masked."
- Maintenance task audits suppressed signals weekly to ensure earnings cache is operating correctly.

---

## F-09 — Dedup failure from issuer aux-ticker changes

**Symptom:** Issuer changes ticker (e.g., corporate rename or share-class reorganization). The scanner sees the old and new tickers as separate issuers; silence signals emit against both; dedup misses because the dedup key is ticker-based.

**Why it happens:** Ticker is not a stable identifier.

**Mitigation:**
- Primary dedup key is CIK (stable identifier). Ticker is a secondary display field.
- `issuer_figi` is the cross-tool convergence key, consistent with Tools 1/2/3.
- Universe refresh explicitly detects ticker changes (CIK stable, ticker changed) and merges baselines under the stable CIK. Pre-change and post-change observations are both preserved.

---

## F-10 — Rate-limit exhaustion during weekly baseline refresh

**Symptom:** Weekly baseline refresh attempts to pull 2 years of historical data for 1,500 issuers across 6 dimensions. Rate limits trip; job runs for 8 hours; stale-lock triggers at 4 hours; lock is seized by next scheduled task; partial refresh produces inconsistent baselines.

**Why it happens:** Baseline refresh is compute-intensive.

**Mitigation:**
- Baseline refresh is chunked: 1/7 of universe per day across Sunday–Saturday. Weekly full refresh = 7-day rolling refresh, not a single long job. Lock scope is reduced.
- Explicit lock acquisition with stale-window check at each chunk.
- Rate-limit-aware fetching (same exponential backoff as Tools 1/2/3).

---

## F-11 — Over-signaling from one mega-event

**Symptom:** A major market event (COVID-like, 2008-like) causes universe-wide behavioral shift. Every issuer's baseline temporarily mismatches reality; thousands of silence signals emit simultaneously; candidate pipeline floods.

**Why it happens:** Baselines assume roughly stationary regimes.

**Mitigation:**
- Scanner monitors "universe-wide signal rate" (% of universe emitting silence signals in a scan cycle). If > 15% of the universe signals in a single cycle, trigger a **regime-change mode**: operational task emits a `regime_change_suspected` meta-signal, does not emit individual candidate signals, and escalates via `OPEN_QUESTIONS.md`. User decides whether to pause emission, rebuild baselines post-event, or continue with downweighted signals.
- Regime-change detection is in the scoring pipeline, not in individual scanners — it requires cross-issuer context.

---

## F-12 — False silence from universe addition

**Symptom:** An issuer newly added to Russell 1000 has 12+ months of pre-addition history. The baseline builder correctly backfills. But pre-addition history is for a smaller, quieter company; post-addition the company profile is larger (it crossed the threshold because of growth); expected cadence is now higher; the old baseline is stale-low; scanner sees the new higher activity as NOT silence when it should be (relative to the newer, correct baseline). Or inversely: issuer dropped out and got re-added; the gap in universe membership corrupts the history.

**Why it happens:** Universe transitions are themselves corporate-development signals that reshape cadence.

**Mitigation:**
- New universe additions enter 180-day warm-up re-lock (consistent with F-06 mitigation).
- Re-entrants (issuers that left and returned) are treated as fully fresh warm-ups; prior baselines are archived, not resumed.

---

## F-13 — Conference-agenda publication timing artifacts

**Symptom:** Scanner checks whether issuer is on the 2026 JPMorgan Healthcare Conference agenda. Agenda is published on conference-start-date minus 30 days; scanner checks on day 60 pre-conference and sees issuer not listed (because agenda isn't published yet); emits a false absence signal.

**Why it happens:** Binary "present/absent" signals require knowing when the agenda is finalized.

**Mitigation:**
- Conference scanner maintains per-conference "agenda expected published by" dates from historical patterns. Absence signal only emits if scan date > published-by date.
- If agenda is repeatedly not published by expected date, flag in maintenance log; may indicate the conference itself is cancelled or delayed.

---

## F-14 — Cross-dimensional correlation inflating combined anomaly

**Symptom:** Two dimensions that are supposed to be independent (EDGAR filing cadence and insider-transaction cadence — both derived from Form 4 activity) are actually correlated. When one is silent, the other tends to also be silent. Combined anomaly score treats them as independent; joint p-value is overstated; candidates cluster with falsely-high scores.

**Why it happens:** Dimensions share underlying data or causal structure.

**Mitigation:**
- Phase 1 validation includes correlation analysis: z-score correlation between each pair of dimensions on held-out data. Correlations > 0.3 reduce the effective independent-dimensions count in the joint-probability estimate.
- `SILENCE_DIMENSIONS.md` documents known correlations (Filing Cadence × Insider Transaction Cadence ≈ 0.4 because both are EDGAR-derived; Press-Release Cadence × News/Social Volume ≈ 0.35 because press releases drive news coverage).
- Scoring dimension 1 (Signal Strength) anchors reference combined anomaly score, not raw joint p-value; the anchor numbers empirically account for correlation.

---

## F-15 — Scheduled task lock contention

**Symptom:** `silence-operational` (00:00), `silence-deep-dives` (00:30), and `silence-maintenance` (00:50) sometimes overlap when operational runs long. Maintenance starts; tries to acquire lock; blocks until stale; operational finishes; maintenance finally starts but now has overlapping scope assumptions.

**Why it happens:** Per D-008, operational and maintenance share a lock; deep-dives is independent. But if operational's wall-clock budget is exceeded, downstream tasks pile up.

**Mitigation:**
- Operational task has hard wall-clock budget (45 minutes for full universe post-Phase 2; 90 minutes with all 6 scanners Phase 5+). Exceeded budget triggers scanner-level prioritization (drop lowest-priority dimensions for the current cycle and emit a `budget_exceeded` warning).
- Maintenance task checks `SESSION_LOCK` age; if locked by operational and operational has been running > 2 hours, maintenance stays passive, logs, and skips cycle.

---

## F-16 — Reporting-layer silence from silence-system silence

**Symptom:** Silence-system produces no candidates for 3 consecutive days (could be: no real candidates; could be: operational task is silently failing). Deep-dive task runs, finds nothing to process, writes nothing. Performance report shows zero candidates — user misreads this as "system is working, just quiet."

**Why it happens:** Absence of output is ambiguous between "no signal" and "system failure."

**Mitigation:**
- Performance report distinguishes "zero candidates produced" from "pipeline inactive." The report has explicit rows for: operational task last-run-date, operational task last-success-date, scanner-level last-emission-date per dimension, universe-coverage health.
- Maintenance task's signal-volume health check compares daily counts to rolling 90-day median; 0-signal days when median is > 5 triggers an alert in the next day's performance report.

---

## F-17 — Issuer self-disclosure changes baseline trajectory before the signal can form

**Symptom:** Issuer goes quiet for 20 days (mid-anomaly window). Then on day 21, issuer pre-announces the exact catalyst the silence was preceding (e.g., "we're announcing a strategic review"). The silence was right — but the baseline now includes the disclosure event as an observation; on day 30, when the scanner would have scored a strong silence signal, the baseline has re-weighted and the signal is weaker. The tool correctly identified a developing situation but emits a weaker-than-ideal signal at maturity.

**Why it happens:** Baselines update continuously; late-stage baseline shifts can dampen strong signals that should be at their peak.

**Mitigation:**
- Signal emission uses a baseline snapshot as of scan-date-minus-7-days, not real-time baseline. This prevents mid-anomaly baseline re-weighting from dampening signals formed before the disclosure.
- Dedup logic (D-011) ensures the day-20 partial-strength signal is retained as the representative for the silence period; the day-30 weaker signal does not overwrite it.

---

## F-18 — Data-source silence vs. issuer silence

**Symptom:** GDELT is down for 3 days. Scanner reads zero mentions for all in-universe issuers. Without safeguards, emits silence signals for every issuer simultaneously — a universe-wide false-positive wave.

**Why it happens:** Data-source failure looks identical to universe-wide silence at the observation layer.

**Mitigation:**
- Per-dimension scanner monitors "observations this scan vs. observations per baseline-expectation." If a scanner returns zero observations across > 50% of the universe in a single scan, the scanner aborts with a `data_source_suspected_down` error rather than emit silence signals.
- Data-source health is checked at the maintenance task and logged.
- Shares mechanism with F-11 regime-change detector but at the scanner level (F-11 is cross-scanner; this is intra-scanner).

---

## F-19 — Backtest contamination

**Symptom:** Phase 7 backtest validation uses baseline data that includes the post-disclosure period of the known events. The baseline "sees" the event and its aftermath; z-score for the pre-disclosure window is artificially deflated; backtest shows better recall than the tool will achieve in production.

**Why it happens:** Naive backtest setup uses full-history baselines against historical test cases.

**Mitigation:**
- Phase 7 backtest uses point-in-time baselines: for a known event on date D, baselines are computed using only data up to date D-1. This is much more expensive computationally but is the only valid evaluation.
- Backtest results are reported with explicit methodology notes: "point-in-time baselines used" is a precondition for result validity.

---

## F-20 — Lost baselines from disk failure

**Symptom:** File system corruption, accidental deletion, or sync-failure destroys `baselines/` contents. Without archives, baselines would need to be rebuilt from scratch — an 18-month warm-up delay.

**Why it happens:** Baselines are the most critical durable state and also the largest artifact.

**Mitigation:**
- Weekly baseline archive snapshot (D-014) to `archive/baselines_YYYY-MM-DD/`.
- Post-phase baseline snapshots (PHASING checkpoints).
- If baselines are lost: restore from most recent archive snapshot; if snapshot is older than 14 days, rebuild from source for the gap window; mark all issuers as needing re-validation before emission resumes.
- `SESSION_STATE.md` always records the baseline-snapshot timestamp of the current running state, making restoration-point identification unambiguous.
