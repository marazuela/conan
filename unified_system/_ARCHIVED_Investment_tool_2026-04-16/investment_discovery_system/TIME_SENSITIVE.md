# Time-Sensitive Investments — Key Date Tracker

**Purpose**: Consolidated, chronologically-sorted view of all active and watchlist candidates with their next material event. Updated every scheduled session.

**Last updated**: 2026-04-16 16:25 UTC by Scheduled Session 68
**Today's date**: 2026-04-16
**Next update**: Next operational scanner run (approx. 2026-04-16 19:00 UTC / 2026-04-17 00:00 UTC)

---

## How to Read This File

Every row is a live investment thesis with a specific, dated catalyst. Rows are sorted by next key date, soonest first. The urgency tier helps prioritize attention:

- 🔴 **URGENT** — next key date within 14 calendar days. Daily monitoring active.
- 🟡 **APPROACHING** — next key date within 14–45 days. Weekly monitoring.
- 🟢 **TRACKING** — next key date >45 days out. Routine monitoring.
- ⚫ **WATCHLIST** — signal detected but below active-candidate threshold, or event window is extended.

Scoring: active candidates are ≥28.0 on the 7-dimension rubric. Watchlist is 22–27.9.

---

## Active & Watchlist Candidates (sorted by next key date)

| # | Tier | Ticker | Score | Reason for Investment | Tool / Strategy | Next Key Date | What Happens Then | Current Price | Mcap |
|---|------|--------|-------|----------------------|-----------------|---------------|-------------------|---------------|------|
| 1 | 🔴 URGENT | **[AXSM](candidates/AXSM_ADA_PDUFA.md)** | 30.75 | AXS-05 sNDA for Alzheimer Disease Agitation (ADA). Priority Review, no AdCom, 3/4 pivotal Phase 3 trials positive, Auvelity safety database exonerates major class-safety concerns, commercial prep (sales force 300→600, balipodect acquisition) inconsistent with internal CRL expectation. Thesis: approval probability ~60–70% vs. market implicit ~45–55%. Expected-value mispricing ~4.5%. | FDA PDUFA Calendar (`tools/fda_pdufa_pipeline.py`) | **2026-04-30** (T-14, intensive window active) | **FDA PDUFA decision on AXS-05 for ADA**. Binary event. Approval → expect +25–35%. CRL → expect -35–50%. **S68**: CLEAN — kill-sweep #30 all-clear at T-14. SEC submissions API on CIK 0001579428: no new 4/144/8-K Apr 14–16. Latest filing still Apr 1 8-K (pre-window). Price $184.79 (+0.33% from S67). Thesis intact. | $184.79 | $9.45B |
| 2 | 🔴 URGENT | **[RPAY](candidates/RPAY_Forager_ActivistPoisonPill.md)** | **≈33.0** (S64 sustained) | **DUAL ACTIVIST CONFIRMED + VERADACE TRACK RECORD VERIFIED.** (1) Forager Fund L.P. 12.9% (Apr 13 13D/A #2). (2) Veradace Capital Management LLC Schedule 13D Apr 15 disclosing **8.6% stake** (7,355,504 shares / $31.3M, avg ~$4.26/sh), 13G→13D flip after Apr 8 engagement. Named grievance: KUBRA opposition + 2 board-seat demand. **S65 DISCOVERY**: Veradace CIK 0001772351 has 21 prior filings (2020–2026) and 3 Schedule 13Ds total — Veradace is an experienced multi-campaign activist (also running 16.5% Mar 10 Schedule 13D on SoundThinking SSTI). **S68**: No new Apr 16 filings from any Repay-related filer (Forager, Veradace, Company). Watching for board response / 8-K / PREC14A. | EDGAR Governance + Activist rotations (`tools/edgar_filing_monitor.py`) | **Days–weeks** (expect Veradace / Forager responsive filings, possible joint group 13D, PREC14A) | Two independent activists (both verified credible) pushing for board representation at $278M micro-cap. Upside 30–60% on strategic review / sale / settlement. Downside 15–25% if both capitulate. | $3.17 | $278M |
| 3 | 🟡 APPROACHING | **[AVNS](candidates/AVNS_merger_arb_AIP_2026-04-14.md)** | 28.5 | American Industrial Partners (PE) all-cash acquisition at $25.00/share (72.1% premium to Apr 13 close). **S64 CONFIRMATION**: DEFA14A filings Apr 14 (2 filings: 0001606498-26-000050 + 0001104659-26-043093) + 8-K Apr 14 re-verified. Item 1.01 definitive merger agreement with A-AV Holdco I / A-AV MergerSub, all TRSU/PRSU/Options cashed at $25. Standard PE take-private — low antitrust risk. Spread $0.36 (1.46%) — market pricing ~95%+ deal completion. **S68**: No new filings Apr 16; 2026 Annual Meeting previously scheduled Apr 21 postponed pending special meeting for merger vote. | EDGAR Keyword Scanner — mna category | **~Late May 2026** (DEF 14A definitive proxy expected) | DEF 14A will set meeting date + record date. HSR waiting period (30 days from filing) expected to clear ~mid-May. Closing target H2 2026. Any topping bid or HSR second request moves stock and timeline. | $24.64 | $1.15B |
| 3a | 🟡 APPROACHING | **[RGR](candidates/RGR_Beretta_ProxyFight.md)** | ≈36.0 | **Beretta Holding S.A.** (9.95% holder) running TWO PARALLEL TRACKS: (1) SC TO-C tender for 20.05% at $44.80/sh cash (would push Beretta to 30.0%) — BLOCKED by 15% poison pill. (2) PREC14A proxy fight Apr 7, 4-nominee slate incl. Robert Eckert. **S65 DEEP-PARSE of RGR PRER14A**: Annual Meeting May 27, 2026 at 9am ET; settlement channel ACTIVE (Ruger sent Beretta draft cooperation agreement Apr 11 based on Apr 2 term sheet); authorized-shares amendment on AM ballot (40M→60M, +50%, defensive lever); board declined to amend rights plan Mar 28. **S67**: direct PRER14A parse re-confirmed "cooperation agreement" Apr 11 passage. **S68**: No new RGR filings Apr 16. Settlement channel remains active. | EDGAR Keyword Scanner — activist category | **May 27, 2026 Annual Meeting (T-41)**; settlement 8-K could come any day | **Binary vote on Beretta's 4-nominee slate** OR settlement (60–70% base rate for 10% activists). Tender offer at $44.80 vs market $41.93 = +6.8% embedded collar. **Next catalysts**: cooperation agreement 8-K (settlement path), RGR DEF 14A (typically 10-20 days from PRER14A), Beretta SC TO-T (tender commencement), ISS/Glass Lewis recs (early-mid May, T-15/T-20), Form 4 Beretta activity, authorized-shares amendment vote outcome at AM. | $41.93 | $669M |
| 4 | 🟡 APPROACHING | **MNKD** | 26.75 (watchlist) | Afrezza pediatric sNDA. $838M mcap, SI 7.5%. Watchlist pending T-20 re-evaluation. | FDA PDUFA Calendar | **2026-05-29** (T-43) | **FDA PDUFA decision on Afrezza pediatric**. Re-evaluate at T-20 (~May 8) — if signal strengthens, promote to active candidate. | $2.71 | $838M |
| 5 | 🟡 APPROACHING | **ARVN** | ~22.5 (watchlist) | Vepdegestrant NDA — first PROTAC drug candidate. Pfizer partnered. $701M mcap, SI 7.6%. Novel modality binary. | FDA PDUFA Calendar | **2026-06-05** (T-50) | **FDA PDUFA decision on vepdegestrant**. Deep dive at T-20 (~May 15). | $10.91 | $701M |
| 6 | 🟢 TRACKING | **ACHV** | Pending (sub-floor) | Cytisinicline smoking cessation PDUFA. Mcap ~$189M — **still below $215M floor** so currently not investable per triage rules. | FDA PDUFA Calendar | **2026-06-20** (T-65) | **FDA PDUFA decision on cytisinicline**. Monitor mcap; if ≥ $215M at T-30 (~May 20), run deep dive. | — (sub-floor) | ~$189M |
| 7 | 🟡 APPROACHING | **VRDN** | 26.00 (watchlist — demoted from 31.5) | Veligrotug IV for Thyroid Eye Disease. Demoted after REVEAL-1 SC Phase 3 miss + -26% on Apr 6. IV PDUFA intact but franchise smaller. $1.52B mcap, SI 8.3%. | FDA PDUFA Calendar | **2026-06-30** (T-75) | **FDA PDUFA decision on veligrotug IV (TED)**. Approval still likely; upside constrained. | $14.89 | $1.52B |
| 8 | 🟢 TRACKING | **[VERA](candidates/VERA_IgAN_PDUFA.md)** | 30.50 | Atacicept BLA for IgA nephropathy — first dual BAFF+APRIL inhibitor. ORIGIN Phase 3: 46% proteinuria reduction from baseline, p<0.0001. Priority Review + Breakthrough + Accelerated Approval. Commercial positioning edge vs sibeprenlimab. **S68**: CEO Marshall Fordyce Form 4 Apr 15 reporting Apr 14 sales — 14,130 sh @ $43.66, 7,921 sh @ $44.58, 900 sh @ $45.51 (weighted avg ~$44.00, total ~22,951 shares), affirmed as **10b5-1 plan** (`<aff10b5One>1</aff10b5One>` flag set). Form 144 Apr 14 is the prospective-sale notice. Same underlying plan as S67 (Jan 9, 2026 adoption + 95-day cooling-off). No material thesis change. | FDA PDUFA Calendar | **2026-07-07** (T-82) | **FDA PDUFA decision on atacicept**. Approval probability high; focus shifts to commercial positioning. Intensive window activates ~Jun 7 (T-30). | $42.19 | $3.03B |
| 9 | 🟡 APPROACHING | **[GSAT](candidates/GSAT_merger_arb_AMZN_2026-04-14.md)** | **≈30.0** (S67 sustained) | **Amazon definitive merger agreement SIGNED Apr 13 2026** (8-K filed Apr 14). $90 cash OR 0.3210 AMZN shares capped at $90. 40% cash proration. Downward adjustment max -$110M (~$0.79/sh) if HIBLEO-4 milestones missed. **S65 DEEP-PARSE of Apr 15 SC 13D/A Amendment No. 14 (FL Investment Holdings/Thermo)** + Exhibit 99.1 Support Agreement. **S67 ADDENDUM**: Primary-source parse of Monroe Capital SC 13D/A #14 (accession 0001193125-26-157479, event Apr 13). Item 4 confirms Monroe reporting-group aggregate = **45.75%** of Company Common Stock, Written Consent executed by Monroe for all its shares on Apr 13, and aggregated with Thermo stockholder-support agreement = majority threshold locked. **S68**: Termination fee for Company = **$419,832,000** confirmed in 8-K body. No new GSAT filings Apr 16. **Next formal filing = Schedule 14C Information Statement** (not S-4 proxy, because majority consent obtained). | EDGAR Keyword Scanner — mna + post-signing | **Schedule 14C filing (~late Apr – early May)**; S-4 registration for stock-election; FCC and HSR dockets; close 2027 | Schedule 14C (information statement, NOT proxy — since majority consent obtained) sets formal closing path. FCC spectrum transfer review (historically 12+ months; Verizon-TracFone precedent ~14 months). Regulatory delay is the primary real risk. At $79.80 vs $89.21 worst-case floor = **11.8% spread** over 15–20 months → 7–9% annualized with Amazon-scale buyer. | $79.80 | $10.26B |
| 10 | 🟢 TRACKING | **SRRK** | Future dive (T-30) | Apitegromab for Spinal Muscular Atrophy. SI 19.4%. | FDA PDUFA Calendar | **~2026-08-31** (T-137) | **PDUFA decision on apitegromab**. Deep dive at T-30 (~Aug 1). | — | — |
| 11 | 🟢 TRACKING | **SVRA** | Future dive (T-30) | Molbreevi for autoimmune pulmonary alveolar proteinosis. | FDA PDUFA Calendar | **2026-08-22** (T-128) | **PDUFA decision on Molbreevi**. Deep dive at T-30 (~Jul 23). | — | — |
| 12 | 🟢 TRACKING | **CAPR** | Future dive (T-30) | Deramiocel for Duchenne Muscular Dystrophy. | FDA PDUFA Calendar | **2026-08-22** (T-128) | **PDUFA decision on deramiocel**. Deep dive at T-30 (~Jul 23). | — | — |
| 13 | ⚫ WATCHLIST | **[SEM](candidates/SEM_Select_Medical_WCAS_TakePrivate.md)** | 22.5 (watchlist) | **Welsh Carson Anderson & Stowe XIV + Ortenzio family take-private at $16.50/sh all-cash.** PREM14A + SC 13E-3 filed Apr 15; Merger Agreement signed Mar 2, 2026. Outside Date Dec 1, 2026. Termination fees $66.504M / $133.010M. No go-shop. Special Committee in place (658 refs). At $16.38 current price the gross spread is only $0.12 = 0.73% (~1.1% annualized over 7.5 months) — too tight for active tier. **S68 entity-resolution note**: verified SEM's correct CIK is **0001320414** (NOT 0001320350 which is LENSAR/LNSR). No new SEM (0001320414) filings Apr 16; the only post-Apr 13 filings are the PREM14A + SC 13E3 Apr 15 already logged. | EDGAR Keyword Scanner — mna | **2026-12-01** Outside Date (T-229); DEF 14A expected 20–45 days after PREM14A | Watchlist triggers: spread >3%, HSR 2nd request, topping bid, MAC, or WCAS funding failure. Archive on close or vote >95%. | $16.38 | $2.03B |
| 14 | ⚫ WATCHLIST | **HROW** | 22.5 (watchlist) | $1.44B mcap, SI 21.1% — high short interest but no hard catalyst. Monitor only. | EDGAR + short-interest analysis | **No hard date** | Monitor for 8-K or short-squeeze trigger. | — | ~$1.44B |

---

## Near-Term Focus (next 60 days)

### 🔴 URGENT — within 14 days
- **AXSM on 2026-04-30 (T-14)**: intensive daily deep-check window active. S68 kill-sweep #30 all-clear. Price $184.79. Thesis intact. Single most time-sensitive event. Next check T-12 (Apr 18).
- **RPAY — catalyst undated but imminent**: dual-activist setup (Forager 12.9% + Veradace 8.6%); S68 no new filings — watching for response escalation (board statement 8-K, PREC14A, or follow-up 13D/A).

### 🟡 APPROACHING — 14–60 days
- **RGR — Annual Meeting May 27, 2026 (T-41)**: S67 direct PRER14A parse re-confirmed Apr 11 cooperation-agreement draft. Next hard date = DEF 14A (10-20 days from PRER14A → early May).
- **GSAT — Schedule 14C filing (~late Apr – early May)**: majority consent legally locked (Monroe 45.75% + Thermo). Spread 11.8% to worst-case floor.
- **MNKD on 2026-05-29 (T-43)**: re-evaluation at T-20 (~May 8).
- **ARVN on 2026-06-05 (T-50)**: deep dive at T-20 (~May 15).
- **AVNS DEF 14A ~late May**: DEFA14A Apr 14 filings confirmed; DEF 14A next. Annual Meeting previously scheduled Apr 21 postponed pending special meeting for merger vote.
- **ACHV on 2026-06-20 (T-65)**: mcap floor monitoring.
- **VRDN on 2026-06-30 (T-75)**: routine monitoring.

### 🟢 TRACKING — 60+ days
- **VERA on 2026-07-07 (T-82)**, **AVNS deal close H2 2026**, **SVRA/CAPR on 2026-08-22**, **SRRK ~2026-08-31**, **GSAT close 2027**.

---

## Recently Resolved (for context)

| Ticker | Event | Outcome | Resolved |
|--------|-------|---------|----------|
| TVTX | FDA PDUFA for FILSPARI in FSGS | ✅ APPROVED 2026-04-13 after-hours. Gapped $30.70 → $41+ sustained (+34%). | 2026-04-13 |

---

## S68 Changes Summary

- **AXSM kill-sweep #30 at T-14 CLEAN**. No new 4/144/8-K. Price $184.79 (+0.33% from S67's $184.18). Thesis intact. Next intensive check T-12 (Apr 18).
- **VERA CEO Fordyce Apr 14 trades re-parsed from primary XML**: `<aff10b5One>1</aff10b5One>` flag set = affirmed 10b5-1 plan (same plan as S67 finding); three tranches at weighted-avg $43.66 / $44.58 / $45.51. Routine. Price $42.19 (-2.9% from S67 $43.45) reflects ongoing 10b5-1 selling pressure but no thesis change.
- **SEM entity-resolution correction logged**: S67 incorrectly mapped SEM to CIK 0001320350 (LENSAR/LNSR); correct SEM CIK = 0001320414. The PREM14A + SC 13E3 Apr 15 filings on Select Medical's actual CIK match the deal specs in the candidate file. Added as a cross-check rule in Warning #0r (entity resolution).
- **GSAT 8-K termination fee confirmed in body**: $419,832,000 (for the Company paying Amazon under breach / fiduciary-out scenarios).
- **Daily scanner pipeline**: 5/5 scanners OK (edgar 1, congressional 12, contract 0, esma_short 11, fda_pdufa 10) → 34 total signals, 0 convergences. Report `reports/2026-04-16_daily_report.md`.
- **Fresh prices** (yfinance fast_info at 2026-04-16 16:20 UTC): AXSM $184.79, RPAY $3.17, RGR $41.93, VERA $42.19, AVNS $24.64, GSAT $79.80, SEM $16.38, MNKD $2.71, ARVN $10.91, VRDN $14.89.

---

## S67 Changes Summary (preserved)

- **SEM: NEW WATCHLIST ENTRY at score 22.5**. WCAS XIV + Ortenzio family $16.50 all-cash take-private. PREM14A + SC 13E-3 filed Apr 15; Merger Agreement signed Mar 2.
- **GSAT: MAJOR primary-source addendum**. Monroe SC 13D/A #14 Item 4 confirmed Monroe 45.75% + Written Consent = majority legally locked. Filing expectation redirected to Schedule 14C.
- **RGR: direct PRER14A parse re-confirms Apr 11 cooperation-agreement draft**. Settlement channel remains active.
- **AXSM: kill-sweep #29 CLEAN at T-14**.
- **RPAY: ≈33.0 sustained**. Cross-check rule: always verify filing is novel vs prior session logs.

---

## S64 Changes Summary (preserved)

- **AXSM kill-sweep #26 CLEAN at T-18**. No 4/144/8-K in 72h window.
- **RPAY upgrade 30.0 → 33.0**: Veradace 13D Apr 15 + track record verified (multi-campaign activist, SSTI parallel).
- **GSAT promoted to active from watchlist**: Apr 13 Thermo Support Agreement + 58% written consent legally binds majority.
- **AVNS DEFA14A + 8-K Apr 14 re-verified**: no thesis change, score 28.5.

---

*Monitoring cadence: scheduled sessions every 3 hours. Intensive-window candidates (AXSM at T-14) get deep kill-sweep every session; others get standard SEC-submissions-API check.*
