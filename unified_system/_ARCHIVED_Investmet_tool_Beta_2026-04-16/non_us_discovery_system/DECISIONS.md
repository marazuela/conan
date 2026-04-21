# DECISIONS — Non-US Primary-Source Discovery System (Tool 2)

Numbered strictly sequentially. A past decision is reopened only if concrete new evidence invalidates it — then a new decision is appended, not the old one edited.

---

## D-000 — Founding architecture: 9-scanner non-US primary-source system, structurally independent from Tool 1

Date: 2026-04-14
Context: Tool 1 covers US-listed equities via 5 scanners (EDGAR, ESMA shorts, Congressional trading, US contract awards, FDA PDUFA). Its universe is ~100% US-centric, English-language, catalyst-shaped. The operator (Pedro) requested a complementary system that covers everything Tool 1 structurally cannot — non-US markets, non-English primary sources — so that the two systems running continuously produce a combined candidate stream with zero universe overlap.

Decision: Build Tool 2 as a fully independent system following the same project-template architecture as Tool 1, with nine scanners, one per non-US exchange: UK LSE RNS, Japan TDnet, Australia ASX, Canada SEDAR+, Hong Kong HKEx, Korea KIND, India BSE/NSE, Brazil CVM, Mexico BMV. Mandate: publicly listed equities, ≥ USD $300M market cap, weeks-to-months horizon, satellite sizing, free public data only. Deliverable shape identical to Tool 1 — per-candidate markdown writeups for 28+ scores, daily reports, convergence-detection, kill-condition monitoring.

Independence is enforced at the file-system level: Tool 2 has its own folder, its own `SESSION_LOCK.md`, its own signal log, its own candidates folder, its own scheduled tasks. The two systems do not read or write each other's files. Cross-system convergence (an entity appearing in both Tool 1's and Tool 2's signal logs) is the domain of a separate analyzer project the operator maintains independently.

Alternatives considered:
1. Single shared signal log across Tool 1 and Tool 2 — rejected because it would add more writer tasks to Tool 1's lock, breaking concurrency invariants, and would expose Tool 1 to bugs in Tool 2.
2. Five-scanner reduced scope (skip India, Brazil, Mexico, Korea) — rejected; operator explicitly chose full coverage of the nine target exchanges.
3. Build scanners in parallel (all 9 simultaneously) — rejected per template Part 16; architectural bugs surface cheapest on the first scanner, then the pattern replicates.

Implications:
- Folder: `non_us_discovery_system/` (producer only). The sibling `reporting_layer/` was removed 2026-04-15 when reporting was consolidated into the project-root `Reporting Hub/`.
- Build order: UK first (canary), then Japan (largest universe, test translation stack), then the remaining seven.
- Same 7-dimension scoring rubric as Tool 1, inherited; re-tuned only after empirical data justifies it.
- OpenFIGI resolver ported from Tool 1 as-is; it's already geography-agnostic.

---

## D-001 — Cross-listing-aware convergence deduplication

Date: 2026-04-14
Context: Major international issuers are frequently dual- or triple-listed (HSBC on LSE + HKEx, Rio Tinto on LSE + ASX + SEDAR, BHP on ASX + LSE). When an issuer publishes a material announcement, the same event typically hits multiple exchange disclosure portals within hours. Without explicit handling, the convergence engine would treat these echoes as three independent cross-strategy signals and falsely flag high-conviction convergence, when in fact one event has been counted three times.

Decision: Convergence keys on `issuer_figi` (the composite FIGI of the ultimate issuer) rather than `figi` of a specific share class. Before confirming convergence, the convergence engine compares pairwise `source_content_hash` values; if similarity exceeds a threshold, the signals are deduplicated to one and convergence is not claimed from that pairing alone. A second independent event (different content) on a second exchange is still required to trigger convergence.

Alternatives considered:
1. Key convergence on `figi` only — rejected, fails by construction for cross-listings.
2. Ignore cross-listing entirely — rejected, produces systematic false positives for the exact names the system is most interested in.

Implications:
- Signal schema must carry `source_content_hash` (sha256 of the filing body).
- Convergence engine has a content-similarity comparison step before any bonus is awarded.
- Maintenance task audits the dedup log to ensure it isn't over- or under-dedupling.

---

## D-002 — Translation-direction honesty: default to `unknown`

Date: 2026-04-14
Context: Non-English scanners depend on translation of foreign-language filings to extract direction (long / short / neutral). Translation is error-prone at precisely the moments that matter most — a Japanese negation ("will *not* exceed") flipped to a positive ("will exceed") creates a confidently wrong direction call on what might otherwise be a high-score signal. Confidently wrong directions are worse than low-volume signals.

Decision: Every non-English scanner emits `translation_confidence` (0.0 – 1.0) per directionally-relevant passage. If confidence < 0.85 on the passage(s) that drive direction, `thesis_direction` is set to `unknown`. Scoring penalizes `unknown` direction (Signal Strength dimension caps at 2 instead of 5 when direction is unknown). Signals with translation confidence < 0.70 on any critical passage are triaged out at Stage 1 entirely.

Alternatives considered:
1. Trust all translations equally — rejected, produces worst failure mode (confidently wrong).
2. Always emit direction as `unknown` for non-English sources — rejected, wastes legitimate signal when translation is clearly unambiguous.

Implications:
- Per-scanner translation-confidence calibration is required before production.
- Scoring rubric explicitly addresses `thesis_direction = unknown` (see `framework/scoring_system.md`).

---

## D-003 — Ticker + MIC as sole entity identifier for resolution

Date: 2026-04-14
Context: Company names in non-Latin scripts (Japanese 株式会社, Korean 주식회사, Chinese 有限公司) make fuzzy name-matching unreliable. Multiple distinct issuers in the same jurisdiction can share significant portions of their local name. OpenFIGI resolution works reliably from `ticker + MIC` but is noisy from company-name lookups.

Decision: Every signal must carry `ticker_local` and `mic`. Entity resolution to FIGI uses `ticker + MIC` exclusively. If the source filing does not include a ticker, the scanner extracts it from the issuer identification block of the filing (every disclosure regime requires this). Signals without a resolvable ticker + MIC are logged to `working/unresolved_entities.md` and excluded from convergence. Company-name translation is carried on the signal for *display* only — it is never a key.

Alternatives considered:
1. Fuzzy name matching as fallback — rejected; produces silent collisions that are worse than explicit misses.
2. Use only ISIN — acceptable but ISIN is not always present in non-Latin-script filings; ticker + MIC is more reliably available.

Implications:
- Each strategy spec mandates ticker extraction as the first parsing step.
- Unresolved-entities log is reviewed weekly by maintenance task; persistent failures become open questions.

---

## D-004 — Cross-listing convergence storm mitigation (companion to D-001)

Date: 2026-04-14
Context: D-001 describes the mechanism (dedup by `source_content_hash` similarity). D-004 operationalizes the content-similarity threshold and the handling protocol when dedup collapses a potential convergence.

Decision: Content-similarity threshold is 0.80 on a normalized Jaccard of token sets extracted from the first 500 words of each filing, after stripping exchange-specific boilerplate. Two signals scoring ≥ 0.80 are considered the same underlying event and merged into one signal in the convergence engine; the engine retains both source URLs in the merged signal's `raw_data.echoed_sources` for traceability. Signals < 0.80 are treated as independent.

When the convergence engine detects and merges a cross-listing echo, it logs the event to `signals/cross_listing_dedup_log.json` so maintenance can audit the dedup rate. If dedup rate exceeds 30% of cross-strategy pairs in a week, the threshold is likely too loose; if below 5%, likely too tight. Review monthly.

Alternatives considered:
1. Exact-match hash dedup only — rejected; cross-exchange filings often differ in boilerplate wording even when the event is identical.
2. Fixed time-window dedup only (within 24 hours, drop duplicates) — rejected; events sometimes propagate across exchanges with a multi-day lag.

Implications:
- Convergence engine has a content-similarity function (Jaccard over normalized token sets).
- Boilerplate-stripping regex lists live in `tools/boilerplate_filters.py` per exchange.
- Monthly maintenance audit of dedup rate is added to the maintenance task checklist.
