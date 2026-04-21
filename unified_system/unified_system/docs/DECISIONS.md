# Decisions Log

Chronological record of architectural and operational decisions. Format: D-NNN | Date | Title | Context | Decision | Consequences.

---

## D-001 | 2026-04-16 | Unified System Created

**Context**: 6 scattered project folders (Investment tool, Investmet tool Beta, Investment tool Delta, Investment tool Gamma, Reporting Hub, Independent review project set up) each with overlapping scanners, divergent scoring rubrics, and duplicated infrastructure. Maintenance burden growing; convergence across systems impossible.

**Decision**: Consolidate into `unified_system/` per `UNIFIED_SYSTEM_IMPLEMENTATION_PLAN.md`. Archive old folders under `_ARCHIVED_*_2026-04-16` (do not delete — preserve history).

**Consequences**: One signal log, one entity cache, one candidates folder. 3 scheduled tasks replace 10. Cross-scanner convergence becomes possible. Migration phase cost: ~6–8 scheduled sessions plus this scaffolding work.

---

## D-002 | 2026-04-16 | Five Profile Scoring System Adopted

**Context**: Tool 1's single 7-dimension rubric treated merger-arb, activist campaigns, FDA catalysts, short positioning, and litigation with one generic weight matrix. Dimensions like "approval probability" are meaningless for merger arb; "spread size" is meaningless for FDA binary.

**Decision**: Replace the single rubric with 5 profile-specific rubrics, each producing a 0–50 normalized score for cross-profile comparability:
- `merger_arb` — Spread × Deal Certainty × Annualized Return × Break Risk × Liquidity
- `activist_governance` — Signal Strength × Information Asymmetry × Activist Track Record × Risk/Reward × Catalyst Clarity × Edge Decay × Liquidity
- `binary_catalyst` — Approval Probability × Market Mispricing × Magnitude × Competitive Landscape × Timeline × Liquidity
- `short_positioning` — Crowding × Trend Direction × Catalyst Proximity × Size vs Float × Historical Analog × Liquidity
- `litigation` — Financial Materiality × Legal Outcome Probability × Market Pricing × Timeline × Liquidity × Party Resolution Confidence

**Consequences**: Each profile has its own auto-cap rules (sub-scale return for merger arb; EV<5% for binary; party confidence<0.85 for litigation). Scoring is now profile-specific, comparable across profiles, and aligned with how each signal type actually moves prices. Existing candidates need re-scoring under the new profiles.

---

## D-003 | 2026-04-16 | Market Cap Floor: $215M USD (≈€200M)

**Context**: Tool 1 used $215M; Tool 2 used $300M. Unified plan initially proposed $300M; Pedro directed €215M. Interpretation: preserve continuity with Tool 1's operational floor ($215M USD ≈ €200M).

**Decision**: Unified floor = **$215M USD** (≈ €200M). Applied universally across all scanners, all geographies.

**Consequences**: Tool 2 candidates between $215M–$300M (none currently active at that range, per Tool 2 SESSION_STATE) are now eligible. No existing candidate affected by the change.

---

## D-004 | 2026-04-16 | Read-Only Reporting Layer

**Context**: Tool 1's reporting was inlined into the operational task, producing noise and occasional race conditions. Previous attempt to spin up "Reporting Hub" as a separate project created coordination headaches.

**Decision**: The `unified-reporting` scheduled task is strictly read-only to operational data. Writes only to `reports/`. Uses `reports/REPORTING_LOCK.md` — independent from `SESSION_LOCK.md`. If it detects malformed JSON, retries 2s × 3 attempts then skips + logs. If it detects bad operational state, logs to `reports/working/issues_YYYY-MM-DD.log` — does NOT fix. Fixing is the maintenance task's job.

**Consequences**: Reporting can run in parallel with operational/maintenance tasks without lock contention. Separation of concerns clean. No risk of reporting task corrupting the signal log.

---

## D-005 | 2026-04-16 | PDF Reporting via reportlab

**Context**: Need PDF output for daily digests, candidate dossiers, weekly reports. Options considered: weasyprint (HTML→PDF, complex deps), wkhtmltopdf (system binary, fragile in sandbox), pypdf (reader/writer only, can't compose), reportlab (pure Python, direct PDF composition).

**Decision**: Use `reportlab` for all PDF generation. Install via `pip install reportlab --break-system-packages` at session start.

**Consequences**: Direct control over layout; no HTML/CSS intermediary. Slightly more verbose than template-based engines but deterministic and sandbox-friendly.

---

## D-006 | 2026-04-16 | Convergence Engine Redesign (Multi-Profile)

**Context**: Tool 1's convergence engine only spanned Tool 1's 5 US scanners. With 15+ scanners across geographies + profiles, convergence patterns like "EU short buildup + US PDUFA approaching on same cross-listed name" were invisible.

**Decision**: Redesign convergence engine to (a) group all signals by `issuer_figi` across all scanners, (b) use 14-day window for most profiles, 30-day for litigation (courts move slowly), (c) dedup by `source_content_hash` to catch cross-listing echoes, (d) classify as same-direction / orthogonal / contradiction, (e) apply +5 for 2 independent signals, +10 for 3+.

**Consequences**: Higher-quality convergence signals. Contradictions surface as flags requiring manual review rather than auto-promoting. Cross-listing echoes no longer double-count.

---

## D-014 (carried from Tool 1) | Subprocess Isolation

**Decision**: Each scanner runs as its own Python subprocess with 120s hard-kill timeout and 60–90s soft budget. Scanner crashes don't propagate.

**Consequences**: Pipeline resilient. Kept in unified system.

---

## D-018 (carried from Tool 1) | EDGAR Wall-Clock Budget

**Decision**: EDGAR EFTS calls capped at 35s wall-clock in this sandbox. Exceeds budget → scanner emits partial results with `partial=true` flag.

**Consequences**: Consistent behavior across sessions. Kept.

---

## D-047 (carried from Tool 1) | Operational + Maintenance Task Split

**Decision**: Operational task owns signal generation and candidate pipeline. Maintenance task owns health checks and fixes only. Same lock (mutual exclusion), but different scopes.

**Consequences**: Kept in unified system. Adds a third read-only reporting task (see D-004).

---

## D-052 (carried from Tool 1) | Atomic File Writes

**Decision**: Every file write is `tmp + fsync + rename`. Truncation on interruption eliminated.

**Consequences**: Kept universally. Every tool in this unified system must use atomic writes.

---

## D-007 | 2026-04-16 | Deferred New Scanner Builds

**Context**: 7 scanners need ground-up builds (HKEx, KIND, BSE/NSE, CVM, BMV, CourtListener, SEC enforcement). Building them all before turning on the system delays operational value.

**Decision**: Ship operational system with the 7 currently-working scanners (edgar, esma_short, fda_pdufa, congressional, lse_rns, tdnet, asx) + SEDAR+ once unblocked. Build new scanners in Phases 5–6 on the scheduled-task track. Stubs in `tools/` to reserve names.

**Consequences**: Faster time to operational. Planned scanners appear in `scanner_registry.json` with `status: "planned"` and `last_run: null`.

---

## D-008 | 2026-04-17 | Thesis-Required Promotion Rule

**Context**: Reporting PDF revealed ~53 "candidates" (12 TDnet MDs + 41 watchlist JSON stubs) with no written rationale thesis. Pedro's response: "How is it possible that we have a candidate and not a thesis on why it is a candidate? then how has it been classified as a candidate? this makes no sense."

**Decision**: No signal may be promoted to `candidates/` (Immediate or Watchlist band) without a written rationale thesis that includes: (a) situation, (b) why under-priced, (c) next catalyst + date, (d) named kill conditions. Enforcement lives in `tools/candidate_gate.py` (`promote_candidate()`). Any scanner or finalizer writing to `candidates/` must route through this gate. Rejected promotions are appended to `working/rejected_promotions_YYYY-MM-DD.json` — they remain visible for research follow-up, not silently dropped.

**Consequences**:
- Scanners that previously emitted raw signal stubs into `candidates/watchlist/` are disallowed from doing so directly.
- Legacy stubs (41 JSONs + 12 thin TDnet MDs) are demoted/archived; see D-009 for backfill plan.
- Reporting PDFs (executive + detail book) exclude anything without a valid thesis.
- The `candidate_template.md` already required these fields — this decision makes the requirement mechanical instead of aspirational.

---

## D-009 | 2026-04-17 | Thesis Backfill + Stub Demotion

**Context**: D-008 is a forward-only rule; legacy stubs need handling. Auditing `candidates/` found 20 MDs with valid theses, 13 MDs missing required fields, 41 JSON watchlist stubs with zero thesis content.

**Decision**:
1. Move all 41 JSON stubs in `candidates/watchlist/` to `candidates/rejected_pending_thesis/` — they are no longer counted as candidates until a thesis is written.
2. The 13 thin MDs are marked for backfill; research a real thesis per file OR demote to `rejected_pending_thesis/`.
3. The reporting PDFs draw ONLY from MDs passing the gate audit.

**Consequences**: Candidate count drops from ~75 to ~20 immediately. Future scans must produce theses at source, not let the operator backfill later.

---

## D-010 — Executive summary uses hand-curated per-candidate rationale cards (2026-04-17)

**Context**: The compact one-line-why table in `executive_summary.pdf` was not self-explanatory per Pedro's directive ("make a case for each of them and be more precise with next key dates ... self explanatory enough to decide whether to do a deep research on it"). A table cell can't carry the reasoning for a $1B+ merger-arb, a PDUFA binary, or a poison-pill/activist setup.

**Decision**: Executive summary is now a **card-per-candidate** layout where each card answers:
- **What** is happening (situation in 2-3 sentences)
- **Edge** — why it's interesting / non-obvious / mispriced (the thesis in 2-3 sentences)
- **Expect** — what realization looks like (upside / downside / probabilities)
- **When** — catalyst dates with precision (ISO dates when known; text windows otherwise)

Rationales are **hand-curated** and stored in `candidates/_curated_rationales.json`, keyed by uppercase ticker. `report_generator.generate_executive_summary()` reads this sidecar and renders cards. Candidates without a curated entry fall back to auto-extracted thesis text flagged "[Auto-extracted from thesis — curated rationale pending]".

**Supporting rules**:
- Whenever a candidate is added/updated, the rationale sidecar MUST be updated in the same session.
- The sidecar is versioned in-tree so updates are auditable.
- `_extract_catalyst_for_summary()` now prefers **forward-looking** dates (days ≥ -3) over historical signal-discovery dates; falls back to parsing the curated `when` field; last-resort to any past date.

**Artifacts**:
- `unified_system/candidates/_curated_rationales.json` — 9 hand-curated entries (TVTX, VRDN, AXSM, VERA, RPAY, AVNS, GSAT, SEM, RGR)
- `unified_system/tools/report_generator.py` — `generate_executive_summary()` rewritten for card layout + `_load_curated_rationales()` + `_fallback_rationale()`
- Outputs: `reports/candidates/executive_summary.pdf` (5 pages: 1 index + 4 cards), `reports/candidates/detail_book.pdf` (11 pages)

**Validation**: Both PDFs regenerate cleanly via `python3 tools/report_generator.py --both`; pypdf text-extraction confirms all 9 cards render What/Edge/Expect/When sections with substantive content and correct forward-looking catalyst dates (VRDN: T+74 PDUFA, VERA: T+81 PDUFA, AXSM: T+13 PDUFA, RGR: T+40 AM window).

---

## D-011 — Beginner-friendly rationale schema for executive summary (2026-04-17)

**Context**: Pedro's follow-up feedback after D-010: "I need more explanation for basic knowledge. I am not an expert in every field we are considering... I need rationale explained in a simple way with a hypothesis, a thesis and your expected outcome and why and potential price increase for each." He cited missing RPAY as the counter-example — the old schema didn't surface time-sensitivity clearly enough for a non-specialist to act.

**Decision**: The executive summary schema (v2) requires, per candidate:
- `one_liner` — one-sentence situation
- `hypothesis` — the specific bet, one sentence
- `thesis` — 2-5 sentences with enough plain-English context for a non-specialist (define domain terms like "poison pill", "merger arbitrage", "PDUFA" inline)
- `expected_outcome` — what happens if we're right, with probability weights where possible
- `price_targets` — dict with `reference_price`, `upside_base`, `upside_best`, `downside` (each a $ range + % + scenario label)
- `time_sensitivity` — explicit urgency label (VERY HIGH / HIGH / MEDIUM-HIGH / MEDIUM / LOW) + concrete entry window
- `kill_watch` — what would invalidate the thesis
- `catalyst_date_iso` — forward-looking ISO date

**Rendering changes (`generate_executive_summary()`)**:
- One full-page card per candidate (portrait LETTER, 0.55" margins).
- Index table adds a color-coded **Urgency** column (VERY HIGH red / HIGH dark-red / MEDIUM-HIGH orange / MEDIUM olive / LOW green).
- Each card: italicized one-liner, HYPOTHESIS section, THESIS in a tinted box with blue left border, EXPECTED OUTCOME, PRICE TARGETS as a 4-row colored table (blue labels, green upside rows, red downside), TIME SENSITIVITY in a yellow box with urgency-colored accent, KILL WATCH.
- New helper `_urgency_band(text)` maps free-text time_sensitivity to (label, color).

**Supporting rules**:
- Every curated rationale MUST define terms of art inline (first occurrence) so the reader doesn't need a glossary.
- RPAY-class situations with hour-to-week entry windows MUST use `VERY HIGH` in the time_sensitivity prefix.
- When `reference_price` is stale (>7 days), the report generator flags the card; the operator re-queries yfinance and updates the sidecar.

**Artifacts**:
- `unified_system/candidates/_curated_rationales.json` — rewritten for all 9 tickers under v2 schema, reference prices anchored to 2026-04-17 intraday.
- `unified_system/tools/report_generator.py` — `_urgency_band()`, rewritten `_fallback_rationale()`, rewritten card renderer.
- Outputs: `reports/candidates/executive_summary.pdf` (10 pages: 1 index + 9 cards), `reports/candidates/detail_book.pdf` (unchanged structure).
- Published to `Conan/reporting/executive_summary.pdf` and `Conan/reporting/detail_book.pdf`.

**Validation**: pypdf text extraction confirms cards render with all 7 sections for RPAY (VERY HIGH urgency), AXSM/VRDN/VERA (HIGH), TVTX (MEDIUM), RGR (MEDIUM-HIGH), AVNS/GSAT/SEM (LOW). Plain-English term definitions confirmed in-line (poison pill, merger arb, activist investing, take-private, proxy fight, PDUFA).

---

## D-012 — Two-folder published reporting layout (2026-04-17)

**Context**: Pedro's feedback: "in the report folder i just need two folders. One that has a summary of all potential opportunities with the information discussed before. And another one with a file for each candidate that details the investment thesis." The reporting folder had accumulated stale timestamped PDFs, orphan dossiers, daily/weekly digests, duplicated copies, and a sibling `Conan/reporting/` that mirrored `unified_system/reports/candidates/`. Dossiers were also out of sync with the curated rationale JSON.

**Decision**: Collapse all published output into exactly two folders under `Conan/reporting/`:

```
Conan/reporting/
├── summary/
│   └── executive_summary.pdf       (all 9 candidates as rich cards)
└── dossiers/
    └── {TICKER}.pdf                (one per active candidate)
```

Each dossier combines the card (page 1, identical to its entry in `summary/`) with the full analyst markdown (page 2+). Both outputs are regenerated from the same source (`candidates/{TICKER}_*.md` + `candidates/_curated_rationales.json`) so they cannot drift.

**Code changes**:
- `tools/report_generator.py` adds `PUBLISH_ROOT = REPO.parent / "reporting"`, `PUBLISH_SUMMARY_DIR`, `PUBLISH_DOSSIERS_DIR`.
- New `_render_card(story, s, c, rat, cat)` is shared between `generate_executive_summary()` and `generate_published_dossier()`.
- New `generate_published_dossier(candidate)` emits `{TICKER}.pdf` with card on page 1 + markdown background on page 2+.
- New `publish_reporting()` does a full refresh: clears stale dossiers, emits summary, emits one dossier per active candidate.
- CLI adds `--publish` flag. `_collect_all_candidates()` now attaches `_source_path` so dossiers can read their own .md.

**Folder moves**:
- `unified_system/reports/candidates/2026-04-17_*_candidates_summary.pdf` → `_archive/2026-04-17_restructure/old_reports/`
- `unified_system/reports/dossiers/pdf/*.pdf` (April 16 JPX orphans) → `_archive/2026-04-17_restructure/orphan_dossiers/`
- `unified_system/reports/daily/*.pdf` and `weekly/*.pdf` → `_archive/2026-04-17_restructure/{daily_digests,weekly_digests}/`
- Old `reporting/detail_book.pdf` and `reporting/executive_summary.pdf` → `_archive/2026-04-17_restructure/old_reporting/`
- Root-level scaffolding `.md`s (CONTEXT, INSTRUCTIONS, OBJECTIVES, OPEN_QUESTIONS, PROGRESS_LOG, SESSION_LOCK, SESSION_STATE, INDEX, UNIFIED_SYSTEM_IMPLEMENTATION_PLAN) → `unified_system/docs/`
- `DECISIONS.md` → `unified_system/docs/DECISIONS.md`
- New `Conan/README.md` as the single entry point: "open reporting/".

**Validation (`python3 tools/report_generator.py --publish`)**:
- `reporting/summary/executive_summary.pdf` — 10 pages (1 index + 9 cards).
- `reporting/dossiers/*.pdf` — 9 files: AVNS, AXSM, GSAT, RGR, RPAY, SEM, TVTX, VERA, VRDN.
- RPAY dossier: 8 pages, VERY HIGH urgency on page 1, background on pages 2–8.
- Each dossier page 1 matches its corresponding card in the summary (same hypothesis / thesis / price targets / urgency band).

**Consequences**:
- Old `generate_candidates_summary()`, `generate_detail_book()`, `generate_daily_digest()`, `generate_weekly_strategic()` remain available but are no longer the default output. Scheduled tasks that referenced them still work; going forward, `--publish` is the canonical regen command.
- Any scheduled task referencing `unified_system/reports/candidates/*.pdf` needs to be repointed at `Conan/reporting/summary/executive_summary.pdf`.

---

## D-013 | 2026-04-17 | Pre-Edge-Only Candidate Mandate (Post-Edge Disqualifier)

**Context**: Pedro's feedback after validating the April 17 executive summary. Several candidates in the system were post-catalyst-resolution and carried no information edge: TVTX had already received FDA approval (stock +34%), AVNS's $25 take-private deal had been announced (stock +67%), GSAT's Amazon acquisition was public (deal fully priced). These candidates were surfaced as if they were still actionable. Pedro: "There is no point on identifying candidates that have already announced publicly an M&A offer and stock price has spiked. We need to identify these potential opportunities in advance, if not information is not useful."

**Decision**: Enforce a pre-edge-only policy across the pipeline.

**Post-edge disqualifier rule**: a candidate is surfaced if and only if the market has not yet priced the catalyst. The following automatically disqualify a candidate from the active pool:
1. FDA has issued an approval or CRL for the relevant drug/indication — archive with outcome noted.
2. A definitive merger agreement has been signed and publicly announced — archive with outcome noted.
3. An activist has gone fully public with a priced take-out offer AND the stock has absorbed the expected response window.
4. A proxy fight date is set AND the stock has already repriced for the expected outcome (typical activist bump is +15-25% on AGM-date announcement).
5. Merger-arb spreads below ~5% with standard timeline — these are post-edge merger-arb, not pre-edge opportunities.

**Operational gate**:
- `_curated_rationales.json` gains a `_archived` block with per-ticker entries: `{archived_date, archive_reason, outcome, former_one_liner, lesson?}`.
- `report_generator.py` adds `_load_post_edge_archive()`. `_collect_all_candidates()` filters out any ticker present in `_archived`. These candidates cannot appear in `executive_summary.pdf` or `dossiers/*.pdf` regardless of their `.md` file or stage.
- Archived `.md` files move to `candidates/_archived_post_edge/` to keep the active candidates folder clean.

**Application to the April 17 candidate set**:
| Ticker | Decision | Reason |
|---|---|---|
| TVTX | ARCHIVE | FDA approved FILSPARI 2026-04-13; stock $30.70 → $41.10 |
| AVNS | ARCHIVE | AIP $25/share take-private announced 2026-04-14; stock +67% |
| GSAT | ARCHIVE | Amazon $90/share all-cash announced 2026-04-14 |
| SEM | ARCHIVE | $16.50 take-private announced March 2; spread ~4%, no edge |
| RPAY | KEEP (borderline) | Forager $4.80 offer public 2026-04-17, but 51% spread means board-response outcome is still unpriced |
| AXSM | KEEP | PDUFA April 30 has not resolved |
| VERA | KEEP | PDUFA July 7 has not resolved |
| VRDN | KEEP | PDUFA June 30 has not resolved |
| RGR | KEEP | AGM May 27; poison-pill waiver binary still unresolved |

**Consequences**:
- Active pool drops from 9 to 5.
- A "delivered/missed" lesson attaches to AVNS: the system had no mechanism to flag AVNS 60-90 days pre-deal. This motivates a new takeover-candidate scanner (see D-014 when that scanner is scoped).
- Every candidate rationale must now answer: "what is the earliest moment this edge disappears?" The answer becomes the implicit kill-watch for the name.
- Missing lane identified: pre-announcement M&A target identification and pre-Phase-3-readout biotech identification. Both require new scanners (spec pending).

---

## D-014 | 2026-04-20 | Sixth Scoring Profile `takeover_candidate` Added (Amends D-002)

**Context**: D-002 (2026-04-16) adopted a five-profile scoring system. D-013 (2026-04-17) flagged a missing lane for pre-announcement M&A target identification and promised D-014 when the scanner was scoped. Session 69 (2026-04-20) shipped the `takeover_candidate_scanner` and added `takeover_candidate` to `run_post_scan.py::WEIGHTS` as the sixth profile, plus four auto-cap rules. PRD v2 §6 already reads "the six scoring profiles" and the v2 spec.md §12 seeds 6 profiles into the `rubrics` table at `rubric_version=1`. D-002's "5 profile-specific rubrics" language is stale.

**Decision**: Six scoring profiles, not five. Append-only amendment — D-002 stays as-is for historical trace; this entry is authoritative on the count and composition going forward.

Profiles are: `merger_arb`, `activist_governance`, `binary_catalyst`, `short_positioning`, `litigation`, `takeover_candidate`.

`takeover_candidate` auto-caps (per `run_post_scan.apply_auto_caps`, preserved verbatim in v2's `rubric_engine`):
- `post_edge_disqualified` — band → `discard`. Signal row still written for audit; never alertable. Enforces D-013's pre-edge mandate.
- `prior_rejection_cap` — band → `archive` when the same target has previously rejected an offer within the lookback window.
- `going_concern_cap` — band → `watchlist` (prevents distressed-target false positives from escalating to immediate).
- `below_triage_gate` — band → `discard` when `patterns_hit < 2` (fewer than two filer-allowlist or structural-pattern matches).

**Consequences**:
- v2 `rubrics` table seed (spec.md §9.1) inserts 6 rows at `rubric_version=1`, one per profile.
- The pre_phase3_readout scanner (added alongside) uses the `binary_catalyst` profile, not a new 7th profile.
- Any future reference to D-002's "five profile" language should be read in the context of this amendment. Spec.md §12 and PRD §6 are already correct; no other docs require edits.
- DECISIONS.md remains append-only per the register convention; D-002 is not edited in place.
