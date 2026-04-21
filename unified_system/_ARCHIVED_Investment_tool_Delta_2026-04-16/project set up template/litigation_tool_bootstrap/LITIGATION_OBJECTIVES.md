# OBJECTIVES — Litigation & Docket Signal System (Tool 3)

## Primary Goal

Build and operate an autonomous investment signal discovery system that identifies publicly-traded equity investment opportunities from legal-epistemology-native sources — federal and state court dockets, administrative-agency filings, patent-trial records, and regulatory-enforcement proceedings — where material information is generated days to months before the affected company's own disclosure of it. The thesis is that the legal system operates on a deterministic schedule (court calendars, statutory response windows, agency notice periods) that is publicly observable, but the company's Reg FD clock does not start until it "learns of" the filing, creating a structural timing gap a disciplined scanner can exploit.

The system is structurally complementary to Tool 1 (US-centric financial-disclosure catalyst discovery) and Tool 2 (non-US primary-source discovery). Every candidate Tool 3 produces is one Tools 1 and 2 categorically cannot find, because their sources do not see a litigation event until the defendant or plaintiff self-discloses — at which point the edge has decayed.

## Mandate

- **Universe:** US-listed equities, minimum USD $300M market cap, approximately 6,500 names. (Non-US extension deferred to Phase 8+ after US coverage is proven; see D-006 in DECISIONS seed.)
- **Edge type:** Structural disclosure-timing + format-hostility asymmetry. Court filings are legally public and indexable, but the parsing cost (docket free-text, caption-based party-naming, PDF exhibit OCR, bankruptcy-specific docket schemas) is high enough that no generalist equity-research workflow pays it. The edge persists because the *opposing party's lawyer* and the *docket* know about the filing before the company's IR team does.
- **Holding horizon:** Weeks to months. Most litigation signals resolve within 30–180 days of docket entry (service perfection, preliminary motions, Markman hearings, IPR institution decisions, HSR Second Request public notice).
- **Position sizing:** Satellite positions (2–5% of portfolio) — asymmetric risk/reward, same as Tools 1 and 2.
- **Constraint:** Legal public data only. PRAGMATIC funding policy (D-008 in seed): record docket-entry metadata and indices using fully-free sources (RECAP, CourtListener, ITC EDIS, PTAB E2E, SEC EDGAR enforcement, FTC/DOJ announcement pages, Delaware Chancery free slip-sheet index). Document bodies behind PACER paywall are flagged for user-directed manual pull; the tool does not spend PACER credits autonomously in v1.
- **Reporting:** Daily litigation-signal reports, full candidate writeups at 28+ scores, weekly docket-heatmap performance report. Output shape identical to Tool 1 / Tool 2 so convergence across tools can operate on a single schema.

## Scope — The Six Litigation Channels (v1)

| # | Channel | Source | Edge Rationale |
|---|---------|--------|----------------|
| 1 | **Federal Civil — PACER/RECAP** | CourtListener RECAP API + free RECAP archive; PACER index only when RECAP gaps | Federal civil is the highest-volume, highest-signal docket. Service-of-process can precede 8-K by 3–10 days. Markman orders, preliminary-injunction rulings, motion-to-dismiss decisions are binary market-movers. |
| 2 | **ITC Section 337 Investigations** | USITC EDIS (edis.usitc.gov) | Institution of a 337 investigation moves respondent stocks 5–15%. ITC publishes institution notice 3–7 days before most respondents 8-K it. Complainant's stock moves on filing of Complaint, ~2 weeks before institution. |
| 3 | **PTAB IPR (Inter Partes Review)** | USPTO PTAB End-to-End system (ptab.uspto.gov) | IPR institution decisions invalidate patents core to revenue streams. Schedule is deterministic (6 months from filing to institution, 12 months to final written decision). Extremely low coverage by equity analysts despite material outcomes. |
| 4 | **Delaware Chancery Court** | Delaware Courts free slip-sheet index + RSS; `courts.delaware.gov` public docket search | Chancery is THE court for M&A disputes (appraisal actions, Revlon claims, DGCL 220 books-and-records demands). Appraisal filings in announced deals are strong signals of deal-break risk. Free index is slow and HTML-scraping-only, hence the asymmetry. |
| 5 | **SEC Enforcement Docket** | SEC litigation releases + EDGAR administrative proceeding filings | SEC files litigation releases the day of enforcement action, often before the respondent company can 8-K. Wells Notices are not themselves public but the timing of the eventual release is. Enforcement against executives (not the entity) is especially under-covered. |
| 6 | **DOJ / FTC Antitrust — HSR Second Requests and Merger Challenges** | DOJ ATR announcement page + FTC press releases + PACER for filed challenges | Second Requests on announced M&A are public via press release and PACER filings, before the deal parties disclose extension of the HSR waiting period. Merger challenges filed in district court are tracked via PACER before companies 8-K. |

Combined target signal volume: 50–200 raw signals per week across all six channels at steady state, narrowing to 2–8 candidates per week after the 3-stage pipeline.

## Out of Scope for v1 (deferred)

- Federal criminal (mostly not equity-relevant; noise-heavy).
- State courts beyond Delaware (volume prohibitive; Delaware handles the highest-signal subset).
- Bankruptcy courts and adversary proceedings (Phase 2 — high signal but docket-schema is specialized).
- International litigation (EU competition enforcement, UK Competition Appeal Tribunal). Phase 3.
- Administrative warning letters (EPA, FAA, FDA). Phase 3 — Tool 1's FDA scanner already partially covers this.
- Class-action securities litigation — partially covered by Tool 1's EDGAR keyword scanner. Re-evaluate after v1 candidates are produced.

## The Six Strategy-to-Scanner Mapping

| # | Strategy | Planned tool file | Frequency |
|---|----------|-------------------|-----------|
| 1 | Federal Civil scanner | `tools/pacer_recap_scanner.py` | Every 6h |
| 2 | ITC 337 scanner | `tools/itc_337_scanner.py` | Every 12h |
| 3 | PTAB IPR scanner | `tools/ptab_ipr_scanner.py` | Daily |
| 4 | Delaware Chancery scanner | `tools/delaware_chancery_scanner.py` | Every 12h |
| 5 | SEC Enforcement scanner | `tools/sec_enforcement_scanner.py` | Every 6h |
| 6 | DOJ/FTC Antitrust scanner | `tools/doj_ftc_antitrust_scanner.py` | Every 12h |

See `LITIGATION_PHASING.md` for build sequence.

## Sub-Goals

1. **Signal infrastructure** — common JSON signal schema inheriting from Tool 1/2's shape; OpenFIGI entity resolution extended with a **party-name-to-issuer-FIGI resolver** (the core litigation-specific challenge, since courts caption parties by legal name, not ticker); internal convergence detection across the six channels; cross-channel dedup (one case can produce signals in both PACER and a Delaware Chancery feed).
2. **Quality over quantity** — every candidate survives the 3-stage pipeline (triage → scoring → deep dive). Target 2–8 high-conviction candidates per week.
3. **Party-resolution integrity** — court captions use legal-entity names, subsidiaries, DBAs, and acquired-company names. Resolution confidence is tracked per signal; signals with confidence < 0.80 are triaged out at Stage 1. (See D-003 in DECISIONS seed.)
4. **Docket-parse resilience** — docket free-text and PDF exhibits vary by court, judge, and even by clerk. Every scanner must tolerate parse failures without crashing the pipeline.
5. **Full autonomy** — scheduled Cowork sessions run the full pipeline without user intervention.
6. **Structural independence from Tools 1 and 2** — own folder, own lock, own candidates, own reports. Cross-system signal merging happens only in a separate analyzer project.

## Success Criteria

- [ ] All 6 Python scanner tools built, compiled, producing valid JSON signals in the common schema.
- [ ] Party-name-to-issuer-FIGI resolver operational with ≥ 80% precision on a validation set of 100 manually-labeled cases.
- [ ] OpenFIGI entity resolution operational for the tickered side of the resolver.
- [ ] Internal convergence engine detecting cross-channel overlap within 30-day rolling window (wider than Tool 1/2's 14-day window — litigation signals on the same entity often span weeks).
- [ ] Daily scheduled pipeline running: scan → triage → party-resolve → entity-resolve → converge → score → deep dive → report → kill-condition monitor.
- [ ] First batch of validated candidates produced with full deep dive analysis, including source-docket citation for every claim.
- [ ] System operates autonomously for 7 consecutive days without manual intervention.
- [ ] Zero PACER autonomous billing in v1 (flag-for-manual-pull pattern working correctly).

## Definition of Done

The system is "done" when a single scheduled Cowork session can: run all 6 scanners (or as many as are healthy that day), resolve parties to issuer FIGIs, triage and score the results, detect convergences within the 30-day window, produce or update candidate writeups for any signal scoring 28+, monitor existing candidates against kill conditions (case dismissed, settled, consolidated, stayed), and output a daily report — all without human input and without spending a single PACER credit. The user reviews at discretion; the system does not depend on the user being present.
