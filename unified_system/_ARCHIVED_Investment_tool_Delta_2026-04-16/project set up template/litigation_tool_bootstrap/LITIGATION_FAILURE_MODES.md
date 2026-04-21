# FAILURE MODES — Litigation & Docket Signal System (Tool 3)

These are failure modes specific to the litigation domain, additive to the catalog in PROJECT_TEMPLATE Part 17. Each has a mitigation that is either already wired into the design (via DECISIONS seed) or assigned to a specific phase/scanner to implement.

Read this before the first scanner is written. Scanners and pipeline code must anticipate these; by the time any of them surfaces in production, silent corruption has probably already begun.

---

## F-01 — Party-name collision (false-positive entity resolution)

**Symptom:** A docket entry names "Delta Corp." as defendant. The resolver matches it to Delta Air Lines (DAL). The actual defendant is a privately-held Delaware shell. A signal is generated against DAL that has nothing to do with DAL.

**Why it happens:** Corporate names are not unique. Thousands of public-adjacent entities share partial names with public companies. SEC EDGAR exact-match will happily return a CIK for any entity that has ever filed; the CIK → ticker step can then mis-attribute to the public relative.

**Mitigation:**
- Resolution confidence thresholds (D-003). Signals < 0.85 are triaged out.
- Resolution method recorded on every signal. Downstream analyses can filter on method.
- The Party-Resolution Confidence scoring dimension (D-003) penalizes borderline resolutions even when they survive triage.
- Phase 1 validation set (100 manually-labeled cases) catches systematic mis-resolution before scale.
- Maintenance task audits resolution-cache entries for patterns suggestive of collision (e.g., same normalized party string resolving to different CIKs in different cases).

---

## F-02 — Subsidiary-in-caption without parent attribution

**Symptom:** The docket names "XYZ Subsidiary LLC." The Exhibit 21 lookup finds XYZ Subsidiary is a wholly-owned subsidiary of Public Parent Inc. The signal is attributed to the parent. But the case is contract litigation affecting only the subsidiary's local operations (say, a janitorial vendor dispute) — not material to the parent.

**Why it happens:** Resolution correctly identifies the parent, but the signal's materiality to the parent is low. The resolver can't tell.

**Mitigation:**
- Scoring dimension Signal Strength (×2) asks "material to the issuer's equity value?" — low-materiality subsidiary cases score 1–2 and wash out at triage or scoring.
- Deep-dive checklist for every 28+ candidate requires explicit materiality assessment. If materiality is actually low, candidate is immediately killed and logged.
- Over time, patterns emerge: specific subsidiaries appearing repeatedly in immaterial cases get cached with a materiality flag (Phase 2+ enhancement).

---

## F-03 — Docket consolidation drift (MDL)

**Symptom:** A case starts in N.D. Cal. as 3:25-cv-01234. It gets consolidated into MDL 9999 in E.D. La. The case-number changes. The scanner sees the old case-number disappear (dismissed! — wrong) and the new case-number appear (new complaint! — wrong).

**Why it happens:** MDL consolidation is a distinct docket operation that scanners not tuned for it will misread.

**Mitigation:**
- Scanner detects consolidation-into-MDL via docket-entry keywords (`transferred to MDL`, `consolidated with`, `transferred and consolidated`). When detected, emit a `consolidation_event` signal linking the old and new case numbers, not a fresh-complaint signal.
- Maintenance task audits for signal pairs where one case disappears and another with same parties appears in another district within 7 days — flags for manual review.

---

## F-04 — Sealed filings and redactions

**Symptom:** A docket entry appears that says "[sealed]" or "[redacted]." The scanner attempts to parse the body; fails; either errors out or emits a garbage signal.

**Why it happens:** Sealed filings are common (trade secrets, protective orders, minor privacy). RECAP and PACER indicate sealing differently.

**Mitigation:**
- Scanner treats sealed/redacted as a distinct docket-entry class. Emits a `sealed_filing` meta-signal (the *fact* of sealing can itself be a signal — see Phase 8+ ideas) but does NOT attempt to score it as a content signal.
- Sealed-filing counts go into the daily report as a separate metric. A surge in sealed filings on an in-universe entity is itself interesting.

---

## F-05 — RECAP coverage gap

**Symptom:** A material docket entry exists on PACER but is not in RECAP. Scanner gets the index entry but no document body. Deep-dive analysis proceeds from the entry description alone, which may be vague ("ORDER granting in part and denying in part" with no elaboration).

**Why it happens:** RECAP archives documents that users have already paid PACER for and uploaded. For less-trafficked cases, RECAP coverage is sparse.

**Mitigation:**
- D-008's flag-for-manual-pull pattern. Scanner records `raw_data.document_status = "in_pacer_only"` and page-count estimate.
- Deep-dive analysis proceeds from the docket-entry description; if the analysis can't reach a defensible thesis without the document body, the candidate is parked in `candidates/pending_pacer/` (new subfolder, outside the normal candidate flow) and queued in `working/pacer_pulls_requested.md`.
- Over time, the user's manual pulls enrich RECAP (the Free Law Project's RECAP extension re-uploads pulled documents), improving coverage for future sessions.

---

## F-06 — Caption changes mid-case (amended complaint, substitutions)

**Symptom:** Case starts as "Foo Corp. v. Acme Inc." Plaintiff amends complaint to add defendants, caption becomes "Foo Corp. v. Acme Inc. et al." Scanner sees the new caption, re-resolves, may mis-identify.

**Why it happens:** Captions are mutable. Dedup keys based on `case_number` are stable; dedup keys based on caption are not.

**Mitigation:**
- Dedup keys are `(court + case_number + docket_entry_id)`, never caption-based.
- Resolver cache keys on the party string seen at time of resolution; it does not "re-resolve when caption changes." A new party name in an amended caption is treated as a new resolution attempt.

---

## F-07 — Clerk-entered administrative noise

**Symptom:** Docket fills up with "Notice of Appearance," "Motion for Admission Pro Hac Vice," "Minute Entry for Status Conference." Scanner emits hundreds of signals per day, all worthless.

**Why it happens:** District-court dockets have high housekeeping volume. These entries are not substantive but they are legitimate docket events.

**Mitigation:**
- Per-channel signal-type taxonomy in `LITIGATION_STRATEGIES.md` is a *whitelist*. Any docket entry that doesn't map to an enumerated signal type is discarded.
- Whitelist is specific: "motion to dismiss denied" is in; "motion to dismiss filed" (procedural, not outcome-determining) is admitted at lower strength.
- Whitelist expansion is a D-0XX decision, not a silent code edit.

---

## F-08 — Time-zone and clerk-entry-time skew

**Symptom:** A ruling issued at 4:45pm ET appears in the docket at 5:30pm ET. The 6pm scanner catches it. Good. But a ruling at 11:55pm ET may appear at 12:10am ET the next day; the per-day filter then cuts it out of "today's" signals.

**Why it happens:** Courts run on local time. PACER's timestamps are UTC-based but the clerk-entry time reflects local clerk office hours. Filters on calendar days introduce fencepost errors.

**Mitigation:**
- Scan window is always a rolling N-hour window, not a calendar-day window. Window size equals cadence × 1.5 (e.g., 6-hourly scanner uses a 9-hour window) to ensure no entry is missed across cycle boundaries.
- Dedup on `(court, case_number, docket_entry_id)` prevents re-emission.

---

## F-09 — PTAB final-written-decision partial cancellations

**Symptom:** A PTAB FWD lists 20 challenged claims, cancels 3 of them, preserves 17. Scanner emits a `ipr_final_written_decision` signal with strength estimate = "FWD" without parsing the claim-level outcome. Deep dive then has to do the work that the scanner could have done.

**Why it happens:** PTAB decisions are long PDFs with tabular claim-by-claim outcomes. Parsing them requires more than a simple keyword scanner.

**Mitigation:**
- Phase 3 scanner for PTAB includes a lightweight claim-outcome parser — extracts "claim X: unpatentable" vs. "claim X: not unpatentable" lines from the FWD's disposition table.
- Where parsing fails, signal is emitted with `raw_data.outcome_parse_status = "failed"` and deep-dive is manual.

---

## F-10 — ITC remand and reconsideration loops

**Symptom:** USITC issues a Final Determination. Then the Commission *reconsiders*. Then the Federal Circuit *remands*. Each is a new event that re-opens a supposedly-closed case.

**Why it happens:** ITC has more procedural layers than district court (ALJ → Commission → Fed Cir → remand → Commission again). Signals can be emitted multiple times for the same underlying case.

**Mitigation:**
- ITC scanner tracks investigation-number lifecycle. A closed investigation that sees new activity is emitted as a distinct `itc_remand_event` signal type, not as a fresh FD.
- Dedup on `investigation_number + event_type + event_date`.

---

## F-11 — Delaware Chancery opinion vs. docket entry timing

**Symptom:** Chancery issues an opinion. It appears on the opinions RSS feed. Several hours later, the docket-entry for the opinion appears in the free docket search. Scanner configured for the docket-search sees the event 4 hours later than it could have via RSS.

**Why it happens:** Delaware's RSS is fast (within minutes). Its docket search is slow and sometimes hours behind.

**Mitigation:**
- Chancery scanner uses **both** sources: RSS as the fast-notification path, docket search as the metadata-enrichment path. A signal is emitted on RSS hit with partial data; enriched on subsequent docket-search hit.
- The initial-partial and enriched signals share a stable dedup key (case number + opinion date).

---

## F-12 — SEC enforcement Wells Notice leakage vs. release

**Symptom:** A Wells Notice is disclosed in a 10-Q risk factor ("the Staff has advised us..."). Two months later, the SEC announces the enforcement action. Scanner emits a signal at announcement; analyst who read the 10-Q already priced it in.

**Why it happens:** Wells Notices are not themselves public. 10-Q risk-factor disclosure is voluntary and not uniform.

**Mitigation:**
- Tool 1's EDGAR scanner (if integrated with Tool 3 via cross-tool analyzer, per D-001) catches the risk-factor disclosure separately. Tool 3's SEC enforcement signal is still emitted at release; convergence across tools reveals the pre-announcement leakage. Edge-decay dimension penalizes the already-leaked thesis.

---

## F-13 — DOJ/FTC press-release silence on Second Requests

**Symptom:** A Second Request is issued. DOJ/FTC does not press-release every Second Request — many are disclosed only by the deal parties in 8-Ks or proxy filings. Scanner on DOJ/FTC press releases misses the issuance.

**Why it happens:** Second Requests are confidential; public acknowledgment is voluntary and done by the deal parties, not the agency, except in high-profile cases.

**Mitigation:**
- Tool 3's DOJ/FTC scanner targets the public press releases only. The Second Request → 8-K path is Tool 1's domain (EDGAR 8-K scanning for "Second Request" keyword). Cross-tool convergence (Tool 1 + Tool 3) captures the full picture. Tool 3 alone is not expected to catch all Second Requests.

---

## F-14 — Resolution-cache poisoning from a single-session mis-classification

**Symptom:** In an early session, a scanner mis-resolves "Alpha Holdings LLC" to the wrong public company. The resolution is cached. Every subsequent session reuses the cached (wrong) resolution.

**Why it happens:** Monotonic caches amplify early errors.

**Mitigation:**
- Every cache entry has `last_verified` timestamp. Maintenance task periodically re-verifies a rotating subset of the cache against fresh EDGAR lookups.
- Cache audit: if a resolution result changes between original and re-verification, flag as `cache_drift` in OPEN_QUESTIONS and invalidate all signals derived from that resolution.
- Phase 1 validation set re-runs every 30 days; a drop in precision is the earliest signal of cache poisoning.

---

## F-15 — Wall-clock overruns from slow courts

**Symptom:** Delaware Chancery HTML search takes 90 seconds to respond to a single query. Scanner blows its 45-second budget, 120-second subprocess hard-kill fires, partial results.

**Why it happens:** Government sites are slow and unpredictable.

**Mitigation:**
- Per-scanner timeouts in the scanner code; graceful degradation when upstream is slow.
- Delaware scanner specifically uses the fast RSS first and only falls back to HTML search when RSS gaps exist.
- Maintenance task tracks per-scanner p95 wall-clock time and opens `OPEN_QUESTIONS` when any scanner exceeds its budget consistently.

---

## F-16 — Rate-limit trips from aggressive initial scans

**Symptom:** Phase 1 endpoint validation scripts hit rate limits on CourtListener or USITC. The same IP gets temp-blocked. Subsequent scheduled tasks fail for 1–24 hours.

**Why it happens:** Validation scripts are typically not rate-limit-aware at first.

**Mitigation:**
- Every endpoint probe and validation call uses the production scanner's rate-limit logic, not a one-off `requests.get`.
- Rate-limit responses are logged; if a 429 is seen, the scanner sleeps and retries with exponential backoff, and emits a `rate_limited` warning into `SESSION_STATE.md`.
- Maintenance task monitors for sustained rate-limit issues.

---

## F-17 — Over-signaling from one mega-case

**Symptom:** A major antitrust case (Google, Apple, Microsoft) generates dozens of docket entries per week. Scanner emits scores of signals, swamping the daily report.

**Why it happens:** Big cases have big dockets.

**Mitigation:**
- Dedup at signal-type level per case per rolling 7-day window: the first `motion_practice` signal on a case in a 7-day window is emitted; subsequent are suppressed unless they are a distinct signal type.
- Daily report groups signals by case, not just by channel.
- A case generating > 5 signals in a 7-day window is flagged as "active litigation" in the daily report's own section.

---

## F-18 — Seasonal docket slowdowns

**Symptom:** Mid-July through Labor Day, federal dockets quiet significantly (summer recess for judges and counsel). Scanner produces 70% less volume. Maintenance task may misread as a system failure.

**Why it happens:** Legal seasonality is real.

**Mitigation:**
- Maintenance task's signal-volume health check compares to rolling 90-day moving average, not to all-time. Seasonal drops are expected and not alerted.
- Scanner uptime metrics (API reachability, scan completion rate) are monitored separately from signal-volume metrics.

---

## F-19 — Judge-effect not modeled

**Symptom:** Motion-to-dismiss grants vary 3× by judge (some grant 60%+, some grant 15%). Signal Strength scoring assumes population averages. Result: over-scoring of signals in hostile-judge districts, under-scoring in pro-plaintiff ones.

**Why it happens:** Per-judge priors are real but require a dataset to learn.

**Mitigation:**
- v1 accepts this as a scoring-precision limitation.
- Phase 8+ opens judge-effect modeling as a scope candidate. Requires accumulating 6+ months of scan data first.
- Documented as an open research question in `OPEN_QUESTIONS.md` Q-001 at Phase 0.

---

## F-20 — Cross-tool attribution (Tool 1 and Tool 3 both see the same event)

**Symptom:** Tool 1's EDGAR scanner catches the 8-K disclosing a lawsuit. Tool 3's PACER scanner catches the complaint filing. Both produce candidates on the same entity. Without coordination, analysts see two candidates for one thesis.

**Why it happens:** Cross-tool convergence requires an analyzer project (per D-001); absent that, redundant candidates emerge.

**Mitigation:**
- Per D-001, cross-tool analyzer is a fourth project, not a Tool 3 concern.
- Until it exists, Tool 3 daily reports include a "known-to-Tool-1" section that notes candidates whose underlying event might also appear in Tool 1's pipeline. This is informational; dedup happens manually for now.
- Building the cross-tool analyzer is a Phase 8+ candidate in `LITIGATION_PHASING.md`.
