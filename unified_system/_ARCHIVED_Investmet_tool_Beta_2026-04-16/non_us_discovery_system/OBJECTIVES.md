# OBJECTIVES — Non-US Primary-Source Discovery System (Tool 2)

## Primary Goal

Build and operate an autonomous investment signal discovery system that identifies non-US-listed equity investment opportunities from primary-source disclosures on nine non-US exchanges — sources that are legally public, free, and real-time, but almost invisible to English-language research workflows because the filings are in local languages, on local portals, or in document formats (NI 43-101, HKEx profit warning, Tanshin, CVM material fact) foreign to US-focused analysts.

The system is structurally complementary to Tool 1 (the US-centric catalyst discovery system). Every candidate this system produces is one Tool 1 categorically cannot find.

## Mandate

- **Universe:** publicly listed equities on the nine target exchanges, minimum USD $300M market cap.
- **Edge type:** structural language + geography asymmetry (not temporarily unnoticed — persistently invisible to English-only research).
- **Holding horizon:** weeks to months.
- **Position sizing:** satellite positions (2–5% of portfolio) — asymmetric risk/reward.
- **Constraint:** legal public data only; free APIs / free disclosure portals only; no proprietary feeds; no CAPTCHA-walled sources.
- **Reporting:** daily signal reports + full candidate writeups at 28+ scores, identical shape to Tool 1's output.

## Geographic Scope — The Nine Target Exchanges

| # | Exchange | Country | Primary language | Universe (approx. listed ≥ $300M) | Build phase |
|---|----------|---------|------------------|-----------------------------------|-------------|
| 1 | LSE RNS | United Kingdom | English | ~1,100 | Phase 1 |
| 2 | TDnet | Japan | Japanese | ~2,500 | Phase 2 |
| 3 | ASX Announcements | Australia | English | ~600 | Phase 3 |
| 4 | SEDAR+ | Canada | English/French | ~900 | Phase 4 |
| 5 | HKEx News | Hong Kong | English/Chinese | ~1,200 | Phase 5 |
| 6 | KIND | Korea | Korean | ~800 | Phase 6 |
| 7 | BSE/NSE | India | English/Hindi | ~900 | Phase 7 |
| 8 | CVM | Brazil | Portuguese | ~300 | Phase 8 |
| 9 | BMV | Mexico | Spanish | ~150 | Phase 9 |

Combined target universe: approximately 8,000 listed companies at the market-cap floor.

## The 9 Strategies

| # | Strategy | Edge Source |
|---|----------|------------|
| 1 | LSE RNS scanning | World's most transparent corporate-action disclosure regime; full text scan catches Rule 2.7, 2.4, TR-1, JORC, AIM events |
| 2 | TDnet scanning | Japan's ~3,800-name universe has near-zero English research coverage; Tanshin and material-fact filings are in-session translatable |
| 3 | ASX announcements | English-language high-signal regime; Appendix 4C cash-flow filings flag distress in small-caps early |
| 4 | SEDAR+ filings | NI 43-101 technical reports are binary events for the large Canadian mining universe, free to read |
| 5 | HKEx News | HK rules mandate profit warnings — systematic scanning catches every pre-announced earnings miss |
| 6 | KIND disclosures | Korean corporate governance events, chaebol-related-party transactions, tender offers — Korean-language barrier is the moat |
| 7 | BSE/NSE disclosures | SEBI Regulation 30 material disclosures, SAST 5% shareholder filings, promoter pledge disclosures |
| 8 | CVM material facts | CVM Resolution 44 disclosures for Brazilian listed equity; Portuguese-language barrier is the moat |
| 9 | BMV eventos relevantes | Mexican material-fact filings; Spanish-language barrier is the moat |

## Sub-Goals

1. **Signal infrastructure** — common JSON signal schema (identical to Tool 1's), OpenFIGI entity resolution, internal convergence detection across the nine strategies, cross-listing-aware deduplication (D-004).
2. **Quality over quantity** — every candidate survives a 3-stage pipeline (triage → scoring → deep dive). 2–5 high-conviction candidates per week across the combined universe.
3. **Translation integrity** — non-English scanners default `thesis_direction` to `unknown` unless direction is unambiguous (D-002).
4. **Full autonomy** — scheduled Cowork sessions run the full pipeline without Pedro's intervention.
5. **Structural independence from Tool 1** — own folder, own lock, own candidates, own reports. Cross-system signal merging happens only via a separate analyzer project, never through direct file coupling.

## Success Criteria

- [ ] All 9 Python scanner tools built, compiled, producing valid JSON signals in the common schema.
- [ ] OpenFIGI entity resolution operational across all 9 exchanges (tickers + MIC → FIGI → issuer_figi).
- [ ] Internal convergence engine detecting cross-strategy overlap within 14-day rolling window, with cross-listing dedup.
- [ ] Daily scheduled pipeline running: scan → triage → entity-resolve → converge → score → deep dive → report → kill-condition monitor.
- [ ] First batch of validated candidates produced with full deep dive analysis.
- [ ] System operates autonomously for 7 consecutive days without manual intervention.

## Definition of Done

The system is "done" when a single scheduled Cowork session can: run all 9 scanners (or as many as are healthy that day), triage and score the results, detect convergences (de-duplicated by cross-listing), produce or update candidate writeups for any signal scoring 28+, monitor existing candidates against kill conditions, and output a daily report — all without human input. Pedro reviews at his discretion; the system does not depend on him being present.
